-- Migration ID: 01
-- Add later changes here under the next ID (02, 03, ...); each block runs once.
CREATE TABLE IF NOT EXISTS applications (
    job_id BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    applied_at TIMESTAMPTZ,
    follow_up_on DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
