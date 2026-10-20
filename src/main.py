"""FastAPI application exposing the crawler and both search engines."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from config import settings
from src.bm25 import BM25Ranker
from src.crawler import DistributedCrawler
from src.db import db
from src.indexer import InvertedIndex

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

UI_PATH = Path(__file__).resolve().parent / "static" / "index.html"

inverted_index = InvertedIndex()
ranker = BM25Ranker(inverted_index)
crawler_instance: Optional[DistributedCrawler] = None
redis_conn: Optional[aioredis.Redis] = None
crawler_task: Optional[asyncio.Task] = None
index_lock = asyncio.Lock()


def _build_index(records: List[Any]) -> InvertedIndex:
    index = InvertedIndex()
    index.build_from_records(records)
    return index


async def _load_index() -> InvertedIndex:
    """Build a fresh index off the event loop and swap it in atomically."""
    global inverted_index, ranker
    async with index_lock:
        records = await db.get_all_documents()
        new_index = await asyncio.to_thread(_build_index, records)
        inverted_index = new_index
        ranker = BM25Ranker(new_index)
        logger.info(
            "Index rebuilt: %s docs, %s terms, avgdl=%.2f",
            new_index.total_docs,
            new_index.vocabulary_size,
            new_index.avg_doc_length,
        )
        return new_index


def _task_done(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("Crawler background task failed")


async def _stop_crawler() -> None:
    global crawler_task
    if crawler_instance:
        crawler_instance.stop()
    if crawler_task and not crawler_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(crawler_task), timeout=6.0)
        except asyncio.TimeoutError:
            crawler_task.cancel()
            await asyncio.gather(crawler_task, return_exceptions=True)
    crawler_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global crawler_instance, redis_conn
    try:
        await db.connect()

        redis_conn = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
            health_check_interval=30,
        )
        await redis_conn.ping()
        logger.info("Connected to Redis %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)

        crawler_instance = DistributedCrawler(redis_conn)
        await _load_index()
        yield
    finally:
        await _stop_crawler()
        if redis_conn:
            await redis_conn.aclose()
            redis_conn = None
        await db.close()


app = FastAPI(
    title="MiniSearch — Distributed Search Engine",
    description=(
        "Async politeness-aware crawler + Redis Bloom filter + PostgreSQL "
        "tsvector + in-memory NumPy BM25."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "distributed-search-engine",
        "docs": "/docs",
        "endpoints": {
            "demo": "/demo",
            "seed": "POST /crawler/seed  (body: [\"https://example.com\"])",
            "start": "POST /crawler/start?workers=5",
            "stop": "POST /crawler/stop",
            "status": "GET /crawler/status",
            "rebuild": "POST /index/rebuild",
            "search_bm25": "GET /search?q=...&mode=bm25",
            "search_postgres": "GET /search?q=...&mode=postgres",
            "stats": "GET /stats",
        },
    }


@app.get("/demo", include_in_schema=False)
async def demo() -> FileResponse:
    if not UI_PATH.exists():
        raise HTTPException(status_code=404, detail="Demo UI not installed")
    return FileResponse(UI_PATH)


@app.get("/bot")
async def bot_info() -> Dict[str, str]:
    return {
        "user_agent": settings.CRAWLER_USER_AGENT,
        "policy": "Polite crawling: robots.txt respected, 1 request/second/domain.",
    }


@app.get("/health")
async def health() -> JSONResponse:
    db_ok = await db.ping()
    redis_ok = False
    if redis_conn:
        try:
            redis_ok = bool(await redis_conn.ping())
        except Exception:
            redis_ok = False
    status = "ok" if db_ok and redis_ok else "degraded"
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content={"status": status, "postgres": db_ok, "redis": redis_ok},
    )


@app.post("/crawler/seed")
async def seed_crawler(urls: list[str]) -> Dict[str, Any]:
    if not crawler_instance:
        raise HTTPException(status_code=503, detail="Crawler not initialized")
    if not urls:
        raise HTTPException(status_code=400, detail="No seed URLs supplied")
    queued = await crawler_instance.add_seeds(urls)
    return {
        "message": f"Queued {queued} seed URLs.",
        "received": len(urls),
        "queued": queued,
    }


@app.post("/crawler/start")
async def start_crawler(
    workers: int = Query(settings.CRAWLER_WORKERS, ge=1, le=64)
) -> Dict[str, Any]:
    global crawler_task
    if not crawler_instance:
        raise HTTPException(status_code=503, detail="Crawler not initialized")
    if crawler_task and not crawler_task.done():
        return {
            "status": "already_running",
            "workers": workers,
            "pages_crawled_this_run": crawler_instance.pages_crawled,
        }

    crawler_task = asyncio.create_task(
        crawler_instance.start(workers), name="crawler-workers"
    )
    crawler_task.add_done_callback(_task_done)
    return {
        "status": "Crawler background process started",
        "workers": workers,
        "max_pages": settings.CRAWLER_MAX_PAGES,
    }


@app.post("/crawler/stop")
async def stop_crawler() -> Dict[str, Any]:
    if not crawler_instance:
        raise HTTPException(status_code=503, detail="Crawler not initialized")
    pages = crawler_instance.pages_crawled
    await _stop_crawler()
    return {"status": "Crawler stopped", "pages_crawled_this_run": pages}


@app.get("/crawler/status")
async def crawler_status() -> Dict[str, Any]:
    if not crawler_instance:
        raise HTTPException(status_code=503, detail="Crawler not initialized")
    status = await crawler_instance.status()
    status["running"] = bool(crawler_task and not crawler_task.done())
    try:
        status["pages_in_database"] = await db.count_pages()
    except Exception:
        status["pages_in_database"] = None
    return status


@app.post("/index/rebuild")
async def rebuild_index() -> Dict[str, Any]:
    index = await _load_index()
    return {
        "status": "Index built",
        "total_documents": index.total_docs,
        "vocabulary_size": index.vocabulary_size,
        "avg_doc_length": round(index.avg_doc_length, 2),
    }


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=512, description="Query phrase"),
    mode: Literal["bm25", "postgres"] = Query(
        "bm25", description="Search engine to use"
    ),
    limit: int = Query(10, ge=1, le=50),
) -> Dict[str, Any]:
    started = time.perf_counter()

    if mode == "bm25":
        results = ranker.search(q, top_k=limit)
        return {
            "mode": "in-house-bm25",
            "query": q,
            "count": len(results),
            "took_ms": round((time.perf_counter() - started) * 1000, 2),
            "results": results,
        }

    rows = await db.postgres_search(q, limit=limit)
    results = [
        {
            "doc_id": row["id"],
            "url": row["url"],
            "domain": row["domain"],
            "title": row["title"],
            "snippet": row["snippet"],
            "score": float(row["rank"]),
        }
        for row in rows
    ]
    return {
        "mode": "postgres-tsvector",
        "query": q,
        "count": len(results),
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "results": results,
    }


@app.get("/stats")
async def stats() -> Dict[str, Any]:
    database = await db.get_stats()
    crawler = await crawler_instance.status() if crawler_instance else {}
    return {
        "database": database,
        "index": {
            "total_documents": inverted_index.total_docs,
            "vocabulary_size": inverted_index.vocabulary_size,
            "avg_doc_length": round(inverted_index.avg_doc_length, 2),
        },
        "crawler": crawler,
    }


@app.get("/documents")
async def documents(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    rows = await db.recent_documents(limit=limit)
    return {
        "count": len(rows),
        "documents": [
            {
                "id": row["id"],
                "url": row["url"],
                "domain": row["domain"],
                "title": row["title"],
                "doc_length": row["doc_length"],
                "crawled_at": row["crawled_at"],
            }
            for row in rows
        ],
    }
