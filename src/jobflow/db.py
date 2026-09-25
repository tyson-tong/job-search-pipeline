from __future__ import annotations

import os
from datetime import date
from importlib import resources

import psycopg

STATUSES = ("new", "interested", "not_interested", "applied", "interviewing", "offered",
            "accepted", "declined", "rejected", "withdrawn", "filtered")
# Hidden from list by default and skipped by match --new. "filtered" is set on import
# when a job fails a search's local filters (see jobs.filter_reason).
CLOSED_STATUSES = ["not_interested", "accepted", "declined", "rejected", "withdrawn", "filtered"]
# Statuses meaning you've applied; the first time a job reaches one sets applied_at.
APPLIED_STATUSES = ["applied", "interviewing", "offered", "accepted", "declined"]


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(url)


def migration_files() -> list[tuple[str, str]]:
    """Return (name, sql) for each packaged migration, in filename order."""
    folder = resources.files("jobflow") / "migrations"
    return sorted((entry.name, entry.read_text()) for entry in folder.iterdir()
                  if entry.name.endswith(".sql"))


def migrate() -> None:
    from .ingest.store import backfill_keys, backfill_sources

    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            name TEXT PRIMARY KEY,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        newly_applied = []
        for name, sql in migration_files():
            if name in applied:
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
            newly_applied.append(name)
            print(f"Applied migration {name}")
        filled = backfill_keys(conn)
        if filled:
            print(f"Backfilled duplicate-detection keys for {filled} jobs")
        if "005_job_sources.sql" in newly_applied:
            added = backfill_sources(conn)
            if added:
                print(f"Recorded {added} more site copies in job_sources")


def fetch_job(job_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, canonical_url, title, company, location, description, raw_json "
            "FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not row:
        raise RuntimeError(f"No job with database id {job_id}")
    keys = ("id", "url", "title", "company", "location", "description", "raw_json")
    job = dict(zip(keys, row))
    job["description"] = job["description"] or ""
    return job


def list_jobs(limit: int = 30, by_score: bool = False, statuses: list[str] | None = None,
              include_closed: bool = False) -> list[dict]:
    """List jobs with the best score among each CV variant's latest assessment.

    Filters to the given statuses, else hides closed jobs unless include_closed.
    """
    if statuses:
        where, params = "WHERE j.status = ANY(%s)", [statuses]
    elif include_closed:
        where, params = "", []
    else:
        where, params = "WHERE j.status <> ALL(%s)", [CLOSED_STATUSES]
    order = "best_score DESC NULLS LAST, j.first_seen_at DESC" if by_score \
        else "j.first_seen_at DESC"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT j.id, j.title, j.company, j.location, j.posted_at, j.status,
                       best.score AS best_score, best.cv_variant
                FROM jobs j
                LEFT JOIN LATERAL (
                    SELECT latest.score, latest.cv_variant FROM (
                        SELECT DISTINCT ON (cv_variant) cv_variant, score
                        FROM match_assessments WHERE job_id = j.id
                        ORDER BY cv_variant, created_at DESC) latest
                    ORDER BY latest.score DESC LIMIT 1) best ON true
                {where}
                ORDER BY {order} LIMIT %s""", (*params, limit)).fetchall()
    keys = ("id", "title", "company", "location", "posted_at", "status", "score", "variant")
    return [dict(zip(keys, row)) for row in rows]


_UNSET = object()


def set_job_state(conn, job_id: int, *, status: str | None = None, note: str | None = None,
                  follow_up_on: object = _UNSET, via: str) -> None:
    """Change a job's status, add a dated note, and/or set its follow-up date, within
    the caller's transaction. Status changes are recorded in status_history.

    jobs.status is the single source of truth; the applications row mirrors it and
    holds the notes, follow-up date, and the first time the job was applied for.
    """
    if status is not None and status not in STATUSES:
        raise RuntimeError(f"Unknown status {status!r}; choose from {', '.join(STATUSES)}")
    row = conn.execute("SELECT status FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not row:
        raise RuntimeError(f"No job with database id {job_id}")
    if status is not None and status != row[0]:
        conn.execute("UPDATE jobs SET status=%s, status_updated_at=now() WHERE id=%s",
                     (status, job_id))
        conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                        VALUES (%s,%s,%s,%s)""", (job_id, row[0], status, via))
    note_line = f"{date.today().isoformat()}: {note.strip()}" if note and note.strip() else None
    set_follow_up = follow_up_on is not _UNSET
    if status is None and not note_line and not set_follow_up:
        return
    conn.execute(
        """INSERT INTO applications(job_id, status, notes, applied_at, follow_up_on, updated_at)
           SELECT id, status, %s, CASE WHEN status = ANY(%s) THEN now() END, %s, now()
           FROM jobs WHERE id=%s
           ON CONFLICT (job_id) DO UPDATE SET status=EXCLUDED.status,
               notes=nullif(concat_ws(chr(10), applications.notes, EXCLUDED.notes), ''),
               applied_at=coalesce(applications.applied_at, EXCLUDED.applied_at),
               follow_up_on=CASE WHEN %s THEN EXCLUDED.follow_up_on
                                 ELSE applications.follow_up_on END,
               updated_at=now()""",
        (note_line, APPLIED_STATUSES, follow_up_on if set_follow_up else None, job_id,
         set_follow_up))


def job_status(job_id: int, status: str | None = None, note: str | None = None) -> dict:
    """Show a job's application status, optionally setting it and appending a note."""
    with connect() as conn:
        set_job_state(conn, job_id, status=status, note=note, via="cli")
        row = conn.execute(
            """SELECT j.id, j.title, j.company, j.status, j.status_updated_at,
                      a.applied_at, a.notes, j.filter_reason
               FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
               WHERE j.id=%s""", (job_id,)).fetchone()
    keys = ("id", "title", "company", "status", "status_updated_at", "applied_at", "notes",
            "filter_reason")
    return dict(zip(keys, row))
