"""Doing one piece of LLM work per job, a few jobs at a time: tailor-cv, cover-letter,
and interview-prep in the terminal (for_jobs), and scoring in the tracker (run_each)."""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .llm import LLMError

# Jobs worked on at once. More would be faster, but each job makes several LLM calls
# sending whole CVs and ads, and providers cap tokens per minute (rate-limited calls are
# retried; see llm.complete).
JOBS_AT_ONCE = 2
# The command each workflow_runs kind is run by, for messages.
COMMANDS = {"tailor": "tailor-cv"}


def run_each(items: list, work, workers: int = JOBS_AT_ONCE) -> tuple[list, list]:
    """Call work(item) for each item, `workers` at a time; work reports its own errors,
    then raises. One failure doesn't stop the rest, unless it's an LLMError the provider
    will repeat for every request (no credit left, a bad key): then the items not yet
    started are skipped. Returns (the items that failed, the items skipped)."""
    stop, lock = threading.Event(), threading.Lock()
    failed: list = []
    skipped: list = []

    def one(item) -> None:
        if stop.is_set():
            with lock:
                skipped.append(item)
            return
        try:
            work(item)
        except Exception as exc:
            with lock:
                failed.append(item)
            if isinstance(exc, LLMError) and exc.fatal:
                stop.set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, items))
    return failed, skipped


class _JobOutput:
    """Stands in for sys.stdout and sys.stderr so that what a job prints, from anywhere,
    is held back and printed in one piece when it finishes, not interleaved with the
    other job running at the same time."""

    def __init__(self, stream):
        self.stream, self.held = stream, threading.local()

    def write(self, text: str) -> int:
        held = getattr(self.held, "lines", None)
        if held is None:
            return self.stream.write(text)
        held.append((self.stream, text))
        return len(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def for_jobs(status: str, kind: str, what: str, run_one, output_exists,
             fixed_by_hand=None, done_one=None) -> None:
    """_run_jobs, unless the same command is already running (in another terminal, say),
    which would draft the same jobs twice and write over its files.

    A Postgres advisory lock, held on its own connection, marks the command as running;
    Postgres releases it when that connection closes, so even a killed run leaves none.
    (It needs a direct database connection: a pooler in transaction mode would lose it.)"""
    from .db import connect

    with connect() as lock:
        lock.autocommit = True
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                            (f"jobflow {kind}",)).fetchone()[0]:
            raise SystemExit(f"{COMMANDS.get(kind, kind)} is already running (in another "
                             "terminal or window). Let it finish, then run it again for any "
                             "jobs it didn't do.")
        _run_jobs(status, kind, what, run_one, output_exists, fixed_by_hand, done_one)


def _plan_jobs(status: str, kind: str, output_exists, fixed_by_hand=None):
    """The jobs with `status` still to do, and those done: (all jobs, to do, done as
    (name, folder) pairs, how many are redrafted, how many are done again).

    A run is recorded in workflow_runs only once it finishes, so a job is never done
    twice, and a failed job is retried next time. A job counts as done only while
    output_exists(its folder) too, which checks the command's own marker file (a
    CV_*_draft.yaml, cover-letter.docx, or interview-prep.html): one whose marker was
    deleted (by clean-up, say) is done again. Deleting only a tailored CV's PDF doesn't
    count. A job whose latest draft failed the fact check (NEEDS-FIX) is done again, for a
    fresh draft, unless fixed_by_hand(its folder) says you fixed that draft yourself: then
    it's recorded as FIXED-BY-HAND and kept."""
    from .candidate.paths import job_directory
    from .db import connect

    with connect() as conn:
        # Done or not by the job's latest run: a draft that failed the fact check after an
        # earlier one passed (its output deleted in between) is drafted again too.
        rows = conn.execute(
            """SELECT j.id, j.company, j.title,
                      last.status IS NOT NULL AND last.status <> 'NEEDS-FIX',
                      last.status IS NOT NULL
               FROM jobs j
               LEFT JOIN LATERAL (SELECT r.status FROM workflow_runs r
                                  WHERE r.job_id = j.id AND r.kind = %s
                                  ORDER BY r.id DESC LIMIT 1) last ON true
               WHERE j.status = %s ORDER BY j.id""", (kind, status)).fetchall()
        todo, done_jobs, redo, lost = [], [], 0, 0
        for job_id, company, title, done, tried in rows:
            name = f"#{job_id} {title} ({company or 'unknown employer'})"
            folder = job_directory(job_id, {"company": company, "title": title})
            if done and output_exists(folder):
                done_jobs.append((name, folder))
                continue
            if done:
                lost += 1
            elif tried and fixed_by_hand and fixed_by_hand(folder):
                conn.execute("INSERT INTO workflow_runs(job_id,kind,status,finished_at) "
                             "VALUES (%s,%s,'FIXED-BY-HAND',now())", (job_id, kind))
                print(f"{name}: you fixed its draft yourself, so it's kept and not drafted again")
                done_jobs.append((name, folder))
                continue
            elif tried:
                redo += 1
            todo.append((job_id, company, title))
    return rows, todo, done_jobs, redo, lost


def _run_jobs(status: str, kind: str, what: str, run_one, output_exists,
              fixed_by_hand=None, done_one=None) -> None:
    """Run run_one(job_id) for each job with `status` not yet done (see _plan_jobs),
    JOBS_AT_ONCE at a time, printing each job's output in one piece when it finishes.
    done_one(folder), if given, runs for each job already done and returns a line to
    print, or None. One failure doesn't stop the rest, unless the LLM provider refuses
    every request (no credit left, a bad key)."""
    rows, todo, done_jobs, redo, lost = _plan_jobs(status, kind, output_exists, fixed_by_hand)
    again = [f"{redo} drafted again: the last draft failed the fact check"] if redo else []
    again += [f"{lost} done again: their output was deleted"] if lost else []
    print(f"{len(rows)} {status.capitalize()} job{'s' if len(rows) != 1 else ''}: "
          f"{len(rows) - len(todo)} already have {what}, {len(todo)} to do"
          + (f" ({'; '.join(again)})" if again else ""))
    for name, folder in done_jobs if done_one else []:
        try:
            line = done_one(folder)
        except RuntimeError as exc:
            print(f"{name}: warning: {exc}", file=sys.stderr)
        else:
            if line:
                print(f"{name}: {line}")
    out, err = _JobOutput(sys.stdout), _JobOutput(sys.stderr)
    lock = threading.Lock()

    def one(row) -> None:
        job_id, company, title = row
        out.held.lines = err.held.lines = lines = []
        print(f"\n#{job_id} {title} ({company or 'unknown employer'})")
        try:
            run_one(job_id)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise
        finally:
            out.held.lines = err.held.lines = None
            with lock:
                for stream, text in lines:  # flushed, so stdout and stderr stay in order
                    stream.write(text)
                    stream.flush()

    sys.stdout.flush()
    sys.stdout, sys.stderr = out, err
    try:
        failed, skipped = run_each(todo, one)
    finally:
        sys.stdout, sys.stderr = out.stream, err.stream
    if skipped:
        raise SystemExit(f"Stopped: the LLM provider will refuse the other {len(skipped)} "
                         "job(s) too. Fix that, then run the command again")
    if failed:
        raise SystemExit(f"{len(failed)} of {len(todo)} failed; run the command again to retry them")
