"""Your settings: config/.env (never overriding real environment variables, so GitHub's
secrets win), config/profile.toml (CVs, folders, scoring weights), and the timezone every
date is compared in."""
from __future__ import annotations

import math
import os
import tomllib
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
# Hosts on this machine: the only ones a database or LLM server may be reached at
# unencrypted.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


# Personal files created from the templates shipped with the repository, in the order
# they're usually filled in. .env holds secrets, so only its owner may read it.
INIT_FILES = ((".env.example", ".env", 0o600),
              ("profile.example.toml", "profile.toml", 0o644),
              ("searches.example.toml", "searches.toml", 0o644))


def init_files(root: Path = ROOT) -> list[tuple[str, bool]]:
    """Copy each missing personal file from its template, and create cv/ for your
    CVs (ignored by Git). Existing files are never touched. Returns (file name, created)
    pairs."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (root / "cv").mkdir(parents=True, exist_ok=True)
    results = []
    for template, target, mode in INIT_FILES:
        source, destination = config_dir / template, config_dir / target
        if destination.exists():
            results.append((target, False))
            continue
        if not source.exists():
            raise RuntimeError(f"Template {source} is missing; run this from the repository")
        # "x" mode fails rather than overwrite if the file appeared in the meantime.
        with destination.open("x") as out:
            out.write(source.read_text())
        destination.chmod(mode)
        results.append((target, True))
    return results


def env_path() -> Path:
    """config/.env in this checkout, where jobflow init creates it."""
    return CONFIG_DIR / ".env"


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env without overriding variables already set.

    Real environment variables (for example GitHub Actions secrets) take precedence,
    and empty values are skipped so they don't mask defaults.
    """
    path = path or env_path()
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        # A quoted value is what's inside the quotes, so it may hold " #" and be followed
        # by a comment; an unquoted one ends at a " #" comment. A quote never closed is a
        # typo: the value is what follows it, not the quote too (a key starting with ").
        quoted = value[:1] in ("'", '"')
        closing = value.find(value[0], 1) if quoted else -1
        if closing > 0:
            value = value[1:closing]
        else:
            value = value[int(quoted):].split(" #", 1)[0].strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def profile_path() -> Path:
    """JOBFLOW_PROFILE, else config/profile.toml in this checkout."""
    if os.environ.get("JOBFLOW_PROFILE"):
        return Path(os.environ["JOBFLOW_PROFILE"]).expanduser().resolve()
    return CONFIG_DIR / "profile.toml"


def profile() -> dict:
    """config/profile.toml's settings, read again only when the file changes."""
    path = profile_path()
    if not path.exists():
        raise RuntimeError(
            f"Profile not found at {path}. Run `uv run jobflow init` to create "
            "config/profile.toml (or set JOBFLOW_PROFILE), then put your CVs in cv/."
        )
    return _read_profile(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _read_profile(path: Path, mtime_ns: int) -> dict:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{path} is not valid TOML: {exc}") from None


def configured_path(section: str, name: str, default: str) -> Path:
    settings = profile()
    value = os.environ.get(name.upper()) or settings.get(section, {}).get(name, default)
    path = Path(value).expanduser()
    # Relative paths are relative to the profile file, not the working directory.
    return path if path.is_absolute() else (profile_path().parent / path).resolve()


def cv_directory() -> Path:
    return configured_path("paths", "cv_directory", "../cv")


def output_directory() -> Path:
    return configured_path("paths", "output_directory", "../output")


def searches_file() -> Path:
    return configured_path("paths", "searches_file", "./searches.toml")


# The required and preferred weights of a match score when the profile doesn't set them.
DEFAULT_WEIGHTS = (0.75, 0.25)


def matching_weights() -> tuple[float, float]:
    matching = profile().get("matching", {})
    required = float(matching.get("required_weight", DEFAULT_WEIGHTS[0]))
    preferred = float(matching.get("preferred_weight", DEFAULT_WEIGHTS[1]))
    # TOML allows nan and inf, which would make every score nan.
    if not (math.isfinite(required) and math.isfinite(preferred)) \
            or required < 0 or preferred < 0 or required + preferred <= 0:
        raise RuntimeError("matching weights must be finite, non-negative, and not both zero")
    return required, preferred


def chosen_cvs() -> list[str]:
    """The CV file names cvs, at the top of the profile, lists (empty when it isn't set)."""
    cvs = profile().get("cvs", [])
    if not isinstance(cvs, list) or not all(isinstance(c, str) and c.strip() for c in cvs):
        raise RuntimeError('cvs in config/profile.toml must be a list of CV file names, such '
                           'as cvs = ["CV_dev.yaml", "CV_strat.yaml"]')
    return [c.strip() for c in cvs]


def timezone() -> ZoneInfo:
    """The timezone of every date and time jobflow shows, stores, or compares:
    JOBFLOW_TIMEZONE if set (the daily GitHub import's copy, published by update-searches),
    else locale.timezone in config/profile.toml, else UTC."""
    name, where = os.environ.get("JOBFLOW_TIMEZONE"), "JOBFLOW_TIMEZONE"
    if not name:
        where = "locale.timezone in config/profile.toml"
        # No profile, as in GitHub Actions without the variable, means UTC; a broken one is
        # an error, not a quiet switch of every date to UTC.
        name = profile().get("locale", {}).get("timezone") if profile_path().exists() else None
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(f"Unknown timezone {name!r} ({where}); use a name such as "
                           '"Australia/Sydney"') from None


def today() -> date:
    """Today's date in timezone()."""
    return datetime.now(timezone()).date()


def now() -> datetime:
    """The time now in timezone()."""
    return datetime.now(timezone())
