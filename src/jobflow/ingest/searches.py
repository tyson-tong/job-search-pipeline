"""Job searches: load searches.toml, translate each search into Apify actor inputs, and
apply the filters a site can't apply itself.

See searches.example.toml for the format and options.py for allowed values.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product
from urllib.parse import urlencode

from . import options as opt
from .sources import ANNUAL_FACTOR, Posting

DEFAULTS = {"posted_within_days": 1, "max_results": 50, "sites": list(opt.SITES)}
SEARCH_KEYS = {"name", "terms", "locations", "sites", "posted_within_days", "max_results",
               "max_cost_usd", "radius_km", "salary_min", "salary_max", "salary_period",
               "job_types", "work_arrangements", "classifications"}


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
    classifications: tuple[str, ...] = ()

    def places(self) -> list[tuple[str, str | None]]:
        """(country, city) pairs; city None means the whole country."""
        return [(country, city) for country, cities in self.locations.items()
                for city in (cities or (None,))]


@dataclass
class PlannedRun:
    site: str
    actor: str
    search: Search
    label: str
    run_input: dict
    # Shown by `jobflow searches`: options this site can't apply, widened date windows.
    notes: list[str] = field(default_factory=list)


class SearchConfigError(RuntimeError):
    pass


# --- Loading and validation ------------------------------------------------------------


def _choices(name: str, values, allowed) -> tuple[str, ...]:
    values = tuple(values or ())
    bad = [v for v in values if v not in allowed]
    if bad:
        raise SearchConfigError(f"{name}: unknown value(s) {bad}; choose from {sorted(allowed)}")
    return values


def _all_classifications() -> set[str]:
    keys = set(opt.SEEK_CLASSIFICATIONS)
    for subs in opt.SEEK_SUBCLASSIFICATIONS.values():
        keys.update(subs)
    return keys


def parse_search(raw: dict, defaults: dict) -> Search:
    name = str(raw.get("name") or "").strip()
    where = f"search {name!r}" if name else "a [[search]] entry"
    unknown = set(raw) - SEARCH_KEYS
    if unknown:
        raise SearchConfigError(f"{where}: unknown setting(s) {sorted(unknown)}")
    if not name:
        raise SearchConfigError(f"{where}: name is required")
    merged = {**defaults, **raw}
    terms = merged.get("terms")
    if isinstance(terms, str):
        terms = [terms]
    if not terms or not all(isinstance(t, str) and t.strip() for t in terms):
        raise SearchConfigError(f"{where}: terms must be a non-empty list of strings")
    raw_locations = merged.get("locations")
    if not isinstance(raw_locations, dict) or not raw_locations:
        raise SearchConfigError(f"{where}: locations must map country codes to city lists, "
                                'e.g. locations = { AU = ["Sydney"], NZ = [] }')
    locations = {}
    for country, cities in raw_locations.items():
        code = country.upper()
        if code not in opt.LINKEDIN_COUNTRIES and code not in opt.INDEED_COUNTRIES:
            raise SearchConfigError(f"{where}: unknown country code {country!r}; use ISO codes "
                                    "such as AU, NZ, GB, US (see jobflow options)")
        if isinstance(cities, str):
            cities = [cities]
        if not isinstance(cities, list) or not all(isinstance(c, str) and c.strip() for c in cities):
            raise SearchConfigError(f"{where}: cities for {country} must be a list of names")
        locations[code] = tuple(c.strip() for c in cities)
    sites = _choices(f"{where} sites", merged.get("sites"), opt.SITES)
    if not sites:
        raise SearchConfigError(f"{where}: sites must list at least one of {list(opt.SITES)}")
    days = merged.get("posted_within_days")
    if days not in opt.POSTED_WITHIN_DAYS:
        raise SearchConfigError(f"{where}: posted_within_days must be one of "
                                f"{list(opt.POSTED_WITHIN_DAYS)} (24 hours, 1 week, 2 weeks, "
                                f"1 month), not {days!r}")
    max_results = int(merged.get("max_results") or 0)
    if max_results < 1:
        raise SearchConfigError(f"{where}: max_results must be at least 1")
    salary_min, salary_max = merged.get("salary_min"), merged.get("salary_max")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise SearchConfigError(f"{where}: salary_min is greater than salary_max")
    period = merged.get("salary_period", "annual")
    _choices(f"{where} salary_period", [period], opt.SALARY_PERIODS)
    return Search(
        name=name, terms=tuple(t.strip() for t in terms), locations=locations, sites=sites,
        posted_within_days=days, max_results=max_results,
        max_cost_usd=float(merged["max_cost_usd"]) if merged.get("max_cost_usd") else None,
        radius_km=int(merged["radius_km"]) if merged.get("radius_km") else None,
        salary_min=float(salary_min) if salary_min is not None else None,
        salary_max=float(salary_max) if salary_max is not None else None,
        salary_period=period,
        job_types=_choices(f"{where} job_types", merged.get("job_types"), opt.JOB_TYPES),
        work_arrangements=_choices(f"{where} work_arrangements",
                                   merged.get("work_arrangements"), opt.WORK_ARRANGEMENTS),
        classifications=_choices(f"{where} classifications", merged.get("classifications"),
                                 _all_classifications()),
    )


def parse_config(text: str) -> list[Search]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SearchConfigError(f"searches config is not valid TOML: {exc}") from exc
    defaults = {**DEFAULTS, **data.get("defaults", {})}
    unknown = set(data.get("defaults", {})) - SEARCH_KEYS
    if unknown:
        raise SearchConfigError(f"[defaults]: unknown setting(s) {sorted(unknown)}")
    searches = [parse_search(raw, defaults) for raw in data.get("search", [])]
    if not searches:
        raise SearchConfigError("no [[search]] entries found")
    names = [s.name for s in searches]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SearchConfigError(f"duplicate search names: {sorted(duplicates)}")
    return searches


def load_searches() -> list[Search]:
    """From JOBFLOW_SEARCHES (the TOML text, as used in GitHub Actions), else the file
    named by paths.searches_file in profile.toml (default ./searches.toml)."""
    text = os.environ.get("JOBFLOW_SEARCHES")
    if not text:
        from ..settings import searches_file
        path = searches_file()
        if not path.exists():
            raise SearchConfigError(f"No searches config: create {path} from "
                                    "searches.example.toml, or set JOBFLOW_SEARCHES")
        text = path.read_text()
    return parse_config(text)


# --- Translation to actor inputs ------------------------------------------------------


POSTED_WITHIN = {"linkedin": (opt.LINKEDIN_POSTED_WITHIN, opt.LINKEDIN_POSTED_ANY_TIME),
                 "indeed": (opt.INDEED_POSTED_WITHIN, opt.INDEED_POSTED_ANY_TIME),
                 "seek": (opt.SEEK_POSTED_WITHIN, opt.SEEK_POSTED_ANY_TIME)}
WINDOW_NAMES = {1: "24 hours", 7: "1 week", 14: "2 weeks", 30: "1 month"}


def site_window(site: str, days: int) -> tuple[object, str | None]:
    """The site's input value for a posted-date window, and a note if the site lacks
    that window and a wider one is used instead (never narrower, so no jobs are missed).
    """
    windows, any_time = POSTED_WITHIN[site]
    if days in windows:
        return windows[days], None
    wider = next((d for d in opt.POSTED_WITHIN_DAYS if d > days and d in windows), None)
    used = WINDOW_NAMES[wider] if wider else "any time"
    note = (f"posted within {WINDOW_NAMES[days]} isn't available here; searching {used} "
            "and filtering older jobs after import")
    return (windows[wider] if wider else any_time), note


def linkedin_runs(search: Search) -> list[PlannedRun]:
    window, window_note = site_window("linkedin", search.posted_within_days)
    urls = []
    for term, (country, city) in product(search.terms, search.places()):
        country_name = opt.LINKEDIN_COUNTRIES.get(country, country)
        params = {"keywords": term,
                  "location": f"{city}, {country_name}" if city else country_name}
        if window:
            params["f_TPR"] = window
        if search.radius_km and city:
            params["distance"] = round(search.radius_km / 1.609)
        urls.append("https://www.linkedin.com/jobs/search/?" + urlencode(params))
    return [PlannedRun("linkedin", opt.ACTORS["linkedin"], search,
                       f"{len(urls)} search URL(s)",
                       {"urls": urls, "limitPerSource": search.max_results,
                        "scrapeCompany": False, "autoConvertToAiSearch": True},
                       _notes("linkedin", search, window_note))]


def indeed_runs(search: Search) -> list[PlannedRun]:
    window, window_note = site_window("indeed", search.posted_within_days)
    runs, notes = [], _notes("indeed", search, window_note)
    for term, (country, city) in product(search.terms, search.places()):
        if country not in opt.INDEED_COUNTRIES:
            notes.append(f"country {country} skipped (not on Indeed)")
            continue
        run_input = {"country": opt.INDEED_COUNTRIES[country], "query": term,
                     "location": city or "", "postedWithinDays": window,
                     "count": search.max_results}
        if search.radius_km and city:
            run_input["radiusKm"] = search.radius_km
        runs.append(PlannedRun("indeed", opt.ACTORS["indeed"], search,
                               f"{term!r} in {city or country}", run_input, notes))
    return runs


def seek_runs(search: Search) -> list[PlannedRun]:
    window, window_note = site_window("seek", search.posted_within_days)
    runs, notes = [], _notes("seek", search, window_note)
    for term, (country, city) in product(search.terms, search.places()):
        if country not in opt.SEEK_COUNTRIES:
            notes.append(f"country {country} skipped (Seek covers "
                         f"{', '.join(opt.SEEK_COUNTRIES)})")
            continue
        run_input: dict = {
            "searchTerm": term, "country": opt.SEEK_COUNTRIES[country],
            "dateRange": window,
            "maxResults": min(search.max_results, opt.SEEK_MAX_RESULTS),
            "sortBy": "ListedDate"}
        if city:
            run_input["location"] = city
            if search.radius_km:
                run_input["radius"] = search.radius_km
        if search.job_types:
            run_input["workTypes"] = [opt.SEEK_JOB_TYPES[t] for t in search.job_types]
        if search.work_arrangements:
            run_input["workArrangements"] = [opt.SEEK_WORK_ARRANGEMENTS[w]
                                             for w in search.work_arrangements]
        if search.salary_min is not None or search.salary_max is not None:
            run_input["salaryType"] = opt.SEEK_SALARY_TYPES[search.salary_period]
            if search.salary_min is not None:
                run_input["salaryMin"] = int(search.salary_min)
            if search.salary_max is not None:
                run_input["salaryMax"] = int(search.salary_max)
        for key in search.classifications:
            run_input[key] = True
        runs.append(PlannedRun("seek", opt.ACTORS["seek"], search,
                               f"{term!r} in {city or country}", run_input, notes))
    return runs


def _notes(site: str, search: Search, window_note: str | None) -> list[str]:
    notes = [window_note] if window_note else []
    if search.classifications and opt.FILTER_SUPPORT[site]["classifications"] == "unsupported":
        notes.append("classifications skipped (Seek only; use search terms instead)")
    return notes


BUILDERS = {"linkedin": linkedin_runs, "indeed": indeed_runs, "seek": seek_runs}


def plan(searches: list[Search], only_search: str | None = None,
         only_site: str | None = None) -> list[PlannedRun]:
    if only_search and only_search not in {s.name for s in searches}:
        raise SearchConfigError(f"No search named {only_search!r}")
    runs = []
    for search in searches:
        if only_search and search.name != only_search:
            continue
        for site in search.sites:
            if not only_site or site == only_site:
                runs.extend(BUILDERS[site](search))
    return runs


# --- Local filtering -------------------------------------------------------------------


def local_filters(site: str, search: Search) -> list[str]:
    support = opt.FILTER_SUPPORT.get(site, {})
    active = []
    if (search.salary_min is not None or search.salary_max is not None) \
            and support.get("salary") == "local":
        active.append("salary")
    if search.job_types and support.get("job_types") == "local":
        active.append("job_types")
    if search.work_arrangements and support.get("work_arrangements") == "local":
        active.append("work_arrangements")
    if site in POSTED_WITHIN and site_window(site, search.posted_within_days)[1]:
        active.append("posted_date")
    return active


def filter_reason(posting: Posting, search: Search, site: str,
                  today: date | None = None) -> str | None:
    """Why a job fails this search's local filters, or None to keep it.

    Only known values can exclude a job: no salary, job type, work arrangement, or
    posted date listed means the job is kept.
    """
    active = local_filters(site, search)
    if "posted_date" in active and posting.posted_at is not None:
        # One day of slack: sites and time zones disagree on what "today" is.
        oldest = (today or date.today()) - timedelta(days=search.posted_within_days + 1)
        if posting.posted_at < oldest:
            return (f"posted {posting.posted_at.isoformat()}, older than "
                    f"{WINDOW_NAMES[search.posted_within_days]}")
    if "salary" in active and posting.salary_min is not None:
        factor = ANNUAL_FACTOR[search.salary_period]
        low = search.salary_min * factor if search.salary_min is not None else None
        high = search.salary_max * factor if search.salary_max is not None else None
        if (low is not None and posting.salary_max < low) or \
                (high is not None and posting.salary_min > high):
            wanted = f"{low:,.0f}" if low is not None else "any"
            wanted += f"-{high:,.0f}" if high is not None else "+"
            return (f"salary {posting.salary_min:,.0f}-{posting.salary_max:,.0f}/yr "
                    f"outside {wanted}/yr")
    if "job_types" in active and posting.job_types \
            and not posting.job_types & set(search.job_types):
        return f"job type {', '.join(sorted(posting.job_types))} not wanted"
    if "work_arrangements" in active and posting.work_arrangements \
            and not posting.work_arrangements & set(search.work_arrangements):
        return f"work arrangement {', '.join(sorted(posting.work_arrangements))} not wanted"
    return None
