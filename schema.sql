CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS crawled_pages (
    id BIGSERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    domain VARCHAR(255) NOT NULL,
    title TEXT,
    content_clean TEXT NOT NULL,
    doc_length INT NOT NULL CHECK (doc_length >= 0),
    status_code INT NOT NULL,
    tsv_content tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content_clean, '')), 'B')
    ) STORED,
    crawled_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain
    ON crawled_pages(domain);

CREATE INDEX IF NOT EXISTS idx_crawled_pages_status
    ON crawled_pages(status_code);

CREATE INDEX IF NOT EXISTS idx_crawled_pages_tsv
    ON crawled_pages USING GIN(tsv_content);

CREATE INDEX IF NOT EXISTS idx_crawled_pages_title_trgm
    ON crawled_pages USING GIN(title gin_trgm_ops);
