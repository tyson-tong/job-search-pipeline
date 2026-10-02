"""Saving scraped postings: merging each into a known job (by the rules in dedupe.py) or
storing it as a new one, and recording scraper runs. refilter.py applies a searches
change to jobs already saved, upkeep.py archives and prunes, and backfill.py fills in
new tables and columns once."""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import psycopg
import psycopg.types.json

from .. import db
from .dedupe import (
    FUZZY_WINDOW_DAYS,
    fingerprint,
    identity,
    is_copy_of_ad,
    is_same_ad,
    match_key,
    same_country,
    same_place,
    simplify,
    title_patterns,
)
from .runs import country_from_location, search_url_country
from .sources import Posting, normalise, seek_url

JobFilter = Callable[[Posting], "str | None"]


def _same_copy(conn, key: str) -> tuple[int, bool] | None:
    """Rule 1: this site's ID (or URL) on any known copy of a job."""
    row = conn.execute("""SELECT s.job_id, j.canonical_key = s.source_key
                          FROM job_sources s JOIN jobs j ON j.id = s.job_id
                          WHERE s.source_key=%s""", (key,)).fetchone()
    return (row[0], row[1]) if row else None


def _same_content(conn, fp: str | None, country: str | None) -> int | None:
    """Rule 2: the exact content fingerprint, in the same country. The fingerprint has the
    location but not the country, and a location may name none ("Remote", or missing):
    one employer's ad for two countries is two jobs."""
    if not fp:
        return None
    for job_pk, existing_country in conn.execute(
            "SELECT id, country FROM jobs WHERE fingerprint=%s ORDER BY id", (fp,)).fetchall():
        if same_country(country, existing_country):
            return job_pk
    return None


def _same_employer_and_title(conn, mkey: str | None, description: str | None,
                             location: str | None, country: str | None) -> int | None:
    """Rule 3: the same employer and title seen recently in the same place and country,
    with one ad a cut-down or padded copy of the other (see is_copy_of_ad)."""
    if not (mkey and description):
        return None
    candidates = conn.execute(
        """SELECT id, description, location, country FROM jobs
           WHERE match_key=%s AND first_seen_at > now() - make_interval(days => %s)
           ORDER BY first_seen_at DESC LIMIT 20""", (mkey, FUZZY_WINDOW_DAYS)).fetchall()
    for job_pk, existing, existing_location, existing_country in candidates:
        # A location naming no city ("Hong Kong", "Singapore") never rules out a match,
        # so the country must: one employer's ad for two countries is two jobs.
        if same_country(country, existing_country) and same_place(location, existing_location) \
                and is_copy_of_ad(description, existing):
            return job_pk
    return None


def _same_ad(conn, title: str | None, description: str | None, location: str | None,
             country: str | None) -> int | None:
    """Rule 4: a recent job with the same title and a near-identical ad under any employer,
    in the same city and country (is_same_ad)."""
    patterns = title_patterns(title)
    if not (description and patterns and title != "Untitled"):
        return None
    # Titles first; descriptions are only loaded for the same title.
    rows = conn.execute(
        """SELECT id, title FROM jobs
           WHERE first_seen_at > now() - make_interval(days => %s) AND title ILIKE ANY(%s)
           ORDER BY first_seen_at DESC""", (FUZZY_WINDOW_DAYS, patterns)).fetchall()
    close = [pk for pk, other in rows if simplify(other) == simplify(title)]
    if not close:
        return None
    candidates = conn.execute(
        """SELECT id, title, description, country, location FROM jobs WHERE id = ANY(%s)
           ORDER BY first_seen_at DESC""", (close[:50],)).fetchall()
    for job_pk, other_title, existing, other_country, other_location in candidates:
        if is_same_ad(title, description, other_title, existing, country, other_country,
                      location, other_location):
            return job_pk
    return None


def find_duplicate(conn, key: str, fp: str | None, mkey: str | None, title: str | None,
                   description: str | None, location: str | None,
                   country: str | None = None) -> tuple[int, bool] | None:
    """Return (job id, is_first_copy) for an existing copy of this posting, if any, by the
    first of the rules in docs/rules.md that matches (_same_copy, _same_content,
    _same_employer_and_title, _same_ad). is_first_copy is True when this is the copy the
    jobs row was created from."""
    found = _same_copy(conn, key)
    if found:
        return found
    job_pk = (_same_content(conn, fp, country)
              or _same_employer_and_title(conn, mkey, description, location, country)
              or _same_ad(conn, title, description, location, country))
    return (job_pk, False) if job_pk else None


def without_nul(value: Any) -> Any:
    """The scraped record without NUL characters, which PostgreSQL text and jsonb reject:
    one in any posting would fail the import of its whole (already paid for) run."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {without_nul(k): without_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [without_nul(v) for v in value]
    return value


def save_job(conn, run_pk: int, item: dict, job_filter: JobFilter | None = None,
             country: str | None = None) -> bool:
    """Upsert one scraped job and record the observation. Returns True if new.

    country is the one country the run searched, if known; otherwise it comes from the
    job's own LinkedIn search URL, else from its location where that names one.

    A new job failing job_filter is still stored, with status 'filtered' and the reason,
    so it stays out of the Inbox and scoring but remains a known duplicate.
    """
    item = without_nul(item)
    posting = normalise(item)
    country = country or search_url_country(item) or country_from_location(posting.location)
    if country:   # read again knowing the country, which decides what a bare salary means
        posting = normalise(item, country)
    if posting.source == "seek" and not posting.url and posting.source_id:
        posting = replace(posting, url=seek_url(posting.source_id, country))
    key, url = identity(posting)
    fp, mkey = fingerprint(posting), match_key(posting)
    description = posting.description or None
    raw = psycopg.types.json.Jsonb(item)
    found = find_duplicate(conn, key, fp, mkey, posting.title, description, posting.location,
                           country)
    if found is None:
        reason = job_filter(posting) if job_filter else None
        job_pk = conn.execute(
            """INSERT INTO jobs(canonical_key, source_job_id, canonical_url, title, company,
                   location, posted_at, description, raw_json, fingerprint, source, match_key,
                   salary_min, salary_max, status, filter_reason, country)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (key, posting.source_id, url, posting.title, posting.company, posting.location,
             posting.posted_at, description, raw, fp, posting.source, mkey,
             posting.salary_min, posting.salary_max, "filtered" if reason else "new",
             reason, country)).fetchone()[0]
    else:
        job_pk, is_first_copy = found
        # Another site's copy keeps the first site's record; its own copy is kept in
        # job_sources and as an observation. A longer description from any site is preferred.
        conn.execute(
            """UPDATE jobs SET raw_json=CASE WHEN %s THEN %s ELSE raw_json END,
                   description=CASE WHEN length(coalesce(%s, '')) > length(coalesce(description, ''))
                                    THEN %s ELSE description END,
                   canonical_url=coalesce(canonical_url, %s),
                   posted_at=coalesce(posted_at, %s),
                   fingerprint=coalesce(fingerprint, %s),
                   source=coalesce(source, %s),
                   match_key=coalesce(match_key, %s),
                   salary_min=coalesce(salary_min, %s),
                   salary_max=coalesce(salary_max, %s),
                   country=coalesce(country, %s)
               WHERE id=%s""",
            (is_first_copy, raw, description, description, url, posting.posted_at, fp,
             posting.source, mkey, posting.salary_min, posting.salary_max, country, job_pk))
    record_source(conn, job_pk, key, posting, url)
    conn.execute(
        """INSERT INTO job_observations(job_id, scraper_run_id, observed_json)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
        (job_pk, run_pk, raw))
    return found is None


def record_source(conn, job_pk: int, key: str, posting: Posting, url: str | None) -> None:
    conn.execute(
        """INSERT INTO job_sources(source_key, job_id, source, url) VALUES (%s,%s,%s,%s)
           ON CONFLICT (source_key) DO UPDATE SET url=coalesce(EXCLUDED.url, job_sources.url)""",
        (key, job_pk, posting.source, url))


def save_run(actor_id: str, run_id: str, dataset_id: str | None, jobs: list[dict], *,
             started_at: datetime | None = None, finished_at: datetime | None = None,
             cost_usd: float | None = None, search_name: str | None = None,
             run_input: dict | None = None, job_filter: JobFilter | None = None,
             country: str | None = None) -> int:
    with db.connect() as conn:
        # Imports take turns, in this process (sites run in parallel) and across processes
        # (a manual jobflow ingest during the daily one): two copies of one job saved at
        # once would each miss the other and never merge. Held until this run commits.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('jobflow save_run'))")
        run_pk = conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, items_seen, cost_usd, search_name, input)
               VALUES (%s, %s, %s, 'SUCCEEDED', coalesce(%s, now()), coalesce(%s, now()),
                       %s, %s, %s, %s)
               ON CONFLICT (apify_run_id) DO UPDATE SET dataset_id=EXCLUDED.dataset_id,
                   status='SUCCEEDED', started_at=EXCLUDED.started_at,
                   finished_at=EXCLUDED.finished_at, items_seen=EXCLUDED.items_seen,
                   cost_usd=EXCLUDED.cost_usd, error=NULL
               RETURNING id""",
            (actor_id, run_id, dataset_id, started_at, finished_at, len(jobs), cost_usd,
             search_name, psycopg.types.json.Jsonb(run_input) if run_input else None)
        ).fetchone()[0]
        added, skipped = 0, 0
        for job in jobs:
            # The run is paid for: a posting that can't be read or that the database rejects
            # is skipped, not allowed to fail the import of every other posting in it.
            # A lost connection isn't the posting's fault, and fails every posting after it:
            # it fails the import, which is then recorded as IMPORT_FAILED.
            try:
                with conn.transaction():
                    added += save_job(conn, run_pk, job, job_filter, country)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                raise
            except Exception as exc:
                title = job.get("title") if isinstance(job, dict) else None
                print(f"skipped a posting that can't be saved ({title!r}): {exc}", file=sys.stderr)
                skipped += 1
        # Every posting failing is no one posting's fault (a scraper that changed its output,
        # say): fail the import, so it's recorded as IMPORT_FAILED with its dataset to retry.
        if jobs and skipped == len(jobs):
            raise RuntimeError(f"none of the run's {len(jobs)} postings could be saved")
        return added


# Apify runs this recent count as the last import's: a day, with slack for GitHub starting
# the daily import late.
RECENT_RUNS = timedelta(hours=36)


def recent_failed_runs(conn) -> list[dict]:
    """Each site with runs that failed in RECENT_RUNS, even if its other runs (or other
    sites') succeeded: {site, failed, total, error}, with the latest failure's error."""
    return [dict(zip(("site", "failed", "total", "error"), row, strict=True)) for row in conn.execute(
        """SELECT split_part(actor_id, ':', 1),
                  count(*) FILTER (WHERE status <> 'SUCCEEDED'), count(*),
                  (array_agg(error ORDER BY created_at DESC)
                       FILTER (WHERE status <> 'SUCCEEDED'))[1]
           FROM scraper_runs WHERE created_at > now() - %s
           GROUP BY 1 HAVING count(*) FILTER (WHERE status <> 'SUCCEEDED') > 0
           ORDER BY 1""", (RECENT_RUNS,)).fetchall()]


def record_failed_run(actor_id: str, run_id: str | None, dataset_id: str | None, status: str,
                      error: str, started_at: datetime | None = None,
                      finished_at: datetime | None = None, cost_usd: float | None = None,
                      search_name: str | None = None, run_input: dict | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, error, cost_usd, search_name, input)
               VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s)
               ON CONFLICT (apify_run_id) DO UPDATE SET status=EXCLUDED.status,
                   finished_at=EXCLUDED.finished_at, error=EXCLUDED.error,
                   cost_usd=EXCLUDED.cost_usd""",
            (actor_id, run_id, dataset_id, status, started_at, finished_at, error[:4000],
             cost_usd, search_name,
             psycopg.types.json.Jsonb(run_input) if run_input else None))
