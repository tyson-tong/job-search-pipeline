from __future__ import annotations

import os
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[2]


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(url)


def migrate() -> None:
    sql = (ROOT / "migrations" / "001_initial.sql").read_text()
    with connect() as conn:
        conn.execute(sql)


def fetch_job(job_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, canonical_url, title, company, location, description, raw_json "
            "FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not row:
        raise RuntimeError(f"No job with database id {job_id}")
    keys = ("id", "url", "title", "company", "location", "description", "raw_json")
    return dict(zip(keys, row))


def list_jobs(limit: int = 30) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, company, location, posted_at, status FROM jobs "
            "ORDER BY first_seen_at DESC LIMIT %s", (limit,)).fetchall()
    return [dict(zip(("id", "title", "company", "location", "posted_at", "status"), row))
            for row in rows]
