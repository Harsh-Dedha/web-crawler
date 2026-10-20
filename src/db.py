"""Async PostgreSQL persistence layer."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class Database:
    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        pool_kwargs: Dict[str, Any] = {
            "min_size": settings.POSTGRES_MIN_POOL,
            "max_size": settings.POSTGRES_MAX_POOL,
            "command_timeout": 60,
            "server_settings": {"application_name": "web-crawler"},
        }
        if settings.POSTGRES_DSN:
            self.pool = await asyncpg.create_pool(
                dsn=settings.POSTGRES_DSN, **pool_kwargs
            )
        else:
            self.pool = await asyncpg.create_pool(
                host=settings.POSTGRES_HOST,
                port=settings.POSTGRES_PORT,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD,
                database=settings.POSTGRES_DB,
                **pool_kwargs,
            )

        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")
        await self.initialize_schema()
        logger.info(
            "Connected to PostgreSQL %s:%s/%s",
            settings.POSTGRES_HOST,
            settings.POSTGRES_PORT,
            settings.POSTGRES_DB,
        )

    async def initialize_schema(self) -> None:
        """Apply schema.sql, making local/first-run setup self-contained."""
        if not SCHEMA_PATH.exists():
            logger.warning("schema.sql not found at %s", SCHEMA_PATH)
            return
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("Database schema is ready")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def save_page(
        self,
        url: str,
        domain: str,
        title: Optional[str],
        content_clean: str,
        doc_length: int,
        status_code: int,
    ) -> Optional[int]:
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")

        query = """
        INSERT INTO crawled_pages
            (url, domain, title, content_clean, doc_length, status_code)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (url) DO UPDATE
        SET domain = EXCLUDED.domain,
            title = EXCLUDED.title,
            content_clean = EXCLUDED.content_clean,
            doc_length = EXCLUDED.doc_length,
            status_code = EXCLUDED.status_code,
            crawled_at = CURRENT_TIMESTAMP
        RETURNING id;
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                query, url, domain, title or "", content_clean, doc_length, status_code
            )

    async def get_all_documents(self) -> List[asyncpg.Record]:
        """Fetch the fields required to build the in-memory inverted index."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        query = """
        SELECT id, url, domain, title, content_clean, doc_length
        FROM crawled_pages
        WHERE status_code = 200
        ORDER BY id;
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(query)

    async def postgres_search(
        self, query: str, limit: int = 10
    ) -> List[asyncpg.Record]:
        """Native PostgreSQL full-text search using the generated tsvector."""
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        sql = """
        WITH q AS (SELECT plainto_tsquery('english', $1) AS query)
        SELECT p.id, p.url, p.domain, p.title,
               ts_rank_cd(p.tsv_content, q.query, 32) AS rank,
               ts_headline(
                   'english', p.content_clean, q.query,
                   'MaxWords=35, MinWords=15, StartSel=<mark>, StopSel=</mark>'
               ) AS snippet
        FROM crawled_pages p, q
        WHERE p.tsv_content @@ q.query
        ORDER BY rank DESC, p.crawled_at DESC
        LIMIT $2;
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, query, limit)

    async def count_pages(self) -> int:
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        async with self.pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT count(*) FROM crawled_pages WHERE status_code = 200"
                )
                or 0
            )

    async def get_stats(self) -> Dict[str, Any]:
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        sql = """
        SELECT count(*) FILTER (WHERE status_code = 200) AS pages,
               coalesce(sum(doc_length) FILTER (WHERE status_code = 200), 0) AS words,
               count(DISTINCT domain) FILTER (WHERE status_code = 200) AS domains,
               max(crawled_at) AS last_crawled_at
        FROM crawled_pages;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql)
        return dict(row) if row else {}

    async def recent_documents(self, limit: int = 20) -> List[asyncpg.Record]:
        if not self.pool:
            raise RuntimeError("Database pool not initialized.")
        sql = """
        SELECT id, url, domain, title, doc_length, crawled_at
        FROM crawled_pages
        WHERE status_code = 200
        ORDER BY crawled_at DESC
        LIMIT $1;
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, limit)

    async def ping(self) -> bool:
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:
            return False


db = Database()
