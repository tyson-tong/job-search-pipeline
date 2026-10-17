"""jobflow clean-up: deleting the output folders of jobs you're done with."""
from __future__ import annotations

import shutil

from . import db, settings
from .text import confirmed, plural

# Of the statuses whose jobs' folders clean-up deletes (db.CLEAN_UP_STATUSES), only a job
# moved to Not interested from My jobs has a folder: Inbox jobs are never worked on. Such a
# job is archived 2 weeks later, still with its folder. A job you applied for keeps its
# folder whatever its status: it holds the CV and letter you sent, for a follow-up or for
# applying again. (Rejected and Declined jobs always count as applied for.)


def clean_up() -> None:
    """Delete the output folders (CVs, letters, reviews, briefings) of jobs that are
    Not interested, Withdrawn, or Archived and that you never applied for, once you confirm.
    The jobs, their notes, and their history stay in the database."""
    root = settings.output_directory()

    def closed_jobs(ids: list[int] | None = None) -> list[tuple[int, str]]:
        with db.connect() as conn:
            return conn.execute(
                "SELECT id, status FROM jobs j WHERE status = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM applications a "
                "                WHERE a.job_id = j.id AND a.applied_at IS NOT NULL) "
                "AND (%s::bigint[] IS NULL OR id = ANY(%s)) ORDER BY id",
                (list(db.CLEAN_UP_STATUSES), ids, ids)).fetchall()

    # A job's folder is output/<company>/<role>-<job id>, found by its id, so a folder is
    # still found if the company or title has changed since it was written. The output
    # folder is read once: closed jobs only accumulate, so one search per job would slow
    # every clean-up down.
    closed = dict(closed_jobs())
    folders = [(folder, job_id, closed[job_id]) for folder in sorted(root.glob("*/*-*"))
               if (job_id := _job_id(folder)) in closed and folder.is_dir()]
    if not folders:
        print("Nothing to clean up: no Not interested, Withdrawn, or Archived job you never "
              f"applied for has a folder in {root}")
        return
    for folder, _, status in folders:
        print(f"  {status.replace('_', ' ').capitalize():<14}  {folder.relative_to(root)}")
    if not confirmed(f"Delete these {plural(len(folders), 'folder')} and everything in them? "
                     "This can't be undone."):
        print("Nothing deleted")
        return
    # A job may have moved on (in the tracker, say) while you were deciding: keep its folder.
    still_closed = {job_id for job_id, _ in closed_jobs([job_id for _, job_id, _ in folders])}
    deleted = 0
    for folder, job_id, _ in folders:
        if job_id not in still_closed:
            print(f"  kept {folder.relative_to(root)}: its job's status has changed since")
            continue
        shutil.rmtree(folder)
        deleted += 1
        if not any(folder.parent.iterdir()):
            folder.parent.rmdir()   # the company's folder, now empty
    print(f"Deleted {plural(deleted, 'folder')}")


def _job_id(folder) -> int | None:
    """The job id a job folder's name ends with ("data-analyst-42"), or None."""
    tail = folder.name.rpartition("-")[2]
    return int(tail) if tail.isascii() and tail.isdecimal() else None
