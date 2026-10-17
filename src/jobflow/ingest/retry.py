"""Importing again the results of runs that couldn't be saved: they're paid for, and kept in
the run's dataset on Apify until Apify deletes it. Every import does this before its own
runs (apify.ingest_searches), and jobflow ingest --retry-imports does only this."""
from __future__ import annotations

import sys
from dataclasses import replace

from .. import settings
from ..text import plural
from . import apify, store
from .filters import search_filter
from .options import POSTED_WITHIN_DAYS
from .runs import run_country, run_hit_cap
from .searches import Search, load_searches


def _search_as_run(record: store.RunRecord, configured: dict[str, Search]) -> Search | None:
    """The search that ran it, as it ran: with the posted-date window it used, which a
    one-off ingest --posted-within-days may have widened. None if the search was renamed or
    removed since."""
    search = configured.get(record.search_name or "")
    if search and record.posted_within_days in POSTED_WITHIN_DAYS:
        return replace(search, posted_within_days=record.posted_within_days)
    return search


def import_failed_runs(configured: list[Search],
                       include_given_up: bool = False) -> tuple[int, int]:
    """Import again the results of each run recorded as IMPORT_FAILED (and, with
    include_given_up, GAVE_UP). Each is saved as its import would have been: filtered by the
    search that ran it, with the window it ran with, as on the day it ran, so a posted-date
    window doesn't filter jobs that have merely aged since. A run whose dataset Apify has
    deleted is recorded as EXPIRED; one that has now failed store.MAX_IMPORT_ATTEMPTS times
    as GAVE_UP: neither is tried again by itself. Returns how many couldn't be imported, of
    how many tried."""
    from apify_client.errors import ApifyApiError

    runs = store.failed_imports(include_given_up)
    if not runs:
        return 0, 0
    print(f"Importing the results of {plural(len(runs), 'earlier run')} that couldn't be saved")
    searches = {search.name: search for search in configured}
    client = apify.make_client()
    failed = 0
    for record, attempts in runs:
        name = f"{record.search_name}/{record.site} run {record.run_id}"
        search, run_input = _search_as_run(record, searches), record.run_input or {}
        try:
            # failed_imports picks only runs with a dataset.
            items = list(client.dataset(record.dataset_id or "").iterate_items(clean=True))
            ran_on = (record.finished_at.astimezone(settings.timezone()).date()
                      if record.finished_at else None)
            added = store.save_run(
                record, items, country=run_country(record.site, run_input),
                hit_cap=bool(run_input) and run_hit_cap(run_input, items),
                job_filter=search_filter(search, record.site, ran_on) if search else None)
        except Exception as exc:
            failed += 1
            if isinstance(exc, ApifyApiError) and exc.status_code == 404:
                gone = "its results are no longer on Apify, so they can't be imported"
                store.record_failed_run(record, status="EXPIRED", error=gone)
                print(f"{name}: {gone}", file=sys.stderr)
                continue
            # Reading its results from Apify counts as importing them too: an error there
            # that repeats would otherwise fail every import until Apify deleted them.
            give_up = attempts + 1 >= store.MAX_IMPORT_ATTEMPTS
            store.record_failed_run(record, status=store.GAVE_UP if give_up
                                    else store.IMPORT_FAILED, error=str(exc))
            print(f"{name}: FAILED again — {exc}"
                  + (f"; it has now failed {store.MAX_IMPORT_ATTEMPTS} times, so imports no "
                     "longer try it: once you've fixed the cause, run `uv run jobflow ingest "
                     "--retry-imports`" if give_up else ""), file=sys.stderr)
            continue
        print(f"{name}: {len(items)} items, {added} new jobs")
        if not search:
            print(f"{name}: its search {record.search_name!r} is no longer in your searches, "
                  "so its jobs weren't filtered: run `uv run jobflow update-searches` to judge "
                  "them by [defaults]", file=sys.stderr)
    return failed, len(runs)


def retry_imports() -> None:
    """jobflow ingest --retry-imports: import_failed_runs alone, without new runs, and
    including the runs imports gave up on."""
    not_imported, total = import_failed_runs(load_searches(), include_given_up=True)
    if not total:
        print("No failed imports to retry")
    elif not_imported:
        raise RuntimeError(f"{not_imported} of {plural(total, 'failed import')} couldn't be "
                           "imported (see above)")
