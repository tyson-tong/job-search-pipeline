"""Running the planned searches on Apify (jobflow ingest and the daily import): the sites
side by side, each site's runs one after another, saving what each found, within the time
GitHub allows; then archiving and pruning old jobs. retry.py imports again the results of
earlier runs that couldn't be saved, before each import's own runs."""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

from apify_client import ApifyClient

from ..text import plural
from . import store
from .filters import search_filter
from .runs import PlannedRun, plan
from .searches import load_searches
from .store import RunRecord

# The longest APIFY_MAX_WAIT_MINUTES may be, and its default. Each site's runs go one after
# another in a daily GitHub job stopped at 180 minutes, where a run cut off keeps charging
# on Apify: a short wait leaves room for several runs per site.
MAX_WAIT_MINUTES = 30
# How long one import may take. GitHub stops the daily job at 180 minutes (timeout-minutes
# in .github/workflows/daily-ingest.yml), which would lose the runs not yet started and skip
# the upkeep after them: a run is started only if it can end within this, wait included.
INGEST_MINUTES = 150
# Apify run statuses: still going after max_wait (we stop it), or stopped before it finished
# by its own timeout (max_wait) or by us. What such a run found is paid for, so it's imported,
# and the run recorded as PARTIAL: it may have left jobs behind.
STILL_GOING = ("READY", "RUNNING")
STOPPED_EARLY = ("TIMING-OUT", "TIMED-OUT", "ABORTING", "ABORTED")


def make_client() -> ApifyClient:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is required")
    return ApifyClient(token)


def run_record(planned: PlannedRun, run=None) -> RunRecord:
    """The run as scraper_runs records it: the planned search, and once Apify has run it,
    its ID, dataset, times, and cost, and whether it was stopped before it finished."""
    record = RunRecord(actor_id=f"{planned.site}:{planned.actor}",
                       search_name=planned.search.name, run_input=planned.run_input,
                       posted_within_days=planned.search.posted_within_days)
    if run is None:
        return record
    return replace(record, run_id=run.id, dataset_id=run.default_dataset_id,
                   started_at=run.started_at, finished_at=run.finished_at,
                   cost_usd=run.usage_total_usd,
                   partial=run.status in (*STILL_GOING, *STOPPED_EARLY))


class StoppedEarly(RuntimeError):
    """A run stopped before it finished, whose results were imported and recorded as
    PARTIAL: raised so the import still counts it as failed."""


def run_search(client: ApifyClient, planned: PlannedRun, max_wait: timedelta) -> None:
    """Run one actor for one search, import its dataset, and record the run.

    Any failure is recorded in scraper_runs and re-raised: with Apify's status, or
    IMPORT_FAILED for a run whose results couldn't be saved. A run still going after
    max_wait is stopped; what it found is imported, recorded as PARTIAL, and StoppedEarly
    raised.
    """
    name = planned.name
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
        detail = f": {run.status_message}" if run.status_message else ""
        if run.status in STILL_GOING:
            # Left alone it would keep scraping, and charging, for results never imported.
            try:
                client.run(run.id).abort()
                stopped = "stopped"
            except Exception as exc:
                stopped = f"stopping it failed ({exc}); abort it in the Apify Console"
            detail += f" (still running after {max_wait}, so {stopped})"
        elif run.status != "SUCCEEDED" and run.status not in STOPPED_EARLY:
            raise RuntimeError(f"Apify run {run.id} ended with {run.status}{detail}")
        record = run_record(planned, run)
        items = list(client.dataset(run.default_dataset_id).iterate_items(clean=True))
        capped = planned.hit_cap(items)
        # Sites run at the same time; save_run makes their imports take turns.
        added = store.save_run(record, items, country=planned.country, hit_cap=capped,
                               job_filter=search_filter(planned.search, planned.site))
        cost = f", ${run.usage_total_usd:.2f}" if run.usage_total_usd is not None else ""
        print(f"{name}: {len(items)} items, {added} new jobs{cost}")
        if capped:
            print(f"{name}: warning: it returned as many results as max_results allows, so "
                  "more jobs were probably posted: raise max_results, or narrow the search",
                  file=sys.stderr)
        if record.partial:
            raise StoppedEarly(f"Apify run {run.id} ended with {run.status}{detail}: the "
                               f"{plural(len(items), 'result')} it found were imported, but it "
                               "may have left jobs behind")
    except StoppedEarly:
        raise
    except Exception as exc:
        # A run that finished on Apify, or was stopped part-way, failed here, importing its
        # results; they stay in its dataset on Apify, to be imported again (retry.py).
        importable = run is not None and run.status in ("SUCCEEDED", *STILL_GOING,
                                                        *STOPPED_EARLY)
        status = ("FAILED" if run is None else store.IMPORT_FAILED if importable
                  else run.status)
        try:
            store.record_failed_run(run_record(planned, run), status=status, error=str(exc))
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
    for planned in site_runs:
        run_name = planned.name
        if outcome.stop.is_set():
            outcome.skipped.append(run_name)
            continue
        if time.monotonic() + max_wait.total_seconds() > deadline:
            outcome.late.append(run_name)
            try:   # recorded, so the tracker's header and jobflow status show it
                store.record_failed_run(run_record(planned), status="NOT_STARTED",
                                        error="not started: the import could have run past "
                                        f"its {INGEST_MINUTES}-minute limit")
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
    from .retry import import_failed_runs

    configured = searches = load_searches()
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
    # Earlier runs whose results couldn't be saved are paid for, and their results are on
    # Apify only for a while: imported first, judged by the searches as configured.
    not_imported, _ = import_failed_runs(configured)
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
    earlier = (f"; and {plural(not_imported, 'earlier run')} whose results couldn't be saved "
               "still couldn't be imported (see above)" if not_imported else "")
    if skipped:
        raise RuntimeError(f"{len(failed)} of {len(runs)} Apify runs failed, and the other "
                           f"{len(skipped)} weren't started: the error is your Apify account's "
                           "(its token, or its monthly usage limit), so they would fail too"
                           + earlier)
    if failed or late:
        raise RuntimeError(f"{len(failed)} of {len(runs)} Apify runs failed"
                           + (f", and {len(late)} weren't started, to finish within "
                              f"{INGEST_MINUTES} minutes: search fewer terms or places per "
                              "site, or lower APIFY_MAX_WAIT_MINUTES (for the daily import, "
                              "its GitHub variable)"
                              if late else "") + earlier)
    if not_imported:
        raise RuntimeError(f"Every Apify run succeeded, but {plural(not_imported, 'earlier run')} "
                           "whose results couldn't be saved still couldn't be imported (see "
                           "above)")
