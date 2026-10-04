"""Doing one piece of LLM work per job, a few jobs at a time: tailor-cv, cover-letter,
and interview-prep in the terminal (for_jobs), and scoring in the tracker (run_each)."""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TextIO

from .llm import LLMError
from .text import plural

# Jobs worked on at once. More would be faster, but each job makes several LLM calls
# sending whole CVs and ads, and providers cap tokens per minute (rate-limited calls are
# retried; see llm.complete).
JOBS_AT_ONCE = 2
# A run in a terminal with more jobs than this to do asks before it starts, as each makes
# several LLM calls (and web searches); one naming its jobs with --job doesn't.
CONFIRM_ABOVE = 5


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


@dataclass(frozen=True, kw_only=True)
class JobCommand:
    """One piece of work done per job: tailor-cv, cover-letter, or interview-prep.

    command: its name, for messages; status: the jobs it works on; job_statuses: the ones
    --job may also name (status if empty); kind: its workflow_runs kind; what: what it makes,
    for messages ("a tailored CV"); run_one(job_id): does one job; output_exists(folder):
    whether its finished output is still in the job's folder; fixed_by_hand(folder), if given:
    whether you fixed a draft that failed the fact check; done_one(folder), if given: runs
    for each job already done and returns a line to print, or None."""
    command: str
    status: str
    kind: str
    what: str
    run_one: Callable[[int], None]
    output_exists: Callable[[Path], bool]
    fixed_by_hand: Callable[[Path], bool] | None = None
    done_one: Callable[[Path], str | None] | None = None
    job_statuses: tuple[str, ...] = ()


def for_jobs(command: JobCommand, only: list[int] | None = None, redo: bool = False) -> None:
    """_run_jobs, unless the same command is already running (in another terminal, say),
    which would draft the same jobs twice and write over its files.

    A Postgres advisory lock, held on its own connection, marks the command as running;
    Postgres releases it when that connection closes, so even a killed run leaves none.
    (It needs a direct database connection: a pooler in transaction mode would lose it.)"""
    from .db import connect

    with connect() as lock:
        lock.autocommit = True
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                            (f"jobflow {command.kind}",)).fetchone()[0]:
            raise SystemExit(f"{command.command} is already running (in another "
                             "terminal or window). Let it finish, then run it again for any "
                             "jobs it didn't do.")
        _run_jobs(command, only, redo)


class Plan(NamedTuple):
    """What _plan_jobs found: how many jobs have the command's status, the (id, company,
    title) of those to do, the (name, folder) of those done, the (id, name, folder) of
    those whose failed draft you fixed by hand (done too, once recorded), and how many of
    those to do are drafted again (their last draft failed the fact check) or done again
    (their output was deleted)."""
    total: int
    todo: list[tuple[int, str | None, str]]
    done: list[tuple[str, Path]]
    fixed: list[tuple[int, str, Path]]
    redrafted: int
    lost: int


def _plan_jobs(command: JobCommand, only: list[int] | None = None, redo: bool = False) -> Plan:
    """The jobs with the command's status (or, given `only`, those of them, which may have
    any of command.job_statuses) still to do, and those done. A job in `only` without one
    of those statuses is an error. With redo, every
    job is to do, done or not. Reads only: _run_jobs records what it found.

    A run is recorded in workflow_runs only once it finishes, so a job is never done
    twice, and a failed job is retried next time. A job counts as done only while
    command.output_exists(its folder) too, which checks the command's own marker files (a
    CV_*_draft.yaml with its CV_REVIEW.md, cover-letter.docx, or interview-prep.html): one
    whose marker was deleted (by clean-up, say) is done again. Deleting only a tailored CV's
    PDF doesn't count. A job whose latest draft failed the fact check, or couldn't be
    rendered (NEEDS-FIX), is done again, for a fresh draft, unless
    command.fixed_by_hand(its folder) says you fixed that draft yourself: then it's kept
    (Plan.fixed)."""
    from .candidate.paths import job_directory
    from .db import connect

    statuses = list(command.job_statuses or (command.status,)) if only else [command.status]
    kind = command.kind
    with connect() as conn:
        if only:
            found = dict(conn.execute("SELECT id, status FROM jobs WHERE id = ANY(%s)",
                                      (only,)).fetchall())
            wrong = [f"#{job_id} is {found[job_id].replace('_', ' ')}" if job_id in found
                     else f"#{job_id} doesn't exist" for job_id in only
                     if found.get(job_id) not in statuses]
            if wrong:
                named = ", ".join(s.replace("_", " ") for s in statuses)
                raise RuntimeError(f"{'; '.join(wrong)}: this command works on {named} jobs "
                                   "(the job panel shows each job's number)")
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
               WHERE j.status = ANY(%s) AND (%s::bigint[] IS NULL OR j.id = ANY(%s))
               ORDER BY j.id""", (kind, statuses, only, only)).fetchall()
        todo, done_jobs, fixed, redrafted, lost = [], [], [], 0, 0
        for job_id, company, title, done, tried in rows:
            name = f"#{job_id} {title} ({company or 'unknown employer'})"
            folder = job_directory(job_id, {"company": company, "title": title})
            if redo:
                todo.append((job_id, company, title))
                continue
            if done and command.output_exists(folder):
                done_jobs.append((name, folder))
                continue
            elif done:
                lost += 1
            elif tried and command.fixed_by_hand and command.fixed_by_hand(folder):
                fixed.append((job_id, name, folder))
                continue
            elif tried:
                redrafted += 1
            todo.append((job_id, company, title))
    return Plan(len(rows), todo, done_jobs, fixed, redrafted, lost)


def _in_terminal() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _confirmed(command: JobCommand, count: int) -> bool:
    """Whether you answer yes to starting the command on `count` jobs."""
    try:
        answer = input(f"Start {command.command} on {count} jobs? Each makes several LLM calls "
                       "(and web searches, if set up). (Y/N) ")
    except EOFError:
        answer = ""
    return answer.strip().casefold() in ("y", "yes")


def _run_jobs(command: JobCommand, only: list[int] | None = None, redo: bool = False) -> None:
    """Run command.run_one(job_id) for each job not yet done (see _plan_jobs), JOBS_AT_ONCE
    at a time, printing each job's output in one piece when it finishes, and
    command.done_one for each job already done. One failure doesn't stop the rest, unless
    the LLM provider refuses every request (no credit left, a bad key). With more than
    CONFIRM_ABOVE jobs to do, a run in a terminal without --job asks first."""
    plan = _plan_jobs(command, only, redo)
    todo = plan.todo
    again = [f"{plan.redrafted} drafted again: their last draft failed the fact check or "
             "couldn't be rendered"] if plan.redrafted else []
    again += [f"{plan.lost} done again: their output was deleted"] if plan.lost else []
    jobs = (f"{plural(plan.total, 'job')} named" if only
            else plural(plan.total, command.status.capitalize() + " job"))
    print(f"{jobs}: "
          f"{plan.total - len(todo)} already have {command.what}, {len(todo)} to do"
          + (f" ({'; '.join(again)})" if again else ""))
    if not only and len(todo) > CONFIRM_ABOVE and _in_terminal() \
            and not _confirmed(command, len(todo)):
        print("Nothing done")
        return
    from .db import connect, record_workflow_run

    if plan.fixed:
        with connect() as conn:   # so the next run doesn't redraft them either
            for job_id, _, _ in plan.fixed:
                record_workflow_run(conn, job_id, command.kind, "FIXED-BY-HAND")
    for _, name, _ in plan.fixed:
        print(f"{name}: you fixed its draft yourself, so it's kept and not drafted again")
    done_one = command.done_one
    if done_one is not None:
        for name, folder in plan.done + [(name, folder) for _, name, folder in plan.fixed]:
            try:
                line = done_one(folder)
            except Exception as exc:   # one job's files (edited by hand, say) never stop the rest
                print(f"{name}: warning: {exc}", file=sys.stderr)
            else:
                if line:
                    print(f"{name}: {line}")
    out, err = _JobOutput(sys.stdout), _JobOutput(sys.stderr)
    lock = threading.Lock()

    def one(row) -> None:
        job_id, company, title = row
        lines: list[tuple[TextIO, str]] = []
        out.held.lines = err.held.lines = lines
        print(f"\n#{job_id} {title} ({company or 'unknown employer'})")
        try:
            command.run_one(job_id)
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
