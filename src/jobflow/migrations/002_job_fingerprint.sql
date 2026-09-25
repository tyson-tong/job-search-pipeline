-- Cross-source duplicate detection: the same posting on different job sites has
-- different source IDs and URLs, so it is matched on a content fingerprint.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fingerprint TEXT;
CREATE INDEX IF NOT EXISTS jobs_fingerprint_idx ON jobs (fingerprint);
