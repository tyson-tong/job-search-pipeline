"""Applying a searches change to jobs already saved (jobflow update-searches): filtering
Inbox jobs the searches now filter, and bringing back Filtered jobs they would now keep."""
from __future__ import annotations

from .. import db
from .filters import excluded_reason, filter_reason
from .sources import normalise

# Each job, its stored record, and the search whose run first found it and that run's site.
_JOBS_SQL = """
    SELECT j.id, j.title, j.company, j.filter_reason, j.raw_json, j.country,
           j.first_seen_at::date, r.search_name, split_part(r.actor_id, ':', 1)
    FROM jobs j
    LEFT JOIN LATERAL (SELECT scraper_run_id FROM job_observations o
                       WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
    LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id"""
# A job you moved out of Filtered yourself (and so, if it's in the Inbox now, one you chose to
# keep), which update-searches leaves be.
_MOVED_BACK_SQL = """EXISTS (SELECT 1 FROM status_history h WHERE h.job_id = j.id
                            AND h.from_status = 'filtered'
                            AND h.via NOT IN ('update-searches', 'archive'))"""
# The reasons a [defaults] search can judge again: it has only the exclusions.
EXCLUSION_REASONS = ("title contains ", "employer matches ")


def _judge(row: tuple, by_name: dict, fallback) -> tuple[bool, str | None]:
    """(whether the job can be judged, why the searches would filter it now, or None).
    A job is judged by every filter an import applies (filter_reason), from its stored
    record, by the search that found it, as on the day it was first seen: a posted-date
    window filters a job posted too long before its import, not one that has merely aged
    since. A job whose search has been renamed or removed is judged by `fallback` (the
    [defaults] exclusions), by its title and employer, the only filters those have."""
    _, title, company, _, raw, country, first_seen, search_name, site = row
    search = by_name.get(search_name)
    if search and raw is not None:
        return True, filter_reason(normalise(raw, country), search, site, today=first_seen)
    if fallback:
        return True, excluded_reason(title, company, fallback)
    return False, None


def filter_inbox_jobs(searches: list, dry_run: bool = False,
                      fallback=None) -> tuple[list[dict], int]:
    """Filter jobs already in the Inbox that the search which found them would filter now
    (see _judge): each becomes 'filtered', with the reason an import would record, and the
    change is recorded in status_history. Only New jobs are touched, and never one you moved
    back to the Inbox from Filtered. Returns (the jobs filtered, the number of Inbox jobs
    checked); with dry_run, nothing is changed."""
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
        rows = conn.execute(f"""{_JOBS_SQL} WHERE j.status = 'new' AND NOT {_MOVED_BACK_SQL}
                                ORDER BY j.id""").fetchall()
        matched = []
        for row in rows:
            _, reason = _judge(row, by_name, fallback)
            if reason:
                matched.append({"id": row[0], "title": row[1], "company": row[2],
                                "reason": reason})
        if not dry_run:   # a job that left the Inbox meanwhile is skipped, and not counted
            matched = [job for job in matched if db.move_job(
                conn, job["id"], "new", "filtered", "update-searches", job["reason"])]
    return matched, len(rows)


def restore_filtered_jobs(searches: list, dry_run: bool = False,
                          fallback=None) -> tuple[list[dict], int]:
    """Bring Filtered jobs (not yet archived) back to the Inbox when the search that found
    them wouldn't filter them now (see _judge). A job judged by `fallback` comes back only
    if it was filtered by an excluded title term or employer: [defaults] can't judge the
    other filters. Returns (the jobs brought back, with the reason they were filtered, and
    the number of Filtered jobs checked); with dry_run, nothing is changed."""
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
        rows = conn.execute(f"{_JOBS_SQL} WHERE j.status = 'filtered' ORDER BY j.id").fetchall()
        restored = []
        for row in rows:
            judged, still = _judge(row, by_name, fallback)
            reason, by_search = row[3], row[7] in by_name and row[4] is not None
            if not by_search and not (reason or "").startswith(EXCLUSION_REASONS):
                continue   # [defaults] can't judge why it was filtered
            if judged and not still:
                restored.append({"id": row[0], "title": row[1], "company": row[2],
                                 "reason": reason})
        if not dry_run:
            restored = [job for job in restored if db.move_job(
                conn, job["id"], "filtered", "new", "update-searches")]
    return restored, len(rows)
