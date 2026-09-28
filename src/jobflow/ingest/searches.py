"""Job searches: load config/searches.toml, translate each search into Apify actor inputs, and
apply the filters a site can't apply itself.

See config/searches.example.toml for the format and options.py for allowed values.
"""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import product
from urllib.parse import parse_qs, urlencode, urlsplit

from . import options as opt
from .sources import ANNUAL_FACTOR, Posting

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


@dataclass
class PlannedRun:
    site: str
    actor: str
    search: Search
    label: str
    run_input: dict
    # Shown in the tracker's Searches tab: options this site can't apply, widened date windows.
    notes: list[str] = field(default_factory=list)

    @property
    def country(self) -> str | None:
        return run_country(self.site, self.run_input)


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
        locations[code] = tuple(known[c.strip().casefold()] for c in cities)
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
        name=name, terms=tuple(t.strip() for t in terms), locations=locations, sites=sites,
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


def _title_terms(merged: dict, key: str, where: str,
                 example: str = '["Internship", "Graduate"]') -> tuple[str, ...]:
    terms = merged.get(key) or []
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list) or not all(isinstance(t, str) and _words(t) for t in terms):
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
        country_name = opt.LINKEDIN_COUNTRIES[country]
        params = {"keywords": term,
                  "location": (f"{opt.LINKEDIN_CITY_NAMES.get(city, city)}, {country_name}"
                               if city else country_name)}
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
        for key in search.seek_classifications:
            run_input[key] = True
        runs.append(PlannedRun("seek", opt.ACTORS["seek"], search,
                               f"{term!r} in {city or country}", run_input, notes))
    return runs


def _notes(site: str, search: Search, window_note: str | None) -> list[str]:
    notes = [window_note] if window_note else []
    if search.seek_classifications and opt.FILTER_SUPPORT[site]["seek_classifications"] == "unsupported":
        notes.append("seek_classifications skipped (Seek only; use search terms instead)")
    return notes


_COUNTRY_BY_NAME = {name.casefold(): code for code, name in opt.LINKEDIN_COUNTRIES.items()}


def country_from_location(location: str | None) -> str | None:
    """ISO code when a location ends in a country name, as LinkedIn's do ("Sydney, New
    South Wales, Australia", "Hong Kong SAR"); None for "Admiralty, Central and Western
    District" and other locations that don't name one."""
    last = (location or "").rsplit(",", 1)[-1].strip().casefold().removesuffix(" sar")
    return _COUNTRY_BY_NAME.get(last)


def run_country(site: str, run_input: dict | None) -> str | None:
    """ISO code of the one country an actor input searches, else None (a LinkedIn run
    covering several countries, or an input this code didn't build)."""
    run_input = run_input or {}
    if site in ("indeed", "seek"):
        table = opt.INDEED_COUNTRIES if site == "indeed" else opt.SEEK_COUNTRIES
        return next((code for code, value in table.items() if value == run_input.get("country")), None)
    countries = {_url_country(url) for url in run_input.get("urls") or []}
    return countries.pop() if len(countries) == 1 else None


def search_url_country(item: dict | None) -> str | None:
    """ISO code of the country a LinkedIn job's own search URL (its inputUrl) asked for, so
    a LinkedIn run covering several countries still knows each job's; None for other sites."""
    return _url_country(str((item or {}).get("inputUrl") or ""))


def _url_country(url: str) -> str | None:
    """The country in a LinkedIn search URL's location, e.g. "Sydney, Australia"."""
    return country_from_location((parse_qs(urlsplit(url).query).get("location") or [""])[0])


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


def _words(text: str | None) -> str:
    """Lower-case words separated by single spaces, ignoring punctuation other than + and #,
    so "C++" and "C#" stay themselves rather than the word "c"."""
    return re.sub(r"[^a-z0-9+#]+", " ", (text or "").casefold()).strip()


def search_warnings(search: Search) -> list[str]:
    """Settings that are allowed but probably not what you meant: a term listed twice, and
    an excluded title term that never excludes anything because it contains a kept one."""
    warnings = []
    for key in ("exclude_title_terms", "include_title_terms", "exclude_companies"):
        terms = [_words(t) for t in getattr(search, key)]
        repeated = sorted({t for t in terms if terms.count(t) > 1})
        if repeated:
            warnings.append(f"{key} lists {', '.join(map(repr, repeated))} more than once")
    for term in search.exclude_title_terms:
        kept = next((k for k in search.include_title_terms
                     if excluded_title_term(term, (k,))), None)
        if kept:
            warnings.append(f"exclude_title_terms {term!r} never applies: a title containing it "
                            f"also contains {kept!r}, which include_title_terms keeps")
    return warnings


def excluded_title_term(title: str | None, terms: tuple[str, ...],
                        keep: tuple[str, ...] = ()) -> str | None:
    """The first excluded term found in the title as whole words (ignoring case and
    punctuation), so "Graduate" excludes "Graduate Analyst" but not "Postgraduate
    Researcher". None when the title also contains a term from `keep`
    (include_title_terms): "Quant Analyst" kept overrides "Analyst" excluded."""
    padded = f" {_words(title)} "

    def found(term: str) -> bool:
        return f" {_words(term)} " in padded
    if any(found(term) for term in keep):
        return None
    return next((term for term in terms if found(term)), None)


def excluded_reason(title: str | None, company: str | None, search: Search) -> str | None:
    """Why the search's excluded employers or title terms keep a job out, or None. An
    excluded employer is matched like a title term (whole words, so "Acme" excludes "Acme
    Pty Ltd" but not "Acmera") and include_title_terms don't override it."""
    employer = excluded_title_term(company, search.exclude_companies)
    if employer:
        return f"employer matches {employer!r}"
    term = excluded_title_term(title, search.exclude_title_terms, search.include_title_terms)
    return f"title contains {term!r}" if term else None


def local_filters(site: str, search: Search) -> list[str]:
    support = opt.FILTER_SUPPORT.get(site, {})
    active = ["title_terms"] if search.exclude_title_terms else []
    if search.exclude_companies:
        active.append("companies")
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
    reason = excluded_reason(posting.title, posting.company, search)
    if reason:
        return reason
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
