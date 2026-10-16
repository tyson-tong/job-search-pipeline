"""Applying a searches change to jobs already saved (jobflow update-searches): filtering
Inbox jobs the searches now filter, and bringing back Filtered jobs they would now keep."""
from __future__ import annotations

from typing import Any

from psycopg.rows import namedtuple_row

from .. import db
from .filters import EXCLUSIONS, POSTED, Filtered, excluded_reason, filter_reason
from .sources import normalise

# Each job, its stored record, and the search whose run first found it and that run's site.
_JOBS_SQL = """
    SELECT j.id, j.title, j.company, j.filter_reason, j.filter_kind, j.raw_json, j.country,
           j.first_seen_at::date AS first_seen, r.search_name,
           split_part(r.actor_id, ':', 1) AS site
    FROM jobs j
    LEFT JOIN LATERAL (SELECT scraper_run_id FROM job_observations o
                       WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
    LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id"""
# A job you moved out of Filtered or Archived yourself (and so, if it's in the Inbox now, one
# you chose to keep), which update-searches leaves be.
_MOVED_BACK_SQL = """EXISTS (SELECT 1 FROM status_history h WHERE h.job_id = j.id
                            AND h.from_status IN ('filtered', 'archived')
                            AND h.via NOT IN ('update-searches', 'archive'))"""


def _jobs(conn, where: str) -> list[Any]:
    """The jobs _JOBS_SQL finds with this condition, as rows read by column name."""
    return conn.cursor(row_factory=namedtuple_row).execute(
        f"{_JOBS_SQL} WHERE {where} ORDER BY j.id").fetchall()


def _by_search(job, by_name: dict) -> bool:
    """Whether the search that found the job can judge it: it still exists, and the job
    still has its record."""
    return job.search_name in by_name and job.raw_json is not None


def _judge(job, by_name: dict, fallback) -> tuple[bool, Filtered | None]:
    """(whether the job can be judged, why the searches would filter it now, or None).
    A job is judged by every filter an import applies (filter_reason), from its stored
    record, by the search that found it, as on the day it was first seen: a posted-date
    window filters a job posted too long before its import, not one that has merely aged
    since. A job whose search has been renamed or removed is judged by `fallback` (the
    [defaults] exclusions), by its title and employer, the only filters those have."""
    if _by_search(job, by_name):
        return True, filter_reason(normalise(job.raw_json, job.country),
                                   by_name[job.search_name], job.site, today=job.first_seen)
    if fallback:
        return True, excluded_reason(job.title, job.company, fallback)
    return False, None


def filter_inbox_jobs(searches: list, dry_run: bool = False,
                      fallback=None) -> tuple[list[dict], int]:
    """Filter jobs already in the Inbox that the search which found them would filter now
    (see _judge): each becomes 'filtered', with the reason an import would record, and the
    change is recorded in status_history. Only New jobs are touched, and never one you moved
    back to the Inbox from Filtered or Archived. Returns (the jobs filtered, the number of
    Inbox jobs checked); with dry_run, nothing is changed."""
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
        jobs = _jobs(conn, f"j.status = 'new' AND NOT {_MOVED_BACK_SQL}")
        matched = []
        for job in jobs:
            _, filtered = _judge(job, by_name, fallback)
            if filtered:
                matched.append({"id": job.id, "title": job.title, "company": job.company,
                                "reason": filtered.reason, "kind": filtered.kind})
        if not dry_run:   # a job that left the Inbox meanwhile is skipped, and not counted
            matched = [job for job in matched if db.move_job(
                conn, job["id"], "new", "filtered", "update-searches", job["reason"],
                job["kind"])]
    return matched, len(jobs)


def restore_filtered_jobs(searches: list, dry_run: bool = False,
                          fallback=None) -> tuple[list[dict], int]:
    """Bring Filtered jobs (not yet archived) back to the Inbox when the search that found
    them wouldn't filter them now (see _judge). A job judged by `fallback` comes back only
    if it was filtered by an excluded title term or employer: [defaults] can't judge the
    other filters. A job filtered for its posted date never comes back: the window it was
    judged by may have been a one-off (ingest --posted-within-days) or since narrowed, and
    the site wouldn't list it under the search's window now either. Returns (the jobs
    brought back, with the reason they were filtered, and the number of Filtered jobs
    checked); with dry_run, nothing is changed."""
    by_name = {search.name: search for search in searches}
    with db.connect() as conn:
        jobs = _jobs(conn, "j.status = 'filtered'")
        restored = []
        for job in jobs:
            if job.filter_kind == POSTED:
                continue   # the window isn't stored, so it can't be judged again
            if not _by_search(job, by_name) and job.filter_kind not in EXCLUSIONS:
                continue   # [defaults] can't judge why it was filtered
            judged, still = _judge(job, by_name, fallback)
            if judged and not still:
                restored.append({"id": job.id, "title": job.title, "company": job.company,
                                 "reason": job.filter_reason})
        if not dry_run:
            restored = [job for job in restored if db.move_job(
                conn, job["id"], "filtered", "new", "update-searches")]
    return restored, len(jobs)
