"""Scoring jobs against your CVs from the web app, in a background thread.

Scoring calls the LLM once per job per CV, so a batch can take minutes; the page
starts a run and polls its progress. One run at a time.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from ..candidate.cvs import cv_sources
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


def start(job_ids: list[int] | None = None, force: bool = False, limit: int = 50) -> dict:
    """Score the given jobs, or up to `limit` unscored open jobs. Checks the LLM settings
    and CV files first, so a configuration problem fails now rather than on every job."""
    credentials()
    if not cv_sources():
        raise RuntimeError("No CV_*.yaml files found; check cv_directory in profile.toml")
    with _lock:
        if _state["running"]:
            raise Busy("Scoring is already running")
        ids = list(job_ids) if job_ids else jobs_needing_match(limit)
        _state.update(running=bool(ids), total=len(ids), done=0, current=None, errors=[],
                      scored=[], started_at=datetime.now(timezone.utc), finished_at=None)
    if ids:
        threading.Thread(target=_run, args=(ids, force), daemon=True).start()
    else:
        with _lock:
            _state["finished_at"] = datetime.now(timezone.utc)
    return status()


def _run(ids: list[int], force: bool) -> None:
    for job_id in ids:
        with _lock:
            _state["current"] = job_id
        try:
            match_job(job_id, force=force)
            with _lock:
                _state["scored"].append(job_id)
        except Exception as exc:  # one failure shouldn't stop the batch
            with _lock:
                _state["errors"].append({"id": job_id, "error": str(exc)[:300]})
        with _lock:
            _state["done"] += 1
    with _lock:
        _state.update(running=False, current=None, finished_at=datetime.now(timezone.utc))


def parse_request(payload: object) -> tuple[list[int] | None, bool, int]:
    if not isinstance(payload, dict):
        raise ValueError("Send an object")
    job_ids = payload.get("job_ids")
    if job_ids is not None and (
            not isinstance(job_ids, list) or not job_ids or len(job_ids) > MAX_JOBS
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in job_ids)):
        raise ValueError(f"job_ids must be a list of 1 to {MAX_JOBS} job ids")
    limit = payload.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_JOBS:
        raise ValueError(f"limit must be between 1 and {MAX_JOBS}")
    return job_ids, payload.get("force") is True, limit
