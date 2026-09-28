"""What the tracker's Searches page shows: your searches, the Apify runs they plan, and
the allowed option values."""
from __future__ import annotations

import os
from itertools import groupby

from . import options as opt
from .searches import (POSTED_WITHIN, WINDOW_NAMES, SearchConfigError, load_searches,
                       local_filters, plan, search_warnings)


def _run_max_results(run_input: dict) -> int:
    """The most results one run can return, from the input the actor is actually sent
    (Seek caps maxResults below what a search may ask for)."""
    if "urls" in run_input:   # LinkedIn: a cap per search URL
        return run_input["limitPerSource"] * len(run_input["urls"])
    return run_input.get("maxResults") or run_input["count"]


def describe_searches() -> dict:
    """Your searches and every planned Apify run, or why the config can't be used.
    Nothing is run and nothing is charged."""
    if os.environ.get("JOBFLOW_SEARCHES"):
        source, path = "JOBFLOW_SEARCHES environment variable", None
    else:
        from ..settings import searches_file
        try:
            path = str(searches_file())
        except RuntimeError as exc:  # no config/profile.toml
            return {"ok": False, "source": None, "path": None, "error": str(exc), "searches": []}
        source = path
    try:
        searches = load_searches()
    except SearchConfigError as exc:
        return {"ok": False, "source": source, "path": path, "error": str(exc), "searches": []}
    runs = plan(searches)
    described, total_results = [], 0
    for search, search_runs in groupby(runs, key=lambda r: r.search):
        sites = []
        for site, site_runs in groupby(search_runs, key=lambda r: r.site):
            site_runs = list(site_runs)
            per_run = _run_max_results(site_runs[0].run_input)
            total_results += per_run * len(site_runs)
            sites.append({"site": site, "actor": site_runs[0].actor, "max_results_per_run": per_run,
                          "local_filters": local_filters(site, search),
                          "notes": list(dict.fromkeys(site_runs[0].notes)),
                          "runs": [{"label": r.label, "input": r.run_input} for r in site_runs]})
        described.append({
            "name": search.name, "terms": list(search.terms),
            "locations": {c: list(cities) for c, cities in search.locations.items()},
            "posted_within": WINDOW_NAMES[search.posted_within_days],
            "max_results": search.max_results, "max_cost_usd": search.max_cost_usd,
            "radius_km": search.radius_km, "salary_min": search.salary_min,
            "salary_max": search.salary_max, "salary_period": search.salary_period,
            "job_types": list(search.job_types), "work_arrangements": list(search.work_arrangements),
            "seek_classifications": list(search.seek_classifications),
            "exclude_title_terms": list(search.exclude_title_terms),
            "include_title_terms": list(search.include_title_terms),
            "exclude_companies": list(search.exclude_companies),
            "warnings": search_warnings(search), "sites": sites})
    return {"ok": True, "source": source, "path": path, "error": None, "searches": described,
            "total_runs": len(runs), "total_max_results": total_results}


def reference() -> dict:
    """Allowed values for config/searches.toml and how each site handles each setting."""
    return {
        "sites": list(opt.SITES),
        "job_types": list(opt.JOB_TYPES),
        "work_arrangements": list(opt.WORK_ARRANGEMENTS),
        "salary_periods": list(opt.SALARY_PERIODS),
        "posted_within": [
            {"days": d, "name": WINDOW_NAMES[d],
             "sites": {site: d in POSTED_WITHIN[site][0] for site in opt.SITES}}
            for d in opt.POSTED_WITHIN_DAYS],
        "filter_support": opt.FILTER_SUPPORT,
        "countries": [{"code": code, "name": opt.LINKEDIN_COUNTRIES[code], "cities": list(cities),
                       "sites": [s for s, table in (("linkedin", opt.LINKEDIN_COUNTRIES),
                                                     ("indeed", opt.INDEED_COUNTRIES),
                                                     ("seek", opt.SEEK_COUNTRIES)) if code in table]}
                      for code, cities in opt.CITIES.items()],
        "seek_classifications": [{"key": key, "name": name,
                             "subclassifications": [{"key": k, "name": n} for k, n in
                                                    opt.SEEK_SUBCLASSIFICATIONS.get(key, {}).items()]}
                            for key, name in opt.SEEK_CLASSIFICATIONS.items()],
    }
