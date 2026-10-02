"""Job searches: load and validate config/searches.toml. runs.py translates each search
into Apify actor inputs, and filters.py applies the filters a site can't apply itself.

See config/searches.example.toml for the format and options.py for allowed values.
"""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass

from . import options as opt

DEFAULTS = {"posted_within_days": 1, "max_results": 50, "sites": list(opt.SITES)}
SEARCH_KEYS = {"name", "terms", "locations", "sites", "posted_within_days", "max_results",
               "max_cost_usd", "radius_km", "salary_min", "salary_max", "salary_period",
               "job_types", "work_arrangements", "seek_classifications", "exclude_title_terms",
               "include_title_terms", "exclude_companies"}
# Earlier names still accepted, so a searches file (or the GitHub copy) written before a
# rename keeps working until it is next edited.
OLD_NAMES = {"classifications": "seek_classifications"}


@dataclass(frozen=True)
class Search:
    name: str
    terms: tuple[str, ...]
    # ISO country code -> cities; no cities means the whole country.
    locations: dict[str, tuple[str, ...]]
    sites: tuple[str, ...]
    posted_within_days: int
    max_results: int
    max_cost_usd: float | None = None
    radius_km: int | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str = "annual"
    job_types: tuple[str, ...] = ()
    work_arrangements: tuple[str, ...] = ()
    seek_classifications: tuple[str, ...] = ()
    # Jobs whose title contains any of these words are filtered on import, unless it also
    # contains one of include_title_terms, which take precedence.
    exclude_title_terms: tuple[str, ...] = ()
    include_title_terms: tuple[str, ...] = ()
    # Jobs whose employer's name contains any of these words are filtered on import;
    # include_title_terms don't override them.
    exclude_companies: tuple[str, ...] = ()

    def places(self) -> list[tuple[str, str | None]]:
        """(country, city) pairs; city None means the whole country."""
        return [(country, city) for country, cities in self.locations.items()
                for city in (cities or (None,))]


class SearchConfigError(RuntimeError):
    pass


# --- Loading and validation ------------------------------------------------------------


def _choices(name: str, values, allowed) -> tuple[str, ...]:
    """A list of allowed strings (one string counts as a list of one)."""
    values = [values] if isinstance(values, str) else values or []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise SearchConfigError(f"{name}: must be a list of values from {sorted(allowed)}, "
                                f"not {values!r}")
    values = tuple(values)
    bad = [v for v in values if v not in allowed]
    if bad:
        raise SearchConfigError(f"{name}: unknown value(s) {bad}; choose from {sorted(allowed)}")
    return values


def _all_classifications() -> set[str]:
    keys = set(opt.SEEK_CLASSIFICATIONS)
    for subs in opt.SEEK_SUBCLASSIFICATIONS.values():
        keys.update(subs)
    return keys


def _current_names(raw: dict, where: str) -> dict:
    """The settings with any earlier names (OLD_NAMES) changed to the current ones."""
    raw = dict(raw)
    for old, new in OLD_NAMES.items():
        if old in raw:
            if new in raw:
                raise SearchConfigError(f"{where}: set {new} only ({old} is its old name)")
            raw[new] = raw.pop(old)
    return raw


def parse_search(raw: dict, defaults: dict) -> Search:
    name = str(raw.get("name") or "").strip()
    where = f"search {name!r}" if name else "a [[search]] entry"
    raw = _current_names(raw, where)
    unknown = set(raw) - SEARCH_KEYS
    if unknown:
        raise SearchConfigError(f"{where}: unknown setting(s) {sorted(unknown)}")
    if not name:
        raise SearchConfigError(f"{where}: name is required")
    merged = {**defaults, **raw}
    terms = merged.get("terms")
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list) or not terms \
            or not all(isinstance(t, str) and t.strip() for t in terms):
        raise SearchConfigError(f"{where}: terms must be a non-empty list of strings")
    raw_locations = merged.get("locations")
    if not isinstance(raw_locations, dict) or not raw_locations:
        raise SearchConfigError(f"{where}: locations must map country codes to city lists, "
                                'e.g. locations = { AU = ["Sydney"], NZ = [] }')
    locations = {}
    for country, cities in raw_locations.items():
        code = opt.COUNTRY_ALIASES.get(country.upper(), country.upper())
        if code in locations:   # UK and GB, say
            raise SearchConfigError(f"{where}: {code} is listed twice in locations")
        if code not in opt.CITIES:
            raise SearchConfigError(f"{where}: unknown country {country!r}; choose from "
                                    f"{', '.join(opt.CITIES)} (UK also works for GB)")
        if isinstance(cities, str):
            cities = [cities]
        if not isinstance(cities, list) or not all(isinstance(c, str) and c.strip() for c in cities):
            raise SearchConfigError(f"{where}: cities for {code} must be a list of names")
        known = {city.casefold(): city for city in opt.CITIES[code]}
        unknown_cities = [c for c in cities if c.strip().casefold() not in known]
        if unknown_cities:
            raise SearchConfigError(f"{where}: unknown {code} cit{'y' if len(unknown_cities) == 1 else 'ies'} "
                                    f"{unknown_cities}; choose from {', '.join(opt.CITIES[code])}, "
                                    "or [] for the whole country")
        # A city listed twice would be searched, and paid for, twice.
        locations[code] = tuple(dict.fromkeys(known[c.strip().casefold()] for c in cities))
    sites = _choices(f"{where} sites", merged.get("sites"), opt.SITES)
    if not sites:
        raise SearchConfigError(f"{where}: sites must list at least one of {list(opt.SITES)}")
    days = merged.get("posted_within_days")
    # true == 1 in Python, so a boolean would otherwise pass as 1 day.
    if isinstance(days, bool) or days not in opt.POSTED_WITHIN_DAYS:
        raise SearchConfigError(f"{where}: posted_within_days must be one of "
                                f"{list(opt.POSTED_WITHIN_DAYS)} (24 hours, 1 week, 2 weeks, "
                                f"1 month), not {days!r}")
    max_results = int(_number(merged, "max_results", where, whole=True) or 0)
    if max_results < 1:
        raise SearchConfigError(f"{where}: max_results must be at least 1")
    salary_min, salary_max = _number(merged, "salary_min", where), _number(merged, "salary_max", where)
    max_cost_usd = _number(merged, "max_cost_usd", where)
    radius_km = _number(merged, "radius_km", where, whole=True)
    # A cap of 0 would be sent as no cap at all, the opposite of what it says.
    if max_cost_usd == 0:
        raise SearchConfigError(f"{where}: max_cost_usd must be more than 0; leave it out "
                                "for no cap")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise SearchConfigError(f"{where}: salary_min is greater than salary_max")
    excluded = _title_terms(merged, "exclude_title_terms", where)
    included = _title_terms(merged, "include_title_terms", where)
    companies = _title_terms(merged, "exclude_companies", where, example='["Acme", "Randstad"]')
    period = merged.get("salary_period", "annual")
    _choices(f"{where} salary_period", [period], opt.SALARY_PERIODS)
    return Search(
        name=name, terms=_unique(terms), locations=locations, sites=sites,
        posted_within_days=days, max_results=max_results,
        max_cost_usd=max_cost_usd,
        radius_km=int(radius_km) if radius_km else None,
        salary_min=salary_min, salary_max=salary_max,
        salary_period=period,
        job_types=_choices(f"{where} job_types", merged.get("job_types"), opt.JOB_TYPES),
        work_arrangements=_choices(f"{where} work_arrangements",
                                   merged.get("work_arrangements"), opt.WORK_ARRANGEMENTS),
        seek_classifications=_choices(f"{where} seek_classifications",
                                      merged.get("seek_classifications"), _all_classifications()),
        exclude_title_terms=excluded,
        include_title_terms=included,
        exclude_companies=companies,
    )


def _unique(terms: list[str]) -> tuple[str, ...]:
    """The terms without repeats in any capitalisation: each would be searched, and paid
    for, again."""
    first: dict[str, str] = {}
    for term in terms:
        first.setdefault(term.strip().casefold(), term.strip())
    return tuple(first.values())


def _number(merged: dict, key: str, where: str, whole: bool = False) -> float | None:
    """A non-negative number setting (a whole one if `whole`), or None when it isn't set.
    TOML's nan and inf are rejected."""
    value = merged.get(key)
    if value is None:
        return None
    kind = "whole number" if whole else "number"
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (whole and value != int(value))):
        raise SearchConfigError(f"{where}: {key} must be a {kind} of 0 or more, not {value!r}")
    return float(value)


def words(text: str | None) -> str:
    """Lower-case words separated by single spaces, ignoring punctuation other than + and #,
    so "C++" and "C#" stay themselves rather than the word "c"."""
    return re.sub(r"[^a-z0-9+#]+", " ", (text or "").casefold()).strip()


def _title_terms(merged: dict, key: str, where: str,
                 example: str = '["Internship", "Graduate"]') -> tuple[str, ...]:
    terms = merged.get(key) or []
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list) or not all(isinstance(t, str) and words(t) for t in terms):
        raise SearchConfigError(f"{where}: {key} must be a list of words, "
                                f"e.g. {example}")
    return tuple(t.strip() for t in terms)


def parse_config(text: str) -> list[Search]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SearchConfigError(f"searches config is not valid TOML: {exc}") from exc
    unknown = set(data) - {"defaults", "search"}
    if unknown:
        raise SearchConfigError(f"unknown section(s) {sorted(unknown)}: use [defaults] and "
                                "[[search]] only")
    if not isinstance(data.get("defaults", {}), dict):
        raise SearchConfigError("defaults must be a [defaults] section of settings")
    raw_searches = data.get("search", [])
    if not isinstance(raw_searches, list) or not all(isinstance(s, dict) for s in raw_searches):
        raise SearchConfigError("each search must be a [[search]] section (with double "
                                "brackets), one per search")
    given_defaults = _current_names(data.get("defaults", {}), "[defaults]")
    defaults = {**DEFAULTS, **given_defaults}
    unknown = set(given_defaults) - SEARCH_KEYS
    if unknown:
        raise SearchConfigError(f"[defaults]: unknown setting(s) {sorted(unknown)}")
    searches = [parse_search(raw, defaults) for raw in raw_searches]
    if not searches:
        raise SearchConfigError("no [[search]] entries found")
    names = [s.name for s in searches]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SearchConfigError(f"duplicate search names: {sorted(duplicates)}")
    return searches


def defaults_search(text: str) -> Search:
    """The exclusions under [defaults] as a search of their own, to judge jobs found by a
    search that has since been renamed or removed. Call after parse_config(text)."""
    raw = _current_names(tomllib.loads(text).get("defaults", {}), "[defaults]")
    return Search(name="[defaults]", terms=(), locations={}, sites=(), posted_within_days=1,
                  max_results=1,
                  exclude_title_terms=_title_terms(raw, "exclude_title_terms", "[defaults]"),
                  include_title_terms=_title_terms(raw, "include_title_terms", "[defaults]"),
                  exclude_companies=_title_terms(raw, "exclude_companies", "[defaults]"))


def load_searches() -> list[Search]:
    """From JOBFLOW_SEARCHES (the TOML text, as used in GitHub Actions), else the file
    named by paths.searches_file in config/profile.toml (default ./searches.toml relative to config/profile.toml)."""
    text = os.environ.get("JOBFLOW_SEARCHES")
    if not text and os.environ.get("GITHUB_ACTIONS"):
        # There is no config/profile.toml here, so the fallback below would only confuse.
        raise SearchConfigError("The JOBFLOW_SEARCHES variable isn't set for this repository's "
                                "Actions: run `uv run jobflow update-searches` on your machine")
    if not text:
        from ..settings import searches_file
        path = searches_file()
        if not path.exists():
            raise SearchConfigError(f"No searches file at {path}. Run `uv run jobflow init` "
                                    "to create it from config/searches.example.toml, then edit it")
        text = path.read_text()
    return parse_config(text)
