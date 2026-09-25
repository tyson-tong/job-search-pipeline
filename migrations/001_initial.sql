CREATE TABLE IF NOT EXISTS scraper_runs (
    id BIGSERIAL PRIMARY KEY,
    actor_id TEXT NOT NULL,
    apify_run_id TEXT UNIQUE,
    dataset_id TEXT,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    items_seen INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id BIGSERIAL PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    source_job_id TEXT,
    canonical_url TEXT,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    posted_at DATE,
    description TEXT,
    raw_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'new',
    status_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    model_name TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS jobs_posted_at_idx ON jobs (posted_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_description_fts_idx
    ON jobs USING GIN (to_tsvector('english', coalesce(description, '')));

CREATE TABLE IF NOT EXISTS job_observations (
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scraper_run_id BIGINT NOT NULL REFERENCES scraper_runs(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_json JSONB NOT NULL,
    PRIMARY KEY (job_id, scraper_run_id)
);

CREATE TABLE IF NOT EXISTS match_assessments (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cv_variant TEXT NOT NULL,
    cv_sha256 TEXT NOT NULL,
    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    matched JSONB NOT NULL DEFAULT '[]'::jsonb,
    partial JSONB NOT NULL DEFAULT '[]'::jsonb,
    gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applications (
    job_id BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    applied_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
