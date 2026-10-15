"""Scoring jobs against your CVs from the web app, in a background thread.

Scoring calls the LLM once per job per CV, so a batch can take minutes; the page
starts a run and polls its progress. One run at a time, scoring batch.JOBS_AT_ONCE jobs
at once (each job's CVs are also assessed at once).
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from ..batch import run_each
from ..candidate.cvs import chosen_sources
from ..candidate.matching import current_digests, jobs_needing_match, match_job, outdated_scores
from ..llm import credentials
from .queries import MAX_ROWS

_lock = threading.Lock()
_state: dict = {"running": False, "total": 0, "done": 0, "errors": [],
                "scored": [], "started_at": None, "finished_at": None}


class Busy(RuntimeError):
    pass


def status() -> dict:
    with _lock:
        return {**_state, "errors": list(_state["errors"]), "scored": list(_state["scored"])}


def start(job_ids: list[int] | None = None, rescore: bool = False) -> dict:
    """Score the given jobs (Inbox ones, or ones you're pursuing), or every job you're
    pursuing (matching.PURSUING), against each CV it has no score for yet; with rescore,
    also against each CV edited since the job was scored against it. Checks the LLM
    settings and CV files first, so a configuration problem fails now rather than on every
    job."""
    credentials()
    variants = list(chosen_sources())
    with _lock:   # claimed before the database is read, so a second start is Busy at once
        if _state["running"]:
            raise Busy("Scoring is already running")
        _state.update(running=True, total=0, done=0, errors=[], scored=[],
                      started_at=datetime.now(UTC), finished_at=None)
    try:
        ids = jobs_needing_match(variants, job_ids)
        if rescore:
            outdated = [job_id for job_id, _ in outdated_scores(current_digests(), job_ids)]
            ids = list(dict.fromkeys(outdated + ids))
        if job_ids and not ids:
            raise ValueError("Only Inbox jobs and jobs you're pursuing with a CV they have no "
                             "score against (or, to rescore, a CV edited since) can be scored")
    except BaseException:
        with _lock:
            _state.update(running=False, finished_at=datetime.now(UTC))
        raise
    with _lock:
        _state.update(running=bool(ids), total=len(ids))
        if not ids:
            _state["finished_at"] = datetime.now(UTC)
    if ids:
        threading.Thread(target=_run, args=(ids, rescore), daemon=True).start()
    return status()


def _run(ids: list[int], rescore: bool = False) -> None:
    def score(job_id: int) -> None:
        try:
            match_job(job_id, rescore=rescore)
            with _lock:
                _state["scored"].append(job_id)
        except Exception as exc:
            with _lock:
                _state["errors"].append({"id": job_id, "error": str(exc)[:300]})
            raise
        finally:
            with _lock:
                _state["done"] += 1

    # Jobs skipped after a fatal LLM error aren't counted as done, so the page says how
    # many were left. Finished whatever happens, or every later start would be Busy.
    try:
        run_each(ids, score)
    finally:
        with _lock:
            _state.update(running=False, finished_at=datetime.now(UTC))


def parse_request(payload: object) -> tuple[list[int] | None, bool]:
    """(job_ids, rescore) from a request to start scoring."""
    if not isinstance(payload, dict):
        raise ValueError("Send an object")
    job_ids, rescore = payload.get("job_ids"), payload.get("rescore", False)
    # At most as many as the job list can show, all of which you can tick.
    if job_ids is not None and (
            not isinstance(job_ids, list) or not job_ids or len(job_ids) > MAX_ROWS
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in job_ids)):
        raise ValueError(f"job_ids must be a list of 1 to {MAX_ROWS} job ids")
    if not isinstance(rescore, bool):
        raise ValueError("rescore must be true or false")
    return job_ids, rescore
