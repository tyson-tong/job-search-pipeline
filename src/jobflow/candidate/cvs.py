"""Loading your CV files, and helpers shared by matching and tailoring."""
from __future__ import annotations

import hashlib
import re
import sys
from functools import lru_cache
from pathlib import Path

from ..settings import cv_directory

JOB_CHAR_LIMIT = 24000
CV_CHAR_LIMIT = 28000
# The CV formats read, in order of preference: when one CV is there in several formats
# (CV_dev.yaml and an exported CV_dev.pdf), the first is used and the others skipped.
CV_FORMATS = (".yaml", ".yml", ".tex", ".docx", ".pdf")
YAML_FORMATS = (".yaml", ".yml")


def variant_name(path: Path) -> str:
    """The name --cv takes: the file name without its extension and a leading "CV_"."""
    return re.sub(r"^cv[_\- ]+", "", path.stem, flags=re.I) or path.stem


def cv_files() -> dict[str, Path]:
    """{variant: file} for each file in cv_directory whose name contains "cv" (any case),
    in one of CV_FORMATS. Hidden files and Word's "~$" lock files are ignored."""
    directory = cv_directory()
    if not directory.is_dir():
        raise RuntimeError(f"CV folder {directory} not found; create it and put your CVs in "
                           "it, or set paths.cv_directory in config/profile.toml")
    candidates = [p for p in directory.iterdir()
                  if p.is_file() and p.suffix.casefold() in CV_FORMATS
                  and "cv" in p.stem.casefold() and not p.name.startswith((".", "~$"))]
    files: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda p: (CV_FORMATS.index(p.suffix.casefold()), p.name)):
        files.setdefault(variant_name(path), path)
    return dict(sorted(files.items()))


def is_yaml(path: Path) -> bool:
    return path.suffix.casefold() in YAML_FORMATS


def cv_sources() -> dict[str, str]:
    """{variant: CV text}. A file with no readable text (a scanned PDF, say) is skipped
    with a warning."""
    sources = {}
    for variant, path in cv_files().items():
        stat = path.stat()
        text = cv_text(path, stat.st_mtime_ns, stat.st_size)
        if text.strip():
            sources[variant] = text
    return sources


@lru_cache(maxsize=64)
def cv_text(path: Path, mtime_ns: int = 0, size: int = 0) -> str:
    """A CV file's text: YAML as it is, the body of a .tex without its commands, and the
    text of a .docx or .pdf. Cached until the file changes (mtime_ns and size)."""
    suffix = path.suffix.casefold()
    if suffix in YAML_FORMATS:
        text = path.read_text()
    elif suffix == ".tex":
        text = path.read_text()
        body = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", text, re.S)
        text = re.sub(r"(?<!\\)%.*", "", body[1] if body else text)
        text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
        text = re.sub(r"[{}]|\\(?=[^a-zA-Z])", "", text)
    elif suffix == ".docx":
        from docx import Document

        doc = Document(path)
        lines = [p.text for p in doc.paragraphs]
        lines += [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
        text = "\n".join(lines)
    else:
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if not text.strip():
        print(f"warning: no text could be read from {path.name} (a scanned PDF?); skipped",
              file=sys.stderr)
    return text


def cv_digest(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def normalise_text(text: object) -> str:
    """Lower-case words only, for comparing CV text regardless of punctuation or markup."""
    return re.sub(r"[^a-z0-9+#]+", " ", str(text or "").casefold()).strip()


def slug(value: str | None) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "unknown").lower()).strip("-")
    return value[:48] or "unknown"
