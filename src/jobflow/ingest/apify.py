from __future__ import annotations

import math
import os
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from functools import partial

from apify_client import ApifyClient

from ..text import plural
from .filters import filter_reason
from .runs import PlannedRun, plan
from .searches import load_searches

# The longest APIFY_MAX_WAIT_MINUTES may be, and its default. Each site's runs go one after
# another in a daily GitHub job stopped at 180 minutes, where a run cut off keeps charging
# on Apify: a short wait leaves room for several runs per site.
MAX_WAIT_MINUTES = 30


def make_client() -> ApifyClient:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is required")
    return ApifyClient(token)


def run_search(client: ApifyClient, planned: PlannedRun, max_wait: timedelta) -> None:
    """Run one actor for one search, import its dataset, and record the run.

    Any failure is recorded in scraper_runs and re-raised: with Apify's status, or
    IMPORT_FAILED for a run that succeeded but whose results couldn't be saved.
    """
    from .store import record_failed_run, save_run

    actor_id = f"{planned.site}:{planned.actor}"
    name = f"{planned.search.name}/{planned.site} {planned.label}"
    cap = planned.search.max_cost_usd
    run = None
    try:
        # Apify stops the run itself at max_wait too, so it can't keep charging if this
        # process is killed first (the daily GitHub job is stopped at 180 minutes).
        run = client.actor(planned.actor).call(
            run_input=planned.run_input, wait_duration=max_wait, run_timeout=max_wait,
            logger=None, max_total_charge_usd=Decimal(str(cap)) if cap else None)
        if run is None:
            raise RuntimeError("Apify returned no run")
        if run.status != "SUCCEEDED":
            detail = f": {run.status_message}" if run.status_message else ""
            if run.status in ("READY", "RUNNING"):
                # Left alone it would keep scraping, and charging, for results never imported.
                try:
                    client.run(run.id).abort()
                    stopped = "stopped"
                except Exception as exc:
                    stopped = f"stopping it failed ({exc}); abort it in the Apify Console"
                detail += (f" (still running after {max_wait}, so {stopped}; results were not "
                           "imported)")
            raise RuntimeError(f"Apify run {run.id} ended with {run.status}{detail}")
        items = list(client.dataset(run.default_dataset_id).iterate_items(clean=True))
        job_filter = partial(filter_reason, search=planned.search, site=planned.site)
        # Sites run at the same time; save_run makes their imports take turns.
        added = save_run(actor_id, run.id, run.default_dataset_id, items,
                         started_at=run.started_at, finished_at=run.finished_at,
                         cost_usd=run.usage_total_usd, search_name=planned.search.name,
                         run_input=planned.run_input, job_filter=job_filter,
                         country=planned.country)
        cost = f", ${run.usage_total_usd:.2f}" if run.usage_total_usd is not None else ""
        print(f"{name}: {len(items)} items, {added} new jobs{cost}")
    except Exception as exc:
        # A run that succeeded on Apify failed here, importing its results; they stay in
        # its dataset on Apify.
        status = "FAILED" if run is None else "IMPORT_FAILED" if run.status == "SUCCEEDED" \
            else run.status
        try:
            record_failed_run(actor_id, run.id if run else None,
                              run.default_dataset_id if run else None, status, str(exc),
                              run.started_at if run else None,
                              run.finished_at if run else None,
                              run.usage_total_usd if run else None,
                              planned.search.name, planned.run_input)
        except Exception as record_exc:
            print(f"{name}: could not record failure: {record_exc}", file=sys.stderr)
        raise


def wait_minutes() -> float:
    """APIFY_MAX_WAIT_MINUTES, by default and at most MAX_WAIT_MINUTES: checked before any run,
    so a typo can't crash the import part-way or wait a negative time."""
    text = os.environ.get("APIFY_MAX_WAIT_MINUTES") or str(MAX_WAIT_MINUTES)
    try:
        minutes = float(text)
    except ValueError:
        minutes = math.nan
    if not math.isfinite(minutes) or not 0 < minutes <= MAX_WAIT_MINUTES:
        raise RuntimeError(f"APIFY_MAX_WAIT_MINUTES must be a number of minutes above 0 and at "
                           f"most {MAX_WAIT_MINUTES}, not {text!r}")
    return minutes


def ingest_searches(only_search: str | None = None, only_site: str | None = None,
                    posted_within_days: int | None = None) -> None:
    searches = load_searches()
    if posted_within_days is not None:   # one of options.POSTED_WITHIN_DAYS
        # This is a one-off override. Keep the saved local file and published Actions
        # variable unchanged; only this import plans runs with the selected window.
        searches = [replace(search, posted_within_days=posted_within_days) for search in searches]
    runs = plan(searches, only_search, only_site)
    if not runs:
        raise RuntimeError("No runs to do: check the search's sites and countries")
    max_wait = timedelta(minutes=wait_minutes())
    by_site: dict[str, list[PlannedRun]] = {}
    for planned in runs:
        by_site.setdefault(planned.site, []).append(planned)
    failed: list[str] = []

    def run_site(client: ApifyClient, site_runs: list[PlannedRun]) -> None:
        # One site's runs go one after another, so at most one Apify run per site.
        for planned in site_runs:
            try:
                run_search(client, planned, max_wait)
            except Exception as exc:
                run_name = f"{planned.search.name}/{planned.site} {planned.label}"
                failed.append(run_name)
                print(f"{run_name}: FAILED — {exc}", file=sys.stderr)

    # Daemon threads so Ctrl+C ends ingest at once instead of waiting up to max_wait for
    # each site's Apify call; an interrupted import rolls back its transaction.
    threads = [threading.Thread(target=run_site, args=(make_client(), site_runs),
                                name=f"ingest-{site}", daemon=True)
               for site, site_runs in by_site.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    from .store import archive_old_jobs, prune_archived_jobs, prune_sightings
    archived = archive_old_jobs()
    if archived:
        print(f"Archived {plural(archived, 'filtered, not interested, or untriaged Inbox job')}")
    pruned = prune_sightings()
    if pruned:
        print(f"Dropped the scraped records of {plural(pruned, 'sighting')} "
              "over 30 days old")
    cleared = prune_archived_jobs()
    if cleared:
        print(f"Cleared the ads of {plural(cleared, 'job')} archived over 90 days ago")
    if failed:
        raise SystemExit(f"{len(failed)} of {len(runs)} Apify runs failed")
