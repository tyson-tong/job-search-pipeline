"""jobflow clean-up: deleting the output folders of jobs you're done with."""
from __future__ import annotations

import shutil

from . import db, settings

# The statuses whose jobs' output folders clean-up deletes, with how it names them. Only a
# job moved to Not interested from My jobs has a folder: Inbox jobs are never worked on.
# Such a job is archived 2 weeks later, still with its folder.
CLEAN_UP_STATUSES = {"not_interested": "Not interested", "withdrawn": "Withdrawn",
                     "declined": "Declined", "rejected": "Rejected", "archived": "Archived"}


def clean_up() -> None:
    """Delete the output folders (CVs, letters, reviews, briefings) of jobs that are
    Not interested, Withdrawn, Declined, Rejected, or Archived, once you confirm. The jobs,
    their notes, and their history stay in the database."""
    root = settings.output_directory()

    def closed_jobs(ids: list[int] | None = None) -> list[tuple[int, str]]:
        with db.connect() as conn:
            return conn.execute(
                "SELECT id, status FROM jobs WHERE status = ANY(%s) "
                "AND (%s::bigint[] IS NULL OR id = ANY(%s)) ORDER BY id",
                (list(CLEAN_UP_STATUSES), ids, ids)).fetchall()

    # A job's folder is output/<company>/<role>-<job id>, found by its id, so a folder is
    # still found if the company or title has changed since it was written.
    folders = [(folder, job_id, status) for job_id, status in closed_jobs()
               for folder in sorted(root.glob(f"*/*-{job_id}")) if folder.is_dir()]
    if not folders:
        print("Nothing to clean up: no Not interested, Withdrawn, Declined, Rejected, or "
              f"Archived job has a folder in {root}")
        return
    for folder, _, status in folders:
        print(f"  {CLEAN_UP_STATUSES[status]:<14}  {folder.relative_to(root)}")
    try:
        answer = input(f"Delete these {len(folders)} folder{'s' if len(folders) != 1 else ''} "
                       "and everything in them? This can't be undone. (Y/N) ")
    except EOFError:
        answer = ""
    if answer.strip().casefold() not in ("y", "yes"):
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
    print(f"Deleted {deleted} folder{'s' if deleted != 1 else ''}")
