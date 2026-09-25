-- Every site's copy of a job: its key (e.g. seek:81234567), ID, and link. jobs keeps the
-- first site's details; this table lets later sightings on any site match by their own
-- ID instead of the fuzzy step, and gives each job a link per site.
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

INSERT INTO job_sources (source_key, job_id, source, source_job_id, url, first_seen_at,
                         last_seen_at)
SELECT canonical_key, id, coalesce(source, split_part(canonical_key, ':', 1)), source_job_id,
       canonical_url, first_seen_at, last_seen_at
FROM jobs
ON CONFLICT (source_key) DO NOTHING;
