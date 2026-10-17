"""Saving scraped postings: merging each into a known job (by the rules in dedupe.py) or
storing it as a new one, and recording scraper runs. refilter.py applies a searches
change to jobs already saved, upkeep.py archives and prunes, and backfill.py fills in
new tables and columns once."""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import psycopg
import psycopg.types.json
from psycopg.rows import dict_row

from .. import db
from .dedupe import (
    FUZZY_WINDOW_DAYS,
    Ad,
    fingerprint,
    identity,
    is_copy_of_ad,
    is_same_ad,
    match_key,
    same_country,
    same_place,
    title_key,
    title_patterns,
)
from .filters import Filtered
from .runs import country_from_location, search_url_country
from .sources import Posting, normalise, seek_url

JobFilter = Callable[[Posting], "Filtered | None"]


@dataclass(frozen=True, kw_only=True)
class RunRecord:
    """One Apify run as scraper_runs records it, whether it succeeded, failed, or was never
    started: built once per run and passed whole, since several of its fields are IDs or
    times that are easily swapped when written out one by one, storing a run that can't be
    retried. posted_within_days is the window its search ran with (ingest
    --posted-within-days may have widened it); partial, that Apify stopped the run before it
    finished, so it may have left jobs behind."""
    actor_id: str   # "site:actor"
    run_id: str | None = None
    dataset_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float | None = None
    search_name: str | None = None
    run_input: dict | None = None
    posted_within_days: int | None = None
    partial: bool = False

    @property
    def site(self) -> str:
        return self.actor_id.split(":", 1)[0]

    def _row(self) -> dict:
        """The record as query parameters, for the INSERTs below."""
        return {"actor_id": self.actor_id, "run_id": self.run_id, "dataset_id": self.dataset_id,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "cost_usd": self.cost_usd, "search_name": self.search_name,
                "input": psycopg.types.json.Jsonb(self.run_input) if self.run_input else None,
                "posted_within_days": self.posted_within_days, "partial": self.partial}


# What scraper_runs.error says about a run Apify stopped before it finished, whose results
# were imported (status PARTIAL): the tracker's header and jobflow status show it.
PARTIAL_ERROR = ("stopped before it finished (it ran past APIFY_MAX_WAIT_MINUTES), so it may "
                 "have left jobs behind; the results it found were imported")


def wait_for_imports(conn) -> None:
    """Take turns with every other import (and archiving), until conn's transaction ends:
    two copies of one job saved at once would each miss the other and never merge, and
    archiving while a run is saved could deadlock with it, failing that run's import."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('jobflow save_run'))")


def _same_copy(conn, key: str) -> tuple[int, bool] | None:
    """Rule 1: this site's ID (or URL) on any known copy of a job."""
    row = conn.execute("""SELECT s.job_id, j.canonical_key = s.source_key
                          FROM job_sources s JOIN jobs j ON j.id = s.job_id
                          WHERE s.source_key=%s""", (key,)).fetchone()
    if row:
        return row[0], row[1]
    # A job from before job_sources whose sightings' records were already pruned when the
    # table was filled in has no row there, but its first copy's key is still unique in jobs.
    row = conn.execute("SELECT id FROM jobs WHERE canonical_key=%s", (key,)).fetchone()
    return (row[0], True) if row else None


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


def _same_employer_and_title(conn, mkey: str | None, ad: Ad) -> int | None:
    """Rule 3: the same employer and title seen recently in the same place and country,
    with one ad a cut-down or padded copy of the other (see is_copy_of_ad)."""
    if not (mkey and ad.description):
        return None
    candidates = conn.execute(
        """SELECT id, description, location, country FROM jobs
           WHERE match_key=%s AND first_seen_at > now() - make_interval(days => %s)
           ORDER BY first_seen_at DESC LIMIT 20""", (mkey, FUZZY_WINDOW_DAYS)).fetchall()
    for job_pk, existing, existing_location, existing_country in candidates:
        # A location naming no city ("Hong Kong", "Singapore") never rules out a match,
        # so the country must: one employer's ad for two countries is two jobs.
        if same_country(ad.country, existing_country) \
                and same_place(ad.location, existing_location) \
                and is_copy_of_ad(ad.description, existing):
            return job_pk
    return None


def _same_ad(conn, ad: Ad) -> int | None:
    """Rule 4: a recent job with the same title and a near-identical ad under any employer,
    in the same city and country (is_same_ad)."""
    patterns = title_patterns(ad.title)
    if not (ad.description and patterns and ad.title != "Untitled"):
        return None
    # Titles first, narrowed in SQL to those with every one of this title's words (as the
    # same title has); descriptions are only loaded for the same title.
    rows = conn.execute(
        """SELECT id, title FROM jobs
           WHERE first_seen_at > now() - make_interval(days => %s) AND title ILIKE ALL(%s)
           ORDER BY first_seen_at DESC""", (FUZZY_WINDOW_DAYS, patterns)).fetchall()
    close = [pk for pk, other in rows if title_key(other) == title_key(ad.title)]
    if not close:
        return None
    candidates = conn.execute(
        """SELECT id, title, description, location, country FROM jobs WHERE id = ANY(%s)
           ORDER BY first_seen_at DESC""", (close[:50],)).fetchall()
    for job_pk, *other in candidates:
        if is_same_ad(ad, Ad(*other)):
            return job_pk
    return None


def find_duplicate(conn, key: str, fp: str | None, mkey: str | None,
                   ad: Ad) -> tuple[int, bool] | None:
    """Return (job id, is_first_copy) for an existing copy of this posting, if any, by the
    first of the rules in docs/rules.md that matches (_same_copy, _same_content,
    _same_employer_and_title, _same_ad). is_first_copy is True when this is the copy the
    jobs row was created from."""
    found = _same_copy(conn, key)
    if found:
        return found
    job_pk = (_same_content(conn, fp, ad.country)
              or _same_employer_and_title(conn, mkey, ad)
              or _same_ad(conn, ad))
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
    found = find_duplicate(conn, key, fp, mkey,
                           Ad(posting.title, description, posting.location, country))
    if found is None:
        filtered = job_filter(posting) if job_filter else None
        job_pk = conn.execute(
            """INSERT INTO jobs(canonical_key, canonical_url, title, company, location,
                   posted_at, description, raw_json, fingerprint, source, match_key,
                   salary_min, salary_max, salary_estimated, status, filter_reason,
                   filter_kind, country)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (key, url, posting.title, posting.company, posting.location,
             posting.posted_at, description, raw, fp, posting.source, mkey,
             posting.salary_min, posting.salary_max, posting.salary_estimated,
             "filtered" if filtered else "new", filtered and filtered.reason,
             filtered and filtered.kind, country)).fetchone()[0]
    else:
        job_pk, is_first_copy = found
        # Another site's copy keeps the first site's record; its own copy is kept in
        # job_sources and as an observation. A longer description from any site is preferred.
        conn.execute(
            """UPDATE jobs SET raw_json=CASE WHEN %s THEN %s ELSE raw_json END,
                   description=CASE WHEN length(coalesce(%s, ''))
                                             > length(coalesce(description, ''))
                                    THEN %s ELSE description END,
                   canonical_url=coalesce(canonical_url, %s),
                   posted_at=coalesce(posted_at, %s),
                   fingerprint=coalesce(fingerprint, %s),
                   source=coalesce(source, %s),
                   match_key=coalesce(match_key, %s),
                   -- Every SET reads the row as it was: the guess goes with the salary taken.
                   salary_estimated=CASE WHEN salary_min IS NULL THEN %s
                                         ELSE salary_estimated END,
                   salary_min=coalesce(salary_min, %s),
                   salary_max=coalesce(salary_max, %s),
                   country=coalesce(country, %s)
               WHERE id=%s""",
            (is_first_copy, raw, description, description, url, posting.posted_at, fp,
             posting.source, mkey, posting.salary_estimated, posting.salary_min,
             posting.salary_max, country, job_pk))
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


def save_run(record: RunRecord, jobs: list[dict], *, job_filter: JobFilter | None = None,
             country: str | None = None, hit_cap: bool = False) -> int:
    """Save a run's postings (see save_job) and record the run: SUCCEEDED, or PARTIAL if
    Apify stopped it before it finished. Returns how many jobs were new."""
    with db.connect() as conn:
        # Sites run in parallel, and a manual jobflow ingest may overlap the daily one.
        wait_for_imports(conn)
        run_pk = conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status, error,
                   started_at, finished_at, items_seen, cost_usd, search_name, input, hit_cap,
                   posted_within_days, partial)
               VALUES (%(actor_id)s, %(run_id)s, %(dataset_id)s, %(status)s, %(error)s,
                       coalesce(%(started_at)s, now()), coalesce(%(finished_at)s, now()),
                       %(items)s, %(cost_usd)s, %(search_name)s, %(input)s, %(hit_cap)s,
                       %(posted_within_days)s, %(partial)s)
               ON CONFLICT (apify_run_id) DO UPDATE SET dataset_id=EXCLUDED.dataset_id,
                   status=EXCLUDED.status, error=EXCLUDED.error,
                   started_at=EXCLUDED.started_at, finished_at=EXCLUDED.finished_at,
                   items_seen=EXCLUDED.items_seen, cost_usd=EXCLUDED.cost_usd,
                   hit_cap=EXCLUDED.hit_cap, partial=EXCLUDED.partial
               RETURNING id""",
            {**record._row(), "status": "PARTIAL" if record.partial else "SUCCEEDED",
             "error": PARTIAL_ERROR if record.partial else None, "items": len(jobs),
             "hit_cap": hit_cap}).fetchone()[0]
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
# the daily import late. The tracker's header calls an import late after this long too.
RECENT_RUNS = timedelta(hours=36)


def recent_failed_runs(conn) -> list[dict]:
    """Each site with runs that failed in RECENT_RUNS, even if its other runs (or other
    sites') succeeded: {site, failed, total, error}, with the latest failure's error."""
    return conn.cursor(row_factory=dict_row).execute(
        """SELECT split_part(actor_id, ':', 1) AS site,
                  count(*) FILTER (WHERE status <> 'SUCCEEDED') AS failed, count(*) AS total,
                  (array_agg(error ORDER BY created_at DESC)
                       FILTER (WHERE status <> 'SUCCEEDED'))[1] AS error
           FROM scraper_runs WHERE created_at > now() - %s
           GROUP BY 1 HAVING count(*) FILTER (WHERE status <> 'SUCCEEDED') > 0
           ORDER BY 1""", (RECENT_RUNS,)).fetchall()


def recent_capped_runs(conn) -> list[dict]:
    """Each site with runs in RECENT_RUNS that returned as many results as max_results
    allows, so probably left jobs behind: {site, capped, total, searches}, the searches
    being those of the capped runs."""
    return conn.cursor(row_factory=dict_row).execute(
        """SELECT split_part(actor_id, ':', 1) AS site,
                  count(*) FILTER (WHERE hit_cap) AS capped, count(*) AS total,
                  coalesce(array_agg(DISTINCT search_name)
                               FILTER (WHERE hit_cap AND search_name IS NOT NULL),
                           '{}') AS searches
           FROM scraper_runs WHERE created_at > now() - %s AND status = 'SUCCEEDED'
           GROUP BY 1 HAVING bool_or(hit_cap)
           ORDER BY 1""", (RECENT_RUNS,)).fetchall()


# A run whose results couldn't be saved is IMPORT_FAILED; once importing them has failed
# this often (the first import included) it is GAVE_UP, which imports no longer retry, as a
# failure that repeats (a bug, say) would otherwise fail every daily import until Apify
# deleted the results. jobflow ingest --retry-imports still tries those.
IMPORT_FAILED, GAVE_UP = "IMPORT_FAILED", "GAVE_UP"
MAX_IMPORT_ATTEMPTS = 3


def failed_imports(include_given_up: bool = False) -> list[tuple[RunRecord, int]]:
    """The runs that succeeded on Apify (or were stopped part-way) but whose results
    couldn't be saved, oldest first: each run's record, to save it as it would have been,
    and how many times importing it has failed. With include_given_up, those GAVE_UP too."""
    statuses = [IMPORT_FAILED, GAVE_UP] if include_given_up else [IMPORT_FAILED]
    with db.connect() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            """SELECT actor_id, apify_run_id AS run_id, dataset_id, search_name,
                      input AS run_input, started_at, finished_at, cost_usd,
                      posted_within_days, partial, import_attempts
               FROM scraper_runs
               WHERE status = ANY(%s) AND apify_run_id IS NOT NULL AND dataset_id IS NOT NULL
               ORDER BY created_at""", (statuses,)).fetchall()
    return [(RunRecord(**{k: v for k, v in row.items() if k != "import_attempts"}),
             row["import_attempts"]) for row in rows]


def record_failed_run(record: RunRecord, *, status: str, error: str) -> None:
    """Record a run that failed, wasn't started, or whose results couldn't be saved (each
    of those counts as an attempt at importing them, see failed_imports)."""
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, error, cost_usd, search_name, input,
                   posted_within_days, partial, import_attempts)
               VALUES (%(actor_id)s, %(run_id)s, %(dataset_id)s, %(status)s, %(started_at)s,
                       coalesce(%(finished_at)s, now()), %(error)s, %(cost_usd)s,
                       %(search_name)s, %(input)s, %(posted_within_days)s, %(partial)s,
                       %(attempt)s)
               ON CONFLICT (apify_run_id) DO UPDATE SET status=EXCLUDED.status,
                   finished_at=EXCLUDED.finished_at, error=EXCLUDED.error,
                   cost_usd=EXCLUDED.cost_usd,
                   import_attempts=scraper_runs.import_attempts + EXCLUDED.import_attempts""",
            {**record._row(), "status": status, "error": error[:4000],
             "attempt": int(status in (IMPORT_FAILED, GAVE_UP))})
