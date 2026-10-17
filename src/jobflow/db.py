"""The database: connections (encrypted unless on this machine, in your timezone),
migrations, loading a job, and changing a job's status, which every status change goes
through so status_history stays complete."""
from __future__ import annotations

import os
import re
import threading
from importlib import resources

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import LOCAL_HOSTS, timezone, today
from .text import plural

# Every status a job can have. The jobs_status_check constraint (migrations/002_jobs.sql)
# lists them too: a new one needs a migration changing it.
STATUSES = ("new", "interested", "not_interested", "applied", "interviewing", "offered",
            "accepted", "declined", "rejected", "withdrawn", "filtered", "archived")
# The groups of statuses the rules use, in one place; the tracker gets the ones it needs from
# /api/summary (web/queries.summary).
# Statuses you can choose; "filtered" and "archived" are only set by jobflow itself.
EDITABLE_STATUSES = tuple(s for s in STATUSES if s not in ("filtered", "archived"))
# The jobs you're pursuing: scored by Score all jobs (and rescored, after a CV is edited),
# worked on by tailor-cv and cover-letter, and counted in skill-gaps.
PURSUING = ("interested", "applied", "interviewing", "offered")
# Every status a job can be scored in: those, and the Inbox's, which you can pick to score
# to help you triage.
SCORABLE = ("new", *PURSUING)
# Jobs not (or no longer) picked as Interested: never shown in My jobs, and only triaged
# there, to TRIAGE_CHOICES, or moved back to the Inbox.
UNTRIAGED = ("new", "not_interested", "filtered", "archived")
TRIAGE_CHOICES = ("new", "interested", "not_interested")
# My jobs' statuses until you choose others: those you're pursuing, and accepted.
DEFAULT_MY_JOBS = (*PURSUING, "accepted")
# A similar job at one of these may be a role you've already applied for.
IN_PROGRESS = ("applied", "interviewing", "offered", "accepted")
# The jobs whose output folders clean-up deletes, if you never applied for them.
CLEAN_UP_STATUSES = ("not_interested", "withdrawn", "archived")
# Finished with, so no follow-up is due. "filtered" is set on import, or by
# update-searches, when a job fails a search's local filters (see jobs.filter_reason);
# "archived" when a filtered, not interested, or untriaged Inbox job is old enough (see
# archive_old_jobs in ingest/upkeep.py).
CLOSED_STATUSES = ("not_interested", "accepted", "declined", "rejected", "withdrawn", "filtered",
                   "archived")
# Statuses meaning you've applied; the first time a job reaches one sets applied_at. Rejected
# too: a job can go straight from Interested to Rejected if you never marked it Applied.
APPLIED_STATUSES = ("applied", "interviewing", "offered", "accepted", "declined", "rejected")
# Each group is a tuple, so none can be changed by accident; a query takes one as list(...),
# which psycopg sends as an array for = ANY(%s), as it doesn't a tuple.

# The sslmode values that never fall back to an unencrypted connection.
ENCRYPTED_SSLMODES = ("require", "verify-ca", "verify-full")


def encrypted_url(url: str) -> str:
    """DATABASE_URL, made to require encryption for a database on another machine: the
    connection carries its password and your job data. With no sslmode, psycopg's default
    ("prefer") quietly connects unencrypted when the server (or anyone in between) allows
    it; so a missing sslmode becomes require, and a weaker one is refused."""
    params = conninfo_to_dict(url)
    hosts = str(params.get("host") or os.environ.get("PGHOST") or "").split(",")
    # libpq connects over TCP to a hostaddr whatever the host says, so one counts too.
    addrs = str(params.get("hostaddr") or os.environ.get("PGHOSTADDR") or "").split(",")
    # No host, or a folder, is a Unix socket on this machine.
    if (all(not host or host.startswith("/") or host in LOCAL_HOSTS for host in hosts)
            and all(not addr or addr in LOCAL_HOSTS for addr in addrs)):
        return url
    mode = params.get("sslmode") or os.environ.get("PGSSLMODE")
    if mode is None:
        return make_conninfo(url, sslmode="require")
    if mode not in ENCRYPTED_SSLMODES:
        raise RuntimeError(f"DATABASE_URL has sslmode={mode}, which can connect unencrypted; "
                           "use sslmode=require (or verify-full) for a database on another machine")
    return url


# The tracker's pool of open connections (see use_pool), and the connection string and
# timezone it was opened for: opening a connection to a database on another continent costs
# a TLS handshake every time.
_pool: ConnectionPool | None = None
_pool_key: tuple[str, str] | None = None
_pooling = False
_pool_lock = threading.Lock()


def use_pool() -> None:
    """Reuse connections from now on, for a long-running process: the tracker."""
    global _pooling
    _pooling = True


def _set_timezone(conn, zone: str) -> None:
    conn.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
    conn.commit()


def connect():
    """A connection (used as `with connect() as conn:`, which commits on success) in
    settings.timezone(), so the dates the database works out (a timestamp's day) and the
    timestamps it returns match the rest of jobflow's; encrypted unless the database is on
    this machine (encrypted_url). From the pool, after use_pool()."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    conninfo, zone = encrypted_url(url), timezone().key
    if not _pooling:
        conn = psycopg.connect(conninfo)
        _set_timezone(conn, zone)
        return conn
    global _pool, _pool_key
    with _pool_lock:   # a new pool if the timezone or DATABASE_URL changed
        if _pool_key != (conninfo, zone):
            old = _pool
            # The timezone is set once per connection the pool opens, and each is checked
            # before use: Neon closes idle connections when it suspends.
            _pool = ConnectionPool(conninfo, min_size=1, max_size=8, open=True,
                                   configure=lambda conn: _set_timezone(conn, zone),
                                   check=ConnectionPool.check_connection)
            _pool_key = (conninfo, zone)
            if old is not None:
                old.close()   # its idle connections now; any in use, when they're returned
        pool = _pool
    return pool.connection()


def migration_files() -> list[tuple[str, str, str | None]]:
    """Return (file-scoped migration key, SQL, follow-up) triples in file and local-ID order.
    A block's follow-up is the function in ingest/backfill.py its "-- Then: NAME" comment
    names, run once after the block is first applied (to fill in a new table or column),
    or None."""
    folder = resources.files("jobflow") / "migrations"
    migrations = []
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        sql = entry.read_text()
        headers = list(re.finditer(r"^-- Migration ID: (\d{2})\s*$", sql, re.MULTILINE))
        if not headers:
            raise RuntimeError(f"Migration file {entry.name} is missing a "
                               "'-- Migration ID: 01' header")
        ids = [header.group(1) for header in headers]
        expected = [f"{number:02d}" for number in range(1, len(ids) + 1)]
        if ids != expected:
            raise RuntimeError(f"Migration IDs in {entry.name} must start at 01 and increase "
                               "by one")
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(sql)
            statement = sql[header.end():end].strip()
            if not statement:
                raise RuntimeError(f"Migration {entry.name}:{ids[index]} has no SQL")
            then = re.search(r"^-- Then: (\w+)\s*$", statement, re.MULTILINE)
            migrations.append((f"{entry.name}:{ids[index]}", statement, then and then[1]))
    return sorted(migrations)


def pending_migrations() -> list[str]:
    """The migrations not yet applied to the database, without applying them."""
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
        applied = ({row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
                   if exists else set())
    return [name for name, _, _ in migration_files() if name not in applied]


def migrate() -> None:
    from .ingest.backfill import FOLLOW_UPS

    done: list[str] = []   # printed once committed, so a failure part-way never claims any
    with connect() as conn:
        # Two commands starting at once (the daily import and yours) would otherwise both
        # apply a new migration; the second waits here until the first has committed.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('jobflow migrate'))")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            name TEXT PRIMARY KEY,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        follow_ups = []
        for name, sql, then in migration_files():
            if name in applied:
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
            done.append(f"Applied migration {name}")
            follow_ups += [then] if then else []
        # Once every table exists: a follow-up may read tables created by later files.
        for then in follow_ups:
            filled = FOLLOW_UPS[then](conn)
            if filled:
                done.append(f"{then}: filled in {plural(filled, 'row')}")
    for line in done:
        print(line)


def fetch_job(job_id: int) -> dict:
    """The job, for LLM work on it (scoring, tailor-cv, cover-letter, interview-prep). One
    whose ad was cleared, after it was archived, has nothing to work from, so it's refused
    before anything is spent on it."""
    with connect() as conn:
        job = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, canonical_url AS url, title, company, location, description, raw_json "
            "FROM jobs WHERE id=%s", (job_id,)).fetchone()
    if not job:
        raise RuntimeError(f"No job with database id {job_id}")
    if not (job["description"] or "").strip():
        raise RuntimeError(f"Job {job_id} has no ad (it was cleared after the job was "
                           "archived), so there's nothing to score it or write from")
    return job


def record_workflow_run(conn, job_id: int, kind: str, status: str,
                        model: str | None = None) -> None:
    """Log a finished piece of work on a job (match, tailor, cover-letter, interview-prep):
    batch.py reads the latest of each kind to tell which jobs are done."""
    conn.execute("INSERT INTO workflow_runs(job_id, kind, status, model_name, finished_at) "
                 "VALUES (%s, %s, %s, %s, now())", (job_id, kind, status, model))


_UNSET = object()


def set_job_state(conn, job_id: int, *, status: str | None = None, note: str | None = None,
                  follow_up_on: object = _UNSET, via: str) -> None:
    """Change a job's status, add a dated note, and/or set its follow-up date, within
    the caller's transaction. Status changes are recorded in status_history.

    jobs.status is the single source of truth; the applications row holds the notes,
    the follow-up date, and the first time the job was applied for.
    """
    if status is not None and status not in STATUSES:
        raise RuntimeError(f"Unknown status {status!r}; choose from {', '.join(STATUSES)}")
    row = conn.execute("SELECT status FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not row:
        raise RuntimeError(f"No job with database id {job_id}")
    if status is not None and status != row[0]:
        # A job you move out of Filtered no longer has a reason to be filtered.
        conn.execute("""UPDATE jobs SET status=%s, status_updated_at=now(),
                            filter_reason=CASE WHEN %s = 'filtered' THEN filter_reason END,
                            filter_kind=CASE WHEN %s = 'filtered' THEN filter_kind END
                        WHERE id=%s""", (status, status, status, job_id))
        conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                        VALUES (%s,%s,%s,%s)""", (job_id, row[0], status, via))
    note_line = f"{today().isoformat()}: {note.strip()}" if note and note.strip() else None
    set_follow_up = follow_up_on is not _UNSET
    if status is None and not note_line and not set_follow_up:
        return
    conn.execute(
        """INSERT INTO applications(job_id, notes, applied_at, follow_up_on, updated_at)
           SELECT id, %s, CASE WHEN status = ANY(%s) THEN now() END, %s, now()
           FROM jobs WHERE id=%s
           ON CONFLICT (job_id) DO UPDATE SET
               notes=nullif(concat_ws(chr(10), applications.notes, EXCLUDED.notes), ''),
               applied_at=coalesce(applications.applied_at, EXCLUDED.applied_at),
               follow_up_on=CASE WHEN %s THEN EXCLUDED.follow_up_on
                                 ELSE applications.follow_up_on END,
               updated_at=now()""",
        (note_line, list(APPLIED_STATUSES), follow_up_on if set_follow_up else None, job_id,
         set_follow_up))


def move_job(conn, job_id: int, from_status: str, to_status: str, via: str,
             filter_reason: str | None = None, filter_kind: str | None = None) -> bool:
    """Move a job jobflow itself is moving (update-searches filtering or restoring it) from
    one status to another, with filter_reason and filter_kind (which filter, see
    ingest/filters.py), recording it in status_history; False, and nothing changed, if the
    job has left from_status since it was read. set_job_state is for the changes you make."""
    moved = conn.execute("""UPDATE jobs SET status=%s, filter_reason=%s, filter_kind=%s,
                                status_updated_at=now()
                            WHERE id=%s AND status=%s""",
                         (to_status, filter_reason, filter_kind, job_id,
                          from_status)).rowcount
    if moved:
        conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                        VALUES (%s,%s,%s,%s)""", (job_id, from_status, to_status, via))
    return bool(moved)
