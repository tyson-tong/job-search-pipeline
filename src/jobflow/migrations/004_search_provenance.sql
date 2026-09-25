-- Parameterised searches: which search and exact actor input produced each run, the
-- annualised salary range parsed from each job, and why a job was filtered out.
ALTER TABLE scraper_runs ADD COLUMN IF NOT EXISTS search_name TEXT;
ALTER TABLE scraper_runs ADD COLUMN IF NOT EXISTS input JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_min NUMERIC(12, 2);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_max NUMERIC(12, 2);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS filter_reason TEXT;
