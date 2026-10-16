"""Running the planned searches on Apify (jobflow ingest and the daily import): the sites
side by side, each site's runs one after another, saving what each found, within the time
GitHub allows; then archiving and pruning old jobs."""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from functools import partial

from apify_client import ApifyClient

from .. import settings
from ..text import plural
from .filters import filter_reason
from .runs import PlannedRun, plan, run_country, run_hit_cap
from .searches import load_searches

# The longest APIFY_MAX_WAIT_MINUTES may be, and its default. Each site's runs go one after
# another in a daily GitHub job stopped at 180 minutes, where a run cut off keeps charging
# on Apify: a short wait leaves room for several runs per site.
MAX_WAIT_MINUTES = 30
# How long one import may take. GitHub stops the daily job at 180 minutes (timeout-minutes
# in .github/workflows/daily-ingest.yml), which would lose the runs not yet started and skip
# the upkeep after them: a run is started only if it can end within this, wait included.
INGEST_MINUTES = 150


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
        # process is killed first (the daily GitHub job is stopped at 180 minutes). The
        # cost cap only binds a pay-per-event actor and the item cap a pay-per-result one,
        # so both are sent: an actor that changes its pricing is still capped.
        run = client.actor(planned.actor).call(
            run_input=planned.run_input, wait_duration=max_wait, run_timeout=max_wait,
            logger=None, max_items=planned.max_results,
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
        capped = planned.hit_cap(items)
        # Sites run at the same time; save_run makes their imports take turns.
        added = save_run(actor_id, run.id, run.default_dataset_id, items,
                         started_at=run.started_at, finished_at=run.finished_at,
                         cost_usd=run.usage_total_usd, search_name=planned.search.name,
                         run_input=planned.run_input, job_filter=job_filter,
                         country=planned.country, hit_cap=capped)
        cost = f", ${run.usage_total_usd:.2f}" if run.usage_total_usd is not None else ""
        print(f"{name}: {len(items)} items, {added} new jobs{cost}")
        if capped:
            print(f"{name}: warning: it returned as many results as max_results allows, so "
                  "more jobs were probably posted: raise max_results, or narrow the search",
                  file=sys.stderr)
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


def account_wide(exc: Exception) -> bool:
    """Whether every other run on this Apify account would fail the same way: a token
    Apify rejects, or the account's monthly usage limit reached."""
    from apify_client.errors import ApifyApiError

    return (isinstance(exc, ApifyApiError) and exc.status_code == 401) \
        or "usage hard limit" in str(exc).casefold()


@dataclass
class _Outcome:
    """The run names an import didn't get through, filled in by each site's thread."""
    failed: list[str] = field(default_factory=list)
    # Not started after an error every later run would hit too (account_wide).
    skipped: list[str] = field(default_factory=list)
    # Not started: they could have run past INGEST_MINUTES.
    late: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)


def _run_site(client: ApifyClient, site_runs: list[PlannedRun], max_wait: timedelta,
              deadline: float, outcome: _Outcome) -> None:
    """One site's runs, one after another, so at most one Apify run per site at a time."""
    from .store import record_failed_run

    for planned in site_runs:
        run_name = f"{planned.search.name}/{planned.site} {planned.label}"
        if outcome.stop.is_set():
            outcome.skipped.append(run_name)
            continue
        if time.monotonic() + max_wait.total_seconds() > deadline:
            outcome.late.append(run_name)
            try:   # recorded, so the tracker's header and jobflow status show it
                record_failed_run(f"{planned.site}:{planned.actor}", None, None,
                                  "NOT_STARTED", "not started: the import could have run "
                                  f"past its {INGEST_MINUTES}-minute limit",
                                  search_name=planned.search.name,
                                  run_input=planned.run_input)
            except Exception as exc:
                print(f"{run_name}: could not record it as not started: {exc}",
                      file=sys.stderr)
            continue
        try:
            run_search(client, planned, max_wait)
        except Exception as exc:
            outcome.failed.append(run_name)
            print(f"{run_name}: FAILED — {exc}", file=sys.stderr)
            if account_wide(exc):
                outcome.stop.set()


def _upkeep() -> None:
    """Archiving and pruning, after every import."""
    from .upkeep import archive_old_jobs, prune_archived_jobs, prune_sightings

    archived = archive_old_jobs()
    if archived:
        print(f"Archived {plural(archived, 'filtered, not interested, or untriaged Inbox job')}")
    pruned = prune_sightings()
    if pruned:
        print(f"Dropped the scraped records of {plural(pruned, 'sighting')} over 30 days old")
    cleared = prune_archived_jobs()
    if cleared:
        print(f"Cleared the ads of {plural(cleared, 'job')} archived over 90 days ago")


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
    outcome = _Outcome()
    deadline = time.monotonic() + INGEST_MINUTES * 60
    # Daemon threads so Ctrl+C ends ingest at once instead of waiting up to max_wait for
    # each site's Apify call; an interrupted import rolls back its transaction.
    threads = [threading.Thread(target=_run_site, name=f"ingest-{site}", daemon=True,
                                args=(make_client(), site_runs, max_wait, deadline, outcome))
               for site, site_runs in by_site.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _upkeep()
    failed, skipped, late = outcome.failed, outcome.skipped, outcome.late
    if late:
        print(f"Not started, to finish within {INGEST_MINUTES} minutes: {', '.join(late)}",
              file=sys.stderr)
    if skipped:
        raise RuntimeError(f"{len(failed)} of {len(runs)} Apify runs failed, and the other "
                           f"{len(skipped)} weren't started: the error is your Apify account's "
                           "(its token, or its monthly usage limit), so they would fail too")
    if failed or late:
        raise RuntimeError(f"{len(failed)} of {len(runs)} Apify runs failed"
                           + (f", and {len(late)} weren't started, to finish within "
                              f"{INGEST_MINUTES} minutes: search fewer terms or places per "
                              "site, or lower APIFY_MAX_WAIT_MINUTES (for the daily import, "
                              "its GitHub variable)"
                              if late else ""))


def retry_imports() -> None:
    """Import again the results of each run recorded as IMPORT_FAILED: they're in the run's
    dataset on Apify (already paid for) until Apify deletes it. Each is saved as its import
    would have been: filtered by the search that ran it, as on the day it ran, so a
    posted-date window doesn't filter jobs that have merely aged since. A run whose dataset
    Apify has deleted is recorded as EXPIRED, so it isn't tried again."""
    from apify_client.errors import ApifyApiError

    from .store import failed_imports, record_failed_run, save_run

    runs = failed_imports()
    if not runs:
        print("No failed imports to retry")
        return
    searches = {search.name: search for search in load_searches()}
    client = make_client()
    failed = 0
    for run in runs:
        site = run["actor_id"].split(":", 1)[0]
        name = f"{run['search_name']}/{site} run {run['apify_run_id']}"
        search, run_input = searches.get(run["search_name"]), run["input"] or {}
        ran_on = (run["finished_at"].astimezone(settings.timezone()).date()
                  if run["finished_at"] else None)

        def record(status: str, error: str, run=run) -> None:
            record_failed_run(run["actor_id"], run["apify_run_id"], run["dataset_id"], status,
                              error, run["started_at"], run["finished_at"],
                              run["cost_usd"], run["search_name"], run["input"])
        try:
            items = list(client.dataset(run["dataset_id"]).iterate_items(clean=True))
        except Exception as exc:
            if isinstance(exc, ApifyApiError) and exc.status_code == 404:
                gone = "its results are no longer on Apify, so they can't be imported"
                record("EXPIRED", gone)
                print(f"{name}: {gone}", file=sys.stderr)
            else:   # left as it is, to try again
                print(f"{name}: couldn't read its results from Apify — {exc}", file=sys.stderr)
            failed += 1
            continue
        job_filter = (partial(filter_reason, search=search, site=site, today=ran_on)
                      if search else None)
        try:
            added = save_run(
                run["actor_id"], run["apify_run_id"], run["dataset_id"], items,
                started_at=run["started_at"], finished_at=run["finished_at"],
                cost_usd=run["cost_usd"], search_name=run["search_name"],
                run_input=run["input"], country=run_country(site, run_input),
                hit_cap=bool(run_input) and run_hit_cap(run_input, items),
                job_filter=job_filter)
        except Exception as exc:
            record("IMPORT_FAILED", str(exc))
            print(f"{name}: FAILED again — {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"{name}: {len(items)} items, {added} new jobs")
        if not search:
            print(f"{name}: its search {run['search_name']!r} is no longer in your searches, "
                  "so its jobs weren't filtered: run `uv run jobflow update-searches` to judge "
                  "them by [defaults]", file=sys.stderr)
    if failed:
        raise RuntimeError(f"{failed} of {plural(len(runs), 'failed import')} couldn't be "
                           "imported (see above)")
