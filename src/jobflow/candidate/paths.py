"""Shared per-job artifact locations."""
from __future__ import annotations

import re
from pathlib import Path

from ..settings import output_directory


def job_directory(job_id: int, job: dict, base: Path | None = None) -> Path:
    """Return output/<company>/<role>-<job-id> for every artifact for this job. A company
    or title written only in letters outside a-z once made an "unknown" folder name: a job
    whose folder is still there keeps it, so its drafts are found and not paid for again."""
    base = base or output_directory()
    company, title = job.get("company"), job.get("title")
    folder = base / slug(company) / f"{slug(title)}-{job_id}"
    old = base / _ascii_slug(company) / f"{_ascii_slug(title)}-{job_id}"
    return old if old != folder and old.is_dir() else folder


def slug(value: str | None) -> str:
    """A company or job title as a folder name: lower-case letters, digits, and dashes.
    Letters outside a-z are kept only in a name with none of those ("滙豐銀行" stays
    itself), so every other name's folder stays where earlier runs wrote it."""
    text = (value or "").lower()
    found = re.sub(r"[^a-z0-9]+", "-", text).strip("-") or re.sub(r"[\W_]+", "-", text).strip("-")
    return found[:48] or "unknown"


def _ascii_slug(value: str | None) -> str:
    """slug as it was before it kept other letters."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:48] or "unknown"
