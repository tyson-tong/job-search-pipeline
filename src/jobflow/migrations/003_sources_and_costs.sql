-- Multiple job sites: which site a job was first seen on, a company+title key for
-- fuzzy cross-site duplicate detection, and what each Apify run cost.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS match_key TEXT;
CREATE INDEX IF NOT EXISTS jobs_match_key_idx ON jobs (match_key, first_seen_at DESC);
ALTER TABLE scraper_runs ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 4);
