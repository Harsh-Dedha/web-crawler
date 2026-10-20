"""Application configuration.

Values can be overridden with environment variables or a local ``.env`` file.
The defaults target the local native PostgreSQL/Redis setup documented in the
README, and also work with the provided docker-compose services.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "crawler_user"
    POSTGRES_PASSWORD: str = "crawler_password"
    POSTGRES_DB: str = "search_engine"
    POSTGRES_DSN: str = ""
    POSTGRES_MIN_POOL: int = 2
    POSTGRES_MAX_POOL: int = 10

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""

    # Crawler
    CRAWLER_MAX_CONCURRENCY: int = 50
    CRAWLER_WORKERS: int = 8
    CRAWLER_DOMAIN_DELAY: float = 1.0  # seconds between requests to one host
    CRAWLER_REQUEST_TIMEOUT: int = 10
    CRAWLER_MAX_RETRIES: int = 3
    CRAWLER_MAX_PAGES: int = 25
    CRAWLER_MAX_LINKS_PER_PAGE: int = 40
    CRAWLER_MIN_CONTENT_CHARS: int = 80
    CRAWLER_SSL_VERIFY: bool = True
    CRAWLER_USER_AGENT: str = "MiniSearchCrawler/1.0 (+https://example.com/bot)"
    # Comma-separated host allow-list. Empty means "allow every host".
    CRAWLER_ALLOWED_DOMAINS: str = ""

    # Bloom filter
    BLOOM_EXPECTED_ELEMENTS: int = 1_000_000
    BLOOM_FALSE_POSITIVE_RATE: float = 0.01
    BLOOM_KEY: str = "crawler:bloom_filter"

    # Frontier
    FRONTIER_QUEUE_KEY: str = "crawler:frontier:queue"
    FRONTIER_QUEUED_SET_KEY: str = "crawler:frontier:queued"

    @property
    def allowed_domains(self) -> set[str]:
        return {
            part.strip().lower()
            for part in self.CRAWLER_ALLOWED_DOMAINS.split(",")
            if part.strip()
        }


settings = Settings()
