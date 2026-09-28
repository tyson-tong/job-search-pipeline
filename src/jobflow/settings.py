from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


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
    """config/.env in the current project, else config/.env in the source checkout."""
    local = Path.cwd() / "config" / ".env"
    return local if local.exists() else CONFIG_DIR / ".env"


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def profile_path() -> Path:
    """JOBFLOW_PROFILE, else config/profile.toml in the current project or checkout."""
    if os.environ.get("JOBFLOW_PROFILE"):
        return Path(os.environ["JOBFLOW_PROFILE"]).expanduser().resolve()
    local = Path.cwd() / "config" / "profile.toml"
    return local.resolve() if local.exists() else CONFIG_DIR / "profile.toml"


def profile() -> dict:
    path = profile_path()
    if not path.exists():
        raise RuntimeError(
            f"Profile not found at {path}. Run `uv run jobflow init` to create "
            "config/profile.toml (or set JOBFLOW_PROFILE), then put your CVs in cv/."
        )
    with path.open("rb") as stream:
        return tomllib.load(stream)


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


def matching_weights() -> tuple[float, float]:
    matching = profile().get("matching", {})
    required = float(matching.get("required_weight", 0.75))
    preferred = float(matching.get("preferred_weight", 0.25))
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


def timezone() -> ZoneInfo | None:
    name = profile().get("locale", {}).get("timezone")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown locale.timezone {name!r} in profile") from exc
