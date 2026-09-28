-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
CREATE TABLE IF NOT EXISTS job_observations (
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scraper_run_id BIGINT NOT NULL REFERENCES scraper_runs(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_json JSONB NOT NULL,
    PRIMARY KEY (job_id, scraper_run_id)
);

-- Migration ID: 02
-- A sighting's scraped record is dropped after a while (see store.prune_sightings).
ALTER TABLE job_observations ALTER COLUMN observed_json DROP NOT NULL;
