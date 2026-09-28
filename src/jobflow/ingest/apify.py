from __future__ import annotations

import math
import os
import sys
import threading
from datetime import timedelta
from decimal import Decimal
from functools import partial

from apify_client import ApifyClient

from .searches import PlannedRun, filter_reason, load_searches, plan

# The longest APIFY_MAX_WAIT_MINUTES may be: the daily GitHub job itself stops at 180 minutes.
MAX_WAIT_MINUTES = 180


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
        run = client.actor(planned.actor).call(
            run_input=planned.run_input, wait_duration=max_wait, logger=None,
            max_total_charge_usd=Decimal(str(cap)) if cap else None)
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
    """APIFY_MAX_WAIT_MINUTES, default 60, at most MAX_WAIT_MINUTES: checked before any run,
    so a typo can't crash the import part-way or wait a negative time."""
    text = os.environ.get("APIFY_MAX_WAIT_MINUTES") or "60"
    try:
        minutes = float(text)
    except ValueError:
        minutes = math.nan
    if not math.isfinite(minutes) or not 0 < minutes <= MAX_WAIT_MINUTES:
        raise RuntimeError(f"APIFY_MAX_WAIT_MINUTES must be a number of minutes above 0 and at "
                           f"most {MAX_WAIT_MINUTES}, not {text!r}")
    return minutes


def ingest_searches(only_search: str | None = None, only_site: str | None = None) -> None:
    runs = plan(load_searches(), only_search, only_site)
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
    from .store import archive_old_jobs
    archived = archive_old_jobs()
    if archived:
        print(f"Archived {archived} filtered or not interested job{'s' if archived != 1 else ''}")
    if failed:
        raise SystemExit(f"{len(failed)} of {len(runs)} Apify runs failed")
