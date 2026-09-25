from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def profile() -> dict:
    path = Path(os.environ.get("JOBFLOW_PROFILE", ROOT / "profile.toml")).expanduser()
    if not path.exists():
        example = ROOT / "profile.example.toml"
        raise RuntimeError(
            f"Profile not found at {path}. Copy {example} to {ROOT / 'profile.toml'} "
            "and set paths.cv_directory."
        )
    with path.open("rb") as stream:
        return tomllib.load(stream)


def configured_path(section: str, name: str, default: str) -> Path:
    settings = profile()
    value = os.environ.get(name.upper()) or settings.get(section, {}).get(name, default)
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def cv_directory() -> Path:
    return configured_path("paths", "cv_directory", "./private/cv")


def tailored_directory() -> Path:
    return configured_path("paths", "tailored_directory", "./output/tailored-cvs")


def interview_directory() -> Path:
    return configured_path("paths", "interview_directory", "./output/interviews")
