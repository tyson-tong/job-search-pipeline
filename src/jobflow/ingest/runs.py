"""Translating each search into the input each site's Apify actor takes, and the country
a run (or a LinkedIn job's own search URL) covers."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from urllib.parse import parse_qs, urlencode, urlsplit

from . import options as opt
from .searches import Search, SearchConfigError


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
