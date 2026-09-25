"""Shared per-job artifact locations."""
from __future__ import annotations

from pathlib import Path

from ..settings import output_directory
from .cvs import slug


def job_directory(job_id: int, job: dict, base: Path | None = None) -> Path:
    """Return output/<company>/<role>-<job-id> for every artifact for this job."""
    return ((base or output_directory()) / slug(job.get("company"))
            / f"{slug(job.get('title'))}-{job_id}")
