from __future__ import annotations

import os
import sys
from datetime import timedelta
from decimal import Decimal
from functools import partial

from apify_client import ApifyClient

from .searches import PlannedRun, filter_reason, load_searches, plan


def make_client() -> ApifyClient:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is required")
    return ApifyClient(token)


def run_search(client: ApifyClient, planned: PlannedRun, max_wait: timedelta) -> None:
    """Run one actor for one search, import its dataset, and record the run.

    Any failure is recorded in scraper_runs and re-raised.
    """
    from .ingest import record_failed_run, save_run

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
                detail += f" (still running after {max_wait}; results were not imported)"
            raise RuntimeError(f"Apify run {run.id} ended with {run.status}{detail}")
        items = list(client.dataset(run.default_dataset_id).iterate_items(clean=True))
        job_filter = partial(filter_reason, search=planned.search, site=planned.site)
        added = save_run(actor_id, run.id, run.default_dataset_id, items,
                         started_at=run.started_at, finished_at=run.finished_at,
                         cost_usd=run.usage_total_usd, search_name=planned.search.name,
                         run_input=planned.run_input, job_filter=job_filter)
        cost = f", ${run.usage_total_usd:.2f}" if run.usage_total_usd is not None else ""
        print(f"{name}: {len(items)} items, {added} new jobs{cost}")
    except Exception as exc:
        try:
            record_failed_run(actor_id, run.id if run else None,
                              run.default_dataset_id if run else None,
                              run.status if run else "FAILED", str(exc),
                              run.started_at if run else None,
                              run.finished_at if run else None,
                              run.usage_total_usd if run else None,
                              planned.search.name, planned.run_input)
        except Exception as record_exc:
            print(f"{name}: could not record failure: {record_exc}", file=sys.stderr)
        raise


def ingest_searches(only_search: str | None = None, only_site: str | None = None) -> None:
    runs = plan(load_searches(), only_search, only_site)
    if not runs:
        raise RuntimeError("No runs to do: check the search's sites and countries")
    client = make_client()
    max_wait = timedelta(minutes=float(os.environ.get("APIFY_MAX_WAIT_MINUTES") or 60))
    failed = []
    for planned in runs:
        try:
            run_search(client, planned, max_wait)
        except Exception as exc:
            failed.append(f"{planned.search.name}/{planned.site} {planned.label}")
            print(f"{failed[-1]}: FAILED — {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(f"{len(failed)} of {len(runs)} Apify runs failed")
