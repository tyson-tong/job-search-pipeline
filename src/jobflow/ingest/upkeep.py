"""Upkeep after each import (and when the tracker starts): archiving jobs you're done
with, and dropping scraped records no longer needed."""
from __future__ import annotations

from datetime import date, timedelta

from .. import db, settings
from .dedupe import FUZZY_WINDOW_DAYS
from .store import wait_for_imports

# A filtered job this long past its posted date (or the date first seen, if later), or a job
# this long after you marked it Not interested, is archived (see archive_old_jobs).
ARCHIVE_AFTER = timedelta(days=14)
# An Inbox job you haven't triaged is archived once it was posted this long ago and has
# been in the Inbox this long (a job moved back to the Inbox starts again).
INBOX_ARCHIVE_AFTER = timedelta(days=30)
# How long each sighting keeps its full scraped record (see prune_sightings).
KEEP_SIGHTING_RECORDS = timedelta(days=30)
# How long an archived job keeps its description and scraped record (see
# prune_archived_jobs): as long as a repost with a new ID is matched by its ad
# (dedupe.FUZZY_WINDOW_DAYS, counted from first seen), so clearing it never loses a merge;
# the job was archived after it was first seen, so this is past that window. Similar-job
# notes look further back (similar.POSSIBLE_WINDOW_DAYS), but only notes are lost: a
# cleared job has no ad to compare.
KEEP_ARCHIVED_RECORDS = timedelta(days=FUZZY_WINDOW_DAYS)


def archive_old_jobs(today: date | None = None) -> int:
    """Move jobs out of the Filtered, Not interested, and Inbox tabs, to status 'archived':
    a filtered job ARCHIVE_AFTER past its posted date or the date first seen, whichever is
    later, a job ARCHIVE_AFTER after you marked it Not interested, and an Inbox job you
    never triaged, posted and in the Inbox for INBOX_ARCHIVE_AFTER. They stay in the database
    and still match as duplicates, so a repost stays out of the Inbox; the tracker shows
    them in its Archived tab. Returns how many were archived."""
    today = today or settings.today()
    with db.connect() as conn:
        # jobflow web archives as it starts, perhaps while the daily import is saving a run.
        wait_for_imports(conn)
        return conn.execute(
            """WITH moved AS (
                   UPDATE jobs j SET status='archived', status_updated_at=now()
                   FROM (SELECT id, status FROM jobs
                         -- A filtered job first seen well after it was posted (in a
                         -- month-long window, say) still gets its 2 weeks in the Filtered tab.
                         WHERE (status = 'filtered'
                                AND greatest(posted_at, first_seen_at::date) < %(cutoff)s)
                            OR (status = 'not_interested' AND status_updated_at::date < %(cutoff)s)
                            OR (status = 'new'
                                AND coalesce(posted_at, first_seen_at::date) < %(inbox_cutoff)s
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
    with db.connect() as conn:
        return conn.execute(
            """UPDATE job_observations SET observed_json = NULL
               WHERE observed_json IS NOT NULL AND observed_at < now() - %s""",
            (KEEP_SIGHTING_RECORDS,)).rowcount


def prune_archived_jobs() -> int:
    """Clear the description and scraped record of jobs archived more than
    KEEP_ARCHIVED_RECORDS ago. The job itself stays: its sites' IDs, its fingerprint, and
    its title, so a repost is still recognised. Returns how many jobs were cleared."""
    with db.connect() as conn:
        wait_for_imports(conn)   # a repost of one of these jobs may be being saved
        return conn.execute(
            """UPDATE jobs SET description = NULL, raw_json = NULL
               WHERE status = 'archived' AND status_updated_at < now() - %s
                 AND (description IS NOT NULL OR raw_json IS NOT NULL)""",
            (KEEP_ARCHIVED_RECORDS,)).rowcount
