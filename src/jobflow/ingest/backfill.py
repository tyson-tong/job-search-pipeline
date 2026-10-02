"""Filling in a table or column once, when a migration adds it: the follow-ups a
migration names with "-- Then: NAME" (see db.migrate)."""
from __future__ import annotations

from collections.abc import Callable

from .dedupe import identity
from .runs import country_from_location, run_country, search_url_country
from .sources import normalise


def backfill_sources(conn) -> int:
    """Record every earlier sighting's site copy in job_sources (run once, when the
    table is created: the migration only creates it)."""
    rows = conn.execute("SELECT job_id, observed_json FROM job_observations").fetchall()
    before = conn.execute("SELECT count(*) FROM job_sources").fetchone()[0]
    for job_pk, raw in rows:
        # Older observations may already have had their full records pruned. An empty
        # placeholder would normalize to the same synthetic "unknown" identity for every
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


# The follow-ups by the name a migration gives them.
FOLLOW_UPS: dict[str, Callable] = {f.__name__: f for f in (backfill_sources, backfill_countries)}
