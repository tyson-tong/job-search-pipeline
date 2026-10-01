"""Saving scraped postings: merging each into a known job (by the rules in dedupe.py) or
storing it as a new one, recording scraper runs, and the upkeep that follows: filtering
the Inbox after a searches change, archiving, and pruning old records."""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Callable

import psycopg
import psycopg.types.json

from .. import settings
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
from .searches import (
    country_from_location,
    excluded_reason,
    filter_reason,
    run_country,
    search_url_country,
)
from .sources import Posting, normalise, seek_url

JobFilter = Callable[[Posting], "str | None"]

# A filtered job this long past its posted date (or the date first seen, if later), or a job
# this long after you marked it Not interested, is archived (see archive_old_jobs).
ARCHIVE_AFTER = timedelta(days=14)
# An Inbox job you haven't triaged is archived once it was posted this long ago and has
# been in the Inbox this long (a job moved back to the Inbox starts again).
INBOX_ARCHIVE_AFTER = timedelta(days=30)
# How long each sighting keeps its full scraped record (see prune_sightings).
KEEP_SIGHTING_RECORDS = timedelta(days=30)
# How long an archived job keeps its description and scraped record (see
# prune_archived_jobs). By then it is past the 90 days in which a repost with a new ID is
# matched by its ad (FUZZY_WINDOW_DAYS), as it was archived at least 2 weeks after it was
# first seen. Similar-job notes look further back (similar.POSSIBLE_WINDOW_DAYS), but
# only notes are lost: a cleared job has no ad to compare.
KEEP_ARCHIVED_RECORDS = timedelta(days=90)


def find_duplicate(conn, key: str, fp: str | None, mkey: str | None, title: str | None,
                   description: str | None, location: str | None,
                   country: str | None = None) -> tuple[int, bool] | None:
    """Return (job id, is_first_copy) for an existing copy of this posting, if any.

    is_first_copy is True when this is the copy the jobs row was created from.
    Tried in order: this site's ID/URL on any known copy (job_sources), exact content
    fingerprint in the same country, the same employer and title seen recently in the same place and country
    with one ad a cut-down or padded copy of the other (another site's copy, or a repost;
    see is_copy_of_ad), then a recent job with the same title and a near-identical ad
    under any employer, in the same city and country (is_same_ad).
    """
    row = conn.execute("""SELECT s.job_id, j.canonical_key = s.source_key
                          FROM job_sources s JOIN jobs j ON j.id = s.job_id
                          WHERE s.source_key=%s""", (key,)).fetchone()
    if row:
        return row[0], row[1]
    if fp:
        # The fingerprint has the location but not the country, and a location may name
        # none ("Remote", or missing): one employer's ad for two countries is two jobs.
        for job_pk, existing_country in conn.execute(
                "SELECT id, country FROM jobs WHERE fingerprint=%s ORDER BY id", (fp,)).fetchall():
            if same_country(country, existing_country):
                return job_pk, False
    if mkey and description:
        candidates = conn.execute(
            """SELECT id, description, location, country FROM jobs
               WHERE match_key=%s AND first_seen_at > now() - make_interval(days => %s)
               ORDER BY first_seen_at DESC LIMIT 20""", (mkey, FUZZY_WINDOW_DAYS)).fetchall()
        for job_pk, existing, existing_location, existing_country in candidates:
            # A location naming no city ("Hong Kong", "Singapore") never rules out a match,
            # so the country must: one employer's ad for two countries is two jobs.
            if same_country(country, existing_country) and same_place(location, existing_location) \
                    and is_copy_of_ad(description, existing):
                return job_pk, False
    patterns = title_patterns(title)
    if description and patterns and title != "Untitled":
        # Titles first; descriptions are only loaded for the same title.
        rows = conn.execute(
            """SELECT id, title FROM jobs
               WHERE first_seen_at > now() - make_interval(days => %s) AND title ILIKE ANY(%s)
               ORDER BY first_seen_at DESC""", (FUZZY_WINDOW_DAYS, patterns)).fetchall()
        close = [pk for pk, other in rows if simplify(other) == simplify(title)]
        if close:
            candidates = conn.execute(
                """SELECT id, title, description, country, location FROM jobs WHERE id = ANY(%s)
                   ORDER BY first_seen_at DESC""", (close[:50],)).fetchall()
            for job_pk, other_title, existing, other_country, other_location in candidates:
                if is_same_ad(title, description, other_title, existing, country, other_country,
                              location, other_location):
                    return job_pk, False
    return None


def without_nul(value: object) -> object:
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
            """UPDATE jobs SET last_seen_at=now(),
                   raw_json=CASE WHEN %s THEN %s ELSE raw_json END,
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
        """INSERT INTO job_sources(source_key, job_id, source, source_job_id, url)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (source_key) DO UPDATE SET last_seen_at=now(),
               url=coalesce(EXCLUDED.url, job_sources.url)""",
        (key, job_pk, posting.source, posting.source_id, url))


def save_run(actor_id: str, run_id: str, dataset_id: str | None, jobs: list[dict], *,
             started_at: datetime | None = None, finished_at: datetime | None = None,
             cost_usd: float | None = None, search_name: str | None = None,
             run_input: dict | None = None, job_filter: JobFilter | None = None,
             country: str | None = None) -> int:
    from ..db import connect

    with connect() as conn:
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


def record_failed_run(actor_id: str, run_id: str | None, dataset_id: str | None, status: str,
                      error: str, started_at: datetime | None = None,
                      finished_at: datetime | None = None, cost_usd: float | None = None,
                      search_name: str | None = None, run_input: dict | None = None) -> None:
    from ..db import connect

    with connect() as conn:
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


def backfill_sources(conn) -> int:
    """Record every earlier sighting's site copy in job_sources (run once, when the
    table is created: the migration only creates it)."""
    rows = conn.execute("SELECT job_id, observed_json FROM job_observations").fetchall()
    before = conn.execute("SELECT count(*) FROM job_sources").fetchone()[0]
    for job_pk, raw in rows:
        posting = normalise(raw or {})
        key, url = identity(posting)
        conn.execute(
            """INSERT INTO job_sources(source_key, job_id, source, source_job_id, url)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_key) DO NOTHING""",
            (key, job_pk, posting.source, posting.source_id, url))
    return conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] - before


# Each job with the search whose run first found it, and that run's site.
_FIRST_SEARCH_SQL = """
    LEFT JOIN LATERAL (SELECT scraper_run_id FROM job_observations o
                       WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
    LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id"""
# A job you moved out of Filtered yourself (and so, if it's in the Inbox now, one you chose to
# keep), which update-searches leaves be.
_MOVED_BACK_SQL = """EXISTS (SELECT 1 FROM status_history h WHERE h.job_id = j.id
                            AND h.from_status = 'filtered'
                            AND h.via NOT IN ('update-searches', 'archive'))"""


def exclude_inbox_jobs(searches: list, dry_run: bool = False,
                       fallback=None) -> tuple[list[dict], int]:
    """Apply exclude_companies and exclude_title_terms to jobs already in the Inbox: each
    New job that the search which found it would keep out (see excluded_reason) becomes
    'filtered', with the reason an import would record, and the change is
    recorded in status_history. Only New jobs are touched, and never one you moved back
    to the Inbox from Filtered. A job whose search has since been renamed or removed is
    judged by `fallback` (the [defaults] exclusions), if given. Returns (the jobs matched,
    the number of Inbox jobs checked); with dry_run, nothing is changed."""
    from ..db import connect

    by_name = {search.name: search for search in searches}
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT j.id, j.title, j.company, r.search_name FROM jobs j {_FIRST_SEARCH_SQL}
                WHERE j.status = 'new' AND NOT {_MOVED_BACK_SQL}
                ORDER BY j.id""").fetchall()
        matched = []
        for job_id, title, company, search_name in rows:
            search = by_name.get(search_name, fallback)
            reason = search and excluded_reason(title, company, search)
            if reason:
                matched.append({"id": job_id, "title": title, "company": company,
                                "reason": reason})
        if not dry_run:
            for job in matched:
                changed = conn.execute(
                    """UPDATE jobs SET status='filtered', filter_reason=%s,
                           status_updated_at=now() WHERE id=%s AND status='new'""",
                    (job["reason"], job["id"])).rowcount
                if changed:
                    conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                                    VALUES (%s, 'new', 'filtered', 'update-searches')""",
                                 (job["id"],))
    return matched, len(rows)


# The reasons a [defaults] search can judge again: it has only the exclusions.
EXCLUSION_REASONS = ("title contains ", "employer matches ")


def restore_filtered_jobs(searches: list, dry_run: bool = False,
                          fallback=None) -> tuple[list[dict], int]:
    """Bring Filtered jobs (not yet archived) back to the Inbox when the search that found
    them wouldn't filter them now: each is judged again from its stored record by every
    filter an import applies (see filter_reason). A job whose search has since been
    renamed or removed is judged by `fallback` (the [defaults] exclusions), and only if it
    was filtered by an excluded title term or employer, the only filters those have.
    Returns (the jobs brought back, with the reason they were filtered, and the number of
    Filtered jobs checked); with dry_run, nothing is changed."""
    from ..db import connect

    by_name = {search.name: search for search in searches}
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT j.id, j.title, j.company, j.filter_reason, j.raw_json, j.country,
                       r.search_name, split_part(r.actor_id, ':', 1)
                FROM jobs j {_FIRST_SEARCH_SQL}
                WHERE j.status = 'filtered' ORDER BY j.id""").fetchall()
        restored = []
        for job_id, title, company, reason, raw, country, search_name, site in rows:
            search = by_name.get(search_name)
            if search and raw is not None:
                still = filter_reason(normalise(raw, country), search, site)
            elif fallback and (reason or "").startswith(EXCLUSION_REASONS):
                still = excluded_reason(title, company, fallback)
            else:
                continue   # nothing left to judge it by
            if not still:
                restored.append({"id": job_id, "title": title, "company": company,
                                 "reason": reason})
        if not dry_run:
            for job in restored:
                changed = conn.execute(
                    """UPDATE jobs SET status='new', filter_reason=NULL, status_updated_at=now()
                       WHERE id=%s AND status='filtered'""", (job["id"],)).rowcount
                if changed:
                    conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                                    VALUES (%s, 'filtered', 'new', 'update-searches')""",
                                 (job["id"],))
    return restored, len(rows)


def archive_old_jobs(today: date | None = None) -> int:
    """Move jobs out of the Filtered, Not interested, and Inbox tabs, to status 'archived':
    a filtered job ARCHIVE_AFTER past its posted date or the date first seen, whichever is
    later, a job ARCHIVE_AFTER after you marked it Not interested, and an Inbox job you
    never triaged, posted and in the Inbox for INBOX_ARCHIVE_AFTER. They stay in the database
    and still match as duplicates, so a repost stays out of the Inbox; the tracker shows
    them in no tab. Returns how many were archived."""
    from ..db import connect

    today = today or settings.today()
    with connect() as conn:
        return conn.execute(
            """WITH moved AS (
                   UPDATE jobs j SET status='archived', status_updated_at=now()
                   FROM (SELECT id, status FROM jobs
                         -- A filtered job first seen well after it was posted (in a
                         -- month-long window, say) still gets its 2 weeks in the Filtered tab.
                         WHERE (status = 'filtered'
                                AND greatest(posted_at, first_seen_at::date) < %(cutoff)s)
                            OR (status = 'not_interested' AND status_updated_at::date < %(cutoff)s)
                            OR (status = 'new' AND coalesce(posted_at, first_seen_at::date) < %(inbox_cutoff)s
                                AND status_updated_at::date < %(inbox_cutoff)s)
                         FOR UPDATE) old
                   WHERE j.id = old.id
                   RETURNING j.id, old.status)
               INSERT INTO status_history(job_id, from_status, to_status, via)
               SELECT id, status, 'archived', 'archive' FROM moved""",
            {"cutoff": today - ARCHIVE_AFTER, "inbox_cutoff": today - INBOX_ARCHIVE_AFTER}).rowcount


def prune_sightings() -> int:
    """Drop the scraped record from sightings older than KEEP_SIGHTING_RECORDS, keeping the
    sighting itself (which job, which run, when). Each job keeps its first copy's record
    in jobs.raw_json and every site's ID and link in job_sources; the other copies were
    only needed while they were new. Returns how many records were dropped."""
    from ..db import connect

    with connect() as conn:
        return conn.execute(
            """UPDATE job_observations SET observed_json = NULL
               WHERE observed_json IS NOT NULL AND observed_at < now() - %s""",
            (KEEP_SIGHTING_RECORDS,)).rowcount


def prune_archived_jobs() -> int:
    """Clear the description and scraped record of jobs archived more than
    KEEP_ARCHIVED_RECORDS ago. The job itself stays: its sites' IDs, its fingerprint, and
    its title, so a repost is still recognised. Returns how many jobs were cleared."""
    from ..db import connect

    with connect() as conn:
        return conn.execute(
            """UPDATE jobs SET description = NULL, raw_json = NULL
               WHERE status = 'archived' AND status_updated_at < now() - %s
                 AND (description IS NOT NULL OR raw_json IS NOT NULL)""",
            (KEEP_ARCHIVED_RECORDS,)).rowcount


def backfill_countries(conn) -> int:
    """Fill jobs.country from the run that first found each job (run once, when the
    column is added), falling back to the job's own search URL, then its location."""
    rows = conn.execute(
        """SELECT j.id, j.location, r.actor_id, r.input, first.observed_json FROM jobs j
           LEFT JOIN LATERAL (SELECT scraper_run_id, observed_json FROM job_observations o
                              WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
           LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id
           WHERE j.country IS NULL""").fetchall()
    filled = 0
    for job_pk, location, actor_id, run_input, item in rows:
        site = (actor_id or "").split(":", 1)[0]
        country = (run_country(site, run_input) or search_url_country(item)
                   or country_from_location(location))
        if country:
            conn.execute("UPDATE jobs SET country=%s WHERE id=%s", (country, job_pk))
            filled += 1
    return filled

