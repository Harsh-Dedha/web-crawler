"""Per-domain politeness: robots.txt rules, rate limiting, and caching."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import aiohttp

logger = logging.getLogger(__name__)


class PolitenessManager:
    """Enforces per-domain rate limits and robots.txt rules asynchronously.

    A lock is held for the duration of the inter-request delay.  That is
    intentional: only one coroutine per domain may be inside the critical
    section, while requests to other domains continue concurrently.
    """

    def __init__(
        self,
        user_agent: str,
        default_delay: float = 1.0,
        robots_timeout: float = 5.0,
    ) -> None:
        self.user_agent = user_agent
        self.default_delay = max(0.0, default_delay)
        self.robots_timeout = robots_timeout
        self.domain_locks: Dict[str, asyncio.Lock] = {}
        self.robots_locks: Dict[str, asyncio.Lock] = {}
        self.last_access_times: Dict[str, float] = {}
        self.robots_cache: Dict[str, RobotFileParser] = {}

    def _get_lock(self, domain: str) -> asyncio.Lock:
        lock = self.domain_locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self.domain_locks[domain] = lock
        return lock

    def _get_robots_lock(self, domain: str) -> asyncio.Lock:
        lock = self.robots_locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self.robots_locks[domain] = lock
        return lock

    async def can_fetch(self, session: aiohttp.ClientSession, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if parsed.scheme not in ("http", "https") or not domain:
            return False

        if domain not in self.robots_cache:
            # Double-checked locking prevents a thundering herd of robots.txt
            # requests when many workers first touch a domain.
            async with self._get_robots_lock(domain):
                if domain not in self.robots_cache:
                    self.robots_cache[domain] = await self._fetch_robots_txt(
                        session, parsed.scheme, domain
                    )

        return self.robots_cache[domain].can_fetch(self.user_agent, url)

    async def _fetch_robots_txt(
        self, session: aiohttp.ClientSession, scheme: str, domain: str
    ) -> RobotFileParser:
        robots_url = urljoin(f"{scheme}://{domain}", "/robots.txt")
        parser = RobotFileParser()
        try:
            async with session.get(
                robots_url,
                timeout=aiohttp.ClientTimeout(total=self.robots_timeout),
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
            ) as response:
                if response.status == 200:
                    text = await response.text(errors="replace")
                    parser.parse(text.splitlines())
                    logger.info("Loaded robots.txt for %s", domain)
                else:
                    # 404/403 means there is no usable policy; allow crawling.
                    parser.parse([])
                    logger.info(
                        "No robots.txt for %s (HTTP %s); allowing all",
                        domain,
                        response.status,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Network failure: be permissive, matching the original design.
            parser.parse([])
            logger.warning("Could not fetch robots.txt for %s: %s", domain, exc)
        return parser

    async def throttle(self, domain: str) -> None:
        """Sleep if this domain was accessed less than ``default_delay`` ago."""
        lock = self._get_lock(domain)
        async with lock:
            now = time.monotonic()
            last_access = self.last_access_times.get(domain, 0.0)
            elapsed = now - last_access
            if elapsed < self.default_delay:
                await asyncio.sleep(self.default_delay - elapsed)
            self.last_access_times[domain] = time.monotonic()
