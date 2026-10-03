"""Shared per-job artifact locations."""
from __future__ import annotations

import re
from pathlib import Path

from ..settings import output_directory


def job_directory(job_id: int, job: dict, base: Path | None = None) -> Path:
    """Return output/<company>/<role>-<job-id> for every artifact for this job."""
    return ((base or output_directory()) / slug(job.get("company"))
            / f"{slug(job.get('title'))}-{job_id}")


def slug(value: str | None) -> str:
    """A company or job title as a folder name: lower-case letters, digits, and dashes."""
    value = re.sub(r"[^a-z0-9]+", "-", (value or "unknown").lower()).strip("-")
    return value[:48] or "unknown"
