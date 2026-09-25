"""Loading your CV files, and helpers shared by matching and tailoring."""
from __future__ import annotations

import hashlib
import re

from ..settings import cv_directory

JOB_CHAR_LIMIT = 24000
CV_CHAR_LIMIT = 28000


def cv_sources() -> dict[str, str]:
    directory = cv_directory()
    return {p.stem.removeprefix("CV_"): p.read_text()
            for p in sorted(directory.glob("CV_*.yaml"))}


def cv_digest(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def normalise_text(text: object) -> str:
    """Lower-case words only, for comparing CV text regardless of punctuation or markup."""
    return re.sub(r"[^a-z0-9+#]+", " ", str(text or "").casefold()).strip()


def slug(value: str | None) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "unknown").lower()).strip("-")
    return value[:48] or "unknown"
