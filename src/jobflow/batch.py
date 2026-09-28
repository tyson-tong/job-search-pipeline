"""Doing one piece of LLM work per job, a few jobs at a time: tailor-cv, cover-letter,
and interview-prep in the terminal, and scoring in the tracker."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .llm import LLMError

# Jobs worked on at once. More would be faster, but each job makes several LLM calls
# sending whole CVs and ads, and providers cap tokens per minute (rate-limited calls are
# retried; see llm.complete).
JOBS_AT_ONCE = 2


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
