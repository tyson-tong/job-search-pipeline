"""Saving the tracker's changes: validated first, then all or nothing, and not over a
change made elsewhere since the page loaded the job."""
from __future__ import annotations

from datetime import date

from .. import db
from ..db import EDITABLE_STATUSES, set_job_state
from .queries import MAX_NOTE, VERSION_SQL


class InvalidChange(ValueError):
    pass


class Conflict(Exception):
    """Some jobs changed after the page loaded them; nothing was saved."""

    def __init__(self, conflicts: list[dict]):
        super().__init__(f"{len(conflicts)} job(s) changed since you loaded them")
        self.conflicts = conflicts


def validate_changes(raw: object) -> list[dict]:
    """Check a save request before anything is written; raises InvalidChange."""
    if not isinstance(raw, list) or not raw:
        raise InvalidChange("Nothing to save")
    changes, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidChange("Each change must be an object")
        unknown = set(item) - {"id", "status", "note", "follow_up_on", "expected_version"}
        if unknown:
            raise InvalidChange(f"Unknown field(s): {', '.join(sorted(unknown))}")
        job_id = item.get("id")
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id in seen:
            raise InvalidChange(f"Invalid or repeated job id: {job_id!r}")
        seen.add(job_id)
        change: dict = {"id": job_id}
        if "status" in item:
            if item["status"] not in EDITABLE_STATUSES:
                raise InvalidChange(f"Job {job_id}: status must be one of "
                                    f"{', '.join(EDITABLE_STATUSES)}")
            change["status"] = item["status"]
        if "note" in item:
            note = item["note"]
            if not isinstance(note, str) or len(note) > MAX_NOTE:
                raise InvalidChange(f"Job {job_id}: a note must be text of at most {MAX_NOTE} "
                                    "characters")
            if note.strip():
                change["note"] = note
        if "follow_up_on" in item:
            value = item["follow_up_on"]
            try:
                change["follow_up_on"] = date.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                raise InvalidChange(f"Job {job_id}: follow-up date must be YYYY-MM-DD") from None
        if len(change) == 1:
            raise InvalidChange(f"Job {job_id}: nothing to change")
        version = item.get("expected_version")
        if version is not None:
            if not isinstance(version, str) or not version.isdigit():
                raise InvalidChange(f"Job {job_id}: invalid expected_version")
            change["expected_version"] = version
        changes.append(change)
    return changes


def find_conflicts(conn, changes: list[dict]) -> list[dict]:
    """Jobs whose tracking version differs from the one the page loaded. Locks the rows,
    so nothing can change them between this check and the save."""
    expected = {c["id"]: c["expected_version"] for c in changes if "expected_version" in c}
    if not expected:
        return []
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company, j.status, a.follow_up_on,
                   {VERSION_SQL} AS version,
                   (SELECT via FROM status_history h WHERE h.job_id = j.id
                    ORDER BY changed_at DESC LIMIT 1) AS last_via
            FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
            WHERE j.id = ANY(%s) FOR UPDATE OF j""", (list(expected),)).fetchall()
    # Each job's version now, so the page can save over it once you've seen the change.
    return [{"id": pk, "title": title, "company": company, "status": status,
             "follow_up_on": follow_up_on, "changed_via": via, "version": version}
            for pk, title, company, status, follow_up_on, version, via in rows
            if version != expected[pk]]


def save_changes(raw: object, force: bool = False) -> list[int]:
    """Validate, check for conflicting edits made elsewhere (unless force), then apply
    every change in one transaction: all are saved or none."""
    changes = validate_changes(raw)
    with db.connect() as conn:
        conflicts = [] if force else find_conflicts(conn, changes)
        if conflicts:
            raise Conflict(conflicts)  # leaving the block rolls back
        for change in changes:
            fields = {k: v for k, v in change.items() if k not in ("id", "expected_version")}
            set_job_state(conn, change["id"], via="web", **fields)
    return [change["id"] for change in changes]
