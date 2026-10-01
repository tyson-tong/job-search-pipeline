"""The tracker's CSV export."""
from __future__ import annotations

import csv
import io

from .. import db
from .queries import list_jobs


def _csv_cell(value: object) -> str:
    """Text for a CSV cell. Scraped text starting with = + - @ would run as a formula in
    a spreadsheet, so it's prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(filters: dict, ids: set[int] | None = None) -> str:
    """The jobs the filters find as CSV, or only those of them in ids (a page's rows)."""
    jobs = list_jobs({**filters, "limit": None})   # LIMIT NULL: every job, however many
    if ids is not None:
        jobs = [job for job in jobs if job["id"] in ids]
    with db.connect() as conn:
        notes = dict(conn.execute("SELECT job_id, notes FROM applications WHERE job_id = ANY(%s)",
                                  ([j["id"] for j in jobs],)).fetchall())
    out = io.StringIO()
    columns = ["id", "status", "title", "company", "location", "country", "score",
               "cv_variant", "posted_at", "first_seen_at", "salary_min", "salary_max", "currency",
               "sites", "url", "applied_at", "follow_up_on", "notes"]
    writer = csv.writer(out)
    writer.writerow(columns)
    for job in jobs:
        job = {**job, "sites": " ".join(job["sites"]), "notes": notes.get(job["id"]) or ""}
        writer.writerow([_csv_cell(job.get(c)) for c in columns])
    return out.getvalue()
