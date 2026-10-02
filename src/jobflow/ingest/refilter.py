"""Applying a searches change to jobs already saved (jobflow update-searches): filtering
Inbox jobs the searches now exclude, and bringing back Filtered jobs they would now keep."""
from __future__ import annotations

from .. import db
from .filters import excluded_reason, filter_reason
from .sources import normalise

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
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
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
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
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
