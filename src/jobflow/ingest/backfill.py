"""Filling in a table or column once, when a migration adds it: the follow-ups a
migration names with "-- Then: NAME" (see db.migrate)."""
from __future__ import annotations

from collections.abc import Callable

from .dedupe import fingerprint, identity, match_key
from .runs import country_from_location, run_country, search_url_country
from .sources import Posting, normalise


def backfill_sources(conn) -> int:
    """Record every earlier sighting's site copy in job_sources (run once, when the
    table is created: the migration only creates it)."""
    rows = conn.execute("SELECT job_id, observed_json FROM job_observations").fetchall()
    before = conn.execute("SELECT count(*) FROM job_sources").fetchone()[0]
    for job_pk, raw in rows:
        # Older observations may already have had their full records pruned. An empty
        # placeholder would normalise to the same synthetic "unknown" identity for every
        # such row, incorrectly attaching one fake source to an arbitrary job.
        if not isinstance(raw, dict) or not raw:
            continue
        posting = normalise(raw)
        key, url = identity(posting)
        conn.execute(
            """INSERT INTO job_sources(source_key, job_id, source, url)
               VALUES (%s,%s,%s,%s) ON CONFLICT (source_key) DO NOTHING""",
            (key, job_pk, posting.source, url))
    return conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] - before


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


def backfill_title_keys(conn) -> int:
    """Work out match_key and fingerprint again for jobs whose title has a + or #, which
    titles once lost ("C++ Developer" and "C# Developer" were one title): run once, when
    titles began keeping them (dedupe.title_key). Only these jobs' keys change. The
    fingerprint is made, as when the job was saved, from its first copy's record; a job
    whose record was already cleared keeps the one it has."""
    rows = conn.execute("""SELECT id, title, company, location, raw_json, country FROM jobs
                           WHERE title ~ '[+#]'""").fetchall()
    for job_pk, title, company, location, raw, country in rows:
        key = match_key(Posting(source="", source_id=None, url=None, title=title,
                                company=company, location=location, posted_at=None,
                                description=""))
        content_fp = fingerprint(normalise(raw, country)) if isinstance(raw, dict) and raw else None
        conn.execute("UPDATE jobs SET match_key=%s, fingerprint=coalesce(%s, fingerprint) "
                     "WHERE id=%s", (key, content_fp, job_pk))
    return len(rows)


def backfill_salary_estimates(conn) -> int:
    """Mark the salaries whose pay period was guessed from the amount (run once, when the
    column is added), read again from each job's first copy's record. A job whose record
    was already cleared stays unmarked."""
    rows = conn.execute("""SELECT id, raw_json, country FROM jobs
                           WHERE salary_min IS NOT NULL AND raw_json IS NOT NULL""").fetchall()
    marked = [job_pk for job_pk, raw, country in rows
              if isinstance(raw, dict) and normalise(raw, country).salary_estimated]
    if marked:
        conn.execute("UPDATE jobs SET salary_estimated = true WHERE id = ANY(%s)", (marked,))
    return len(marked)


# The follow-ups by the name a migration gives them.
FOLLOW_UPS: dict[str, Callable] = {f.__name__: f for f in (
    backfill_sources, backfill_countries, backfill_salary_estimates, backfill_title_keys)}
