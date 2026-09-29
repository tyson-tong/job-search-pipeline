-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
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

-- Migration ID: 02
-- tailor-cv, cover-letter, and interview-prep look up each job's latest run of their kind.
CREATE INDEX IF NOT EXISTS workflow_runs_job_idx ON workflow_runs (job_id, kind, id DESC);

-- Migration ID: 03
-- Nothing wrote error, and started_at was always the time the row was written.
ALTER TABLE workflow_runs DROP COLUMN IF EXISTS error, DROP COLUMN IF EXISTS started_at;
