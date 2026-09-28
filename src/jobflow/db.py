from __future__ import annotations

import os
import re
from datetime import date
from importlib import resources

import psycopg

STATUSES = ("new", "interested", "not_interested", "applied", "interviewing", "offered",
            "accepted", "declined", "rejected", "withdrawn", "filtered")
# Finished with, so no follow-up is due. "filtered" is set on import, or by
# update-searches, when a job fails a search's local filters (see jobs.filter_reason).
CLOSED_STATUSES = ["not_interested", "accepted", "declined", "rejected", "withdrawn", "filtered"]
# Statuses meaning you've applied; the first time a job reaches one sets applied_at.
APPLIED_STATUSES = ["applied", "interviewing", "offered", "accepted", "declined"]


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(url)


def migration_files() -> list[tuple[str, str]]:
    """Return (file-scoped migration key, SQL) pairs in file and local-ID order."""
    folder = resources.files("jobflow") / "migrations"
    migrations = []
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        sql = entry.read_text()
        headers = list(re.finditer(r"^-- Migration ID: (\d{2})\s*$", sql, re.MULTILINE))
        if not headers:
            raise RuntimeError(f"Migration file {entry.name} is missing a '-- Migration ID: 01' header")
        ids = [header.group(1) for header in headers]
        expected = [f"{number:02d}" for number in range(1, len(ids) + 1)]
        if ids != expected:
            raise RuntimeError(f"Migration IDs in {entry.name} must start at 01 and increase by one")
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(sql)
            statement = sql[header.end():end].strip()
            if not statement:
                raise RuntimeError(f"Migration {entry.name}:{ids[index]} has no SQL")
            migrations.append((f"{entry.name}:{ids[index]}", statement))
    return sorted(migrations)


def migrate() -> None:
    from .ingest.store import backfill_countries, backfill_sources

    with connect() as conn:
        # Two commands starting at once (the daily import and yours) would otherwise both
        # apply a new migration; the second waits here until the first has committed.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('jobflow migrate'))")
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
        if "008_job_sources.sql:01" in newly_applied:
            added = backfill_sources(conn)
            if added:
                print(f"Recorded {added} more site copies in job_sources")
        if "002_jobs.sql:02" in newly_applied:
            filled = backfill_countries(conn)
            if filled:
                print(f"Recorded the country of {filled} jobs")


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
