-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
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
    cost_usd NUMERIC(10, 4),
    search_name TEXT,
    input JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration ID: 02
-- A run that returned as many results as it was allowed probably left jobs behind; the
-- tracker's header and jobflow status flag it.
ALTER TABLE scraper_runs ADD COLUMN IF NOT EXISTS hit_cap BOOLEAN NOT NULL DEFAULT false;
