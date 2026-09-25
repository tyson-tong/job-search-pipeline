CREATE TABLE IF NOT EXISTS match_assessments (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cv_variant TEXT NOT NULL,
    cv_sha256 TEXT NOT NULL,
    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
    matched JSONB NOT NULL DEFAULT '[]'::jsonb,
    partial JSONB NOT NULL DEFAULT '[]'::jsonb,
    gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
