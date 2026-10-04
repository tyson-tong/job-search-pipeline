-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
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
    fingerprint TEXT,
    source TEXT,
    match_key TEXT,
    salary_min NUMERIC(12, 2),
    salary_max NUMERIC(12, 2),
    filter_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'new',
    status_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_posted_at_idx ON jobs (posted_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_fingerprint_idx ON jobs (fingerprint);
CREATE INDEX IF NOT EXISTS jobs_match_key_idx ON jobs (match_key, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS jobs_description_fts_idx
    ON jobs USING GIN (to_tsvector('english', coalesce(description, '')));

-- Migration ID: 02
-- The country a job was found in, so one ad for offices in two countries stays two jobs.
-- Then: backfill_countries
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS country TEXT;

-- Migration ID: 03
-- An archived job's scraped record is cleared after a while (see upkeep.prune_archived_jobs).
ALTER TABLE jobs ALTER COLUMN raw_json DROP NOT NULL;

-- Migration ID: 04
-- Tracker search matches titles, companies, and locations with ILIKE; nothing searched descriptions.
DROP INDEX IF EXISTS jobs_description_fts_idx;

-- Migration ID: 05
-- Never read: with a 24-hour search window a job is rarely seen twice.
ALTER TABLE jobs DROP COLUMN IF EXISTS last_seen_at;

-- Migration ID: 06
-- The statuses jobflow knows (db.STATUSES): a typo in code fails at once instead of hiding a job.
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN (
    'new', 'interested', 'not_interested', 'applied', 'interviewing', 'offered', 'accepted',
    'declined', 'rejected', 'withdrawn', 'filtered', 'archived'));

-- Migration ID: 07
-- Similar jobs (every tracker refresh) and the merge rules look at jobs first seen recently.
CREATE INDEX IF NOT EXISTS jobs_first_seen_at_idx ON jobs (first_seen_at DESC);

-- Migration ID: 08
-- A salary whose pay period the ad didn't state, so it was guessed from the amount; it never
-- filters a job out.
-- Then: backfill_salary_estimates
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_estimated BOOLEAN NOT NULL DEFAULT false;

-- Migration ID: 09
-- Titles keep + and # now (dedupe.title_key), so "C++ Developer" and "C# Developer" are two
-- titles: the keys of jobs whose title has one are worked out again, in the follow-up.
-- Then: backfill_title_keys
SELECT 1;
