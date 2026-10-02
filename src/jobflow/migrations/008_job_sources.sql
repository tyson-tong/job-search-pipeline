-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
-- Then: backfill_sources
CREATE TABLE IF NOT EXISTS job_sources (
    source_key TEXT PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_job_id TEXT,
    url TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_sources_job_idx ON job_sources (job_id);

-- Migration ID: 02
-- Never read: source_job_id is in source_key, and a copy is rarely seen twice.
ALTER TABLE job_sources DROP COLUMN IF EXISTS source_job_id, DROP COLUMN IF EXISTS last_seen_at;
