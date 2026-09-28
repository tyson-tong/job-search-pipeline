"""Scoring jobs against your CVs from the web app, in a background thread.

Scoring calls the LLM once per job per CV, so a batch can take minutes; the page
starts a run and polls its progress. One run at a time, scoring batch.JOBS_AT_ONCE jobs
at once (each job's CVs are also assessed at once).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from ..batch import run_each
from ..candidate.cvs import chosen_sources
from ..candidate.matching import jobs_needing_match, match_job
from ..llm import credentials

MAX_JOBS = 500
_lock = threading.Lock()
_state: dict = {"running": False, "total": 0, "done": 0, "current": None, "errors": [],
                "scored": [], "started_at": None, "finished_at": None}


class Busy(RuntimeError):
    pass


def status() -> dict:
    with _lock:
        return {**_state, "errors": list(_state["errors"]), "scored": list(_state["scored"])}


def start(job_ids: list[int] | None = None) -> dict:
    """Score the given jobs, or every Interested job not yet scored. Only Interested jobs
    that have never been scored are ever scored. Checks the LLM settings and CV files
    first, so a configuration problem fails now rather than on every job."""
    credentials()
    chosen_sources()
    with _lock:
        if _state["running"]:
            raise Busy("Scoring is already running")
        ids = jobs_needing_match(job_ids)
        if job_ids and not ids:
            raise ValueError("Only interested jobs that haven't been scored can be scored")
        _state.update(running=bool(ids), total=len(ids), done=0, current=None, errors=[],
                      scored=[], started_at=datetime.now(timezone.utc), finished_at=None)
    if ids:
        threading.Thread(target=_run, args=(ids,), daemon=True).start()
    else:
        with _lock:
            _state["finished_at"] = datetime.now(timezone.utc)
    return status()


def _run(ids: list[int]) -> None:
    def score(job_id: int) -> None:
        with _lock:
            _state["current"] = job_id
        try:
            match_job(job_id)
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
    # many were left.
    run_each(ids, score)
    with _lock:
        _state.update(running=False, current=None, finished_at=datetime.now(timezone.utc))


def parse_request(payload: object) -> list[int] | None:
    if not isinstance(payload, dict):
        raise ValueError("Send an object")
    job_ids = payload.get("job_ids")
    if job_ids is not None and (
            not isinstance(job_ids, list) or not job_ids or len(job_ids) > MAX_JOBS
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in job_ids)):
        raise ValueError(f"job_ids must be a list of 1 to {MAX_JOBS} job ids")
    return job_ids
