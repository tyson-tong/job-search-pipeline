-- Application tracking for the web app: clearer status names, a follow-up date, and a
-- history of every status change.
UPDATE jobs SET status = 'not_interested' WHERE status = 'ignored';
UPDATE jobs SET status = 'offered' WHERE status = 'offer';
UPDATE applications SET status = 'not_interested' WHERE status = 'ignored';
UPDATE applications SET status = 'offered' WHERE status = 'offer';

ALTER TABLE applications ADD COLUMN IF NOT EXISTS follow_up_on DATE;

CREATE TABLE IF NOT EXISTS status_history (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    via TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS status_history_job_idx ON status_history (job_id, changed_at);
