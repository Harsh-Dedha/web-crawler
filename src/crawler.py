"""Asynchronous, politeness-aware web crawler."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import aiohttp
import redis.asyncio as aioredis
from bs4 import BeautifulSoup

from config import settings
from src.bloom_filter import RedisBloomFilter
from src.db import db
from src.politeness import PolitenessManager

logger = logging.getLogger(__name__)

_TITLE_SUFFIX = re.compile(r"\s*[-|–]\s*Wikipedia\s*$", re.IGNORECASE)


def normalize_url(raw_url: str) -> str:
    """Canonicalise a URL enough to make Bloom-filter dedupe reliable."""
    without_fragment, _ = urldefrag(raw_url.strip())
    parsed = urlparse(without_fragment)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


class DistributedCrawler:
    def __init__(self, redis_client: aioredis.Redis) -> None:
        self.redis = redis_client
        self.bloom = RedisBloomFilter(
            redis_client=redis_client,
            key=settings.BLOOM_KEY,
            expected_elements=settings.BLOOM_EXPECTED_ELEMENTS,
            false_positive_rate=settings.BLOOM_FALSE_POSITIVE_RATE,
        )
        self.politeness = PolitenessManager(
            user_agent=settings.CRAWLER_USER_AGENT,
            default_delay=settings.CRAWLER_DOMAIN_DELAY,
        )
        self.queue_key = settings.FRONTIER_QUEUE_KEY
        self.queued_key = settings.FRONTIER_QUEUED_SET_KEY
        self.running = False
        self.pages_crawled = 0
        self.pages_started = 0
        self.started_at: Optional[float] = None
        self._counter_lock = asyncio.Lock()

    @staticmethod
    def _domain_allowed(domain: str) -> bool:
        allowed = settings.allowed_domains
        if not allowed:
            return True
        return domain in allowed or any(
            domain.endswith(f".{allowed_domain}") for allowed_domain in allowed
        )

    def clean_text(self, soup: BeautifulSoup) -> Tuple[str, str]:
        for element in soup(
            ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]
        ):
            element.decompose()

        title = ""
        if soup.title:
            title = soup.title.get_text(" ", strip=True)
        title = _TITLE_SUFFIX.sub("", title).strip()[:500]

        # Prefer the semantic main content when the site provides one.
        root = soup.find("main") or soup.find("article") or soup.body or soup
        text = root.get_text(separator=" ")
        clean_words = " ".join(text.split())
        return title, clean_words

    def extract_links(self, base_url: str, html: str) -> Set[str]:
        soup = BeautifulSoup(html, "lxml")
        found_links: Set[str] = set()
        for tag in soup.find_all("a", href=True):
            raw_url = (tag.get("href") or "").strip()
            if not raw_url or raw_url.startswith(
                ("mailto:", "javascript:", "tel:", "data:", "#")
            ):
                continue
            clean_link = normalize_url(urljoin(base_url, raw_url))
            parsed = urlparse(clean_link)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                found_links.add(clean_link)
        return found_links

    async def add_seeds(self, urls: list[str]) -> int:
        queued = 0
        for raw_url in urls:
            clean_url = normalize_url(raw_url)
            parsed = urlparse(clean_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                logger.warning("Skipping invalid seed URL: %s", raw_url)
                continue
            if not self._domain_allowed(parsed.netloc.lower()):
                logger.warning("Skipping seed outside the allow-list: %s", clean_url)
                continue
            # The Redis set prevents duplicate seeds before the Bloom filter
            # sees them during processing.
            if await self.redis.sadd(self.queued_key, clean_url):
                await self.redis.rpush(self.queue_key, clean_url)
                queued += 1
        return queued

    async def fetch_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> Tuple[int, str]:
        """Fetch a page, retrying 429/5xx responses with exponential backoff."""
        max_retries = max(1, settings.CRAWLER_MAX_RETRIES)
        backoff = 1.5
        headers = {
            "User-Agent": settings.CRAWLER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }

        for attempt in range(max_retries):
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=settings.CRAWLER_REQUEST_TIMEOUT
                    ),
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    if response.status in (429, 500, 502, 503, 504):
                        if attempt == max_retries - 1:
                            logger.warning(
                                "Giving up on %s after HTTP %s", url, response.status
                            )
                            return response.status, ""
                        retry_after = response.headers.get("Retry-After", "")
                        try:
                            delay = min(float(retry_after), 10.0)
                        except (TypeError, ValueError):
                            delay = backoff ** (attempt + 1)
                        await asyncio.sleep(delay)
                        continue

                    content_type = response.headers.get("Content-Type", "").lower()
                    if content_type and not any(
                        marker in content_type
                        for marker in ("text/html", "application/xhtml+xml")
                    ):
                        logger.info(
                            "Skipping non-HTML content at %s (%s)", url, content_type
                        )
                        return response.status, ""

                    html = await response.text(errors="replace")
                    return response.status, html
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Failed to fetch %s after %s attempts: %s",
                        url,
                        max_retries,
                        exc,
                    )
                    return 0, ""
                await asyncio.sleep(backoff ** (attempt + 1))

        return 0, ""

    async def process_url(self, session: aiohttp.ClientSession, url: str) -> None:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if not self._domain_allowed(domain):
            return

        # Bloom filter: O(1) probabilistic deduplication.  A false positive
        # only skips a page; it can never cause an already-seen page to be
        # fetched twice (no false negatives for inserted items).
        if await self.bloom.contains(url):
            return
        await self.bloom.add(url)

        if not await self.politeness.can_fetch(session, url):
            logger.info("Disallowed by robots.txt: %s", url)
            return

        # Claim one of the limited fetch slots.  Doing this before the network
        # request makes CRAWLER_MAX_PAGES a real upper bound even when several
        # workers are already in flight.
        if settings.CRAWLER_MAX_PAGES > 0:
            async with self._counter_lock:
                if self.pages_started >= settings.CRAWLER_MAX_PAGES:
                    self.running = False
                    return
                self.pages_started += 1

        await self.politeness.throttle(domain)

        status_code, html = await self.fetch_page(session, url)
        if status_code != 200 or not html:
            return

        try:
            soup = BeautifulSoup(html, "lxml")
            title, clean_content = self.clean_text(soup)
        except Exception as exc:
            logger.warning("Could not parse %s: %s", url, exc)
            return

        if len(clean_content) < settings.CRAWLER_MIN_CONTENT_CHARS:
            logger.info("Skipping thin page: %s", url)
            return

        doc_length = len(clean_content.split())
        try:
            doc_id = await db.save_page(
                url=url,
                domain=domain,
                title=title,
                content_clean=clean_content,
                doc_length=doc_length,
                status_code=status_code,
            )
        except Exception:
            logger.exception("Could not persist %s", url)
            return

        reached_limit = False
        async with self._counter_lock:
            self.pages_crawled += 1
            reached_limit = (
                settings.CRAWLER_MAX_PAGES > 0
                and self.pages_started >= settings.CRAWLER_MAX_PAGES
                and self.pages_crawled >= settings.CRAWLER_MAX_PAGES
            )

        logger.info(
            "[crawl %s] %s (%s words, doc_id=%s)",
            self.pages_crawled,
            url,
            doc_length,
            doc_id,
        )

        try:
            discovered_urls = self.extract_links(url, html)
        except Exception as exc:
            logger.warning("Could not extract links from %s: %s", url, exc)
            discovered_urls = set()

        queued_count = 0
        for link in discovered_urls:
            if queued_count >= settings.CRAWLER_MAX_LINKS_PER_PAGE:
                break
            link_domain = urlparse(link).netloc.lower()
            if not self._domain_allowed(link_domain):
                continue
            if await self.bloom.contains(link):
                continue
            # SADD makes enqueueing idempotent across workers.
            if await self.redis.sadd(self.queued_key, link):
                await self.redis.rpush(self.queue_key, link)
                queued_count += 1

        if reached_limit:
            logger.info(
                "Reached CRAWLER_MAX_PAGES=%s; stopping workers",
                settings.CRAWLER_MAX_PAGES,
            )
            self.running = False

    async def worker(self, worker_id: int) -> None:
        connector = aiohttp.TCPConnector(
            limit=settings.CRAWLER_MAX_CONCURRENCY,
            ssl=settings.CRAWLER_SSL_VERIFY,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.running:
                item = await self.redis.blpop(self.queue_key, timeout=2)
                if not item:
                    continue
                _, raw_url = item
                url = raw_url.decode("utf-8") if isinstance(raw_url, bytes) else raw_url
                await self.redis.srem(self.queued_key, url)
                try:
                    await self.process_url(session, url)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[Worker %s] Error handling %s", worker_id, url)

    async def start(self, num_workers: int = 8) -> None:
        if self.running:
            logger.info("Crawler is already running")
            return
        self.running = True
        self.pages_crawled = 0
        self.pages_started = 0
        self.started_at = time.time()
        logger.info("Starting crawler with %s concurrent workers", num_workers)
        tasks = [
            asyncio.create_task(self.worker(worker_id))
            for worker_id in range(max(1, num_workers))
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        finally:
            self.running = False
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Crawler stopped after %s pages", self.pages_crawled)

    def stop(self) -> None:
        self.running = False

    async def status(self) -> dict:
        queue_length = int(await self.redis.llen(self.queue_key))
        queued_set_size = int(await self.redis.scard(self.queued_key))
        bloom_bits = await self.bloom.bit_count()
        return {
            "running": self.running,
            "pages_crawled_this_run": self.pages_crawled,
            "pages_started_this_run": self.pages_started,
            "frontier_queue_length": queue_length,
            "frontier_queued_set_size": queued_set_size,
            "bloom_bits_set": bloom_bits,
            "bloom_size_bytes": self.bloom.memory_bytes,
            "bloom_hash_count": self.bloom.k,
            "started_at": self.started_at,
        }
