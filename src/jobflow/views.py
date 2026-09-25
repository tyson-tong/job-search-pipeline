"""Plain-text output for the searches and options commands."""
from __future__ import annotations

import json
from itertools import groupby

from . import search_options as opt
from .searches import PlannedRun, local_filters


def print_plan(runs: list[PlannedRun], show_json: bool = False) -> None:
    total_results = 0
    for search, search_runs in groupby(runs, key=lambda r: r.search):
        print(f"== {search.name}: {', '.join(search.terms)}")
        for site, site_runs in groupby(search_runs, key=lambda r: r.site):
            site_runs = list(site_runs)
            per_run = search.max_results * (len(site_runs[0].run_input.get("urls", [])) or 1)
            total_results += per_run * len(site_runs)
            active = local_filters(site, search)
            print(f"  {site}: {len(site_runs)} run(s), up to {per_run} results each"
                  + (f"; filtered after import by {', '.join(active)}" if active else ""))
            for run in site_runs:
                print(f"    - {run.label}")
                if show_json:
                    print("      " + json.dumps(run.run_input, ensure_ascii=False))
            for note in dict.fromkeys(site_runs[0].skipped):
                print(f"    skipped: {note}")
    print(f"\n{len(runs)} Apify run(s), at most {total_results} results in total. "
          "Actors charge per result; see each actor's pricing on apify.com.")


def print_options(topic: str | None = None) -> None:
    if topic == "countries":
        print("Country codes (ISO) per site:")
        print(f"  seek:     {', '.join(opt.SEEK_COUNTRIES)}")
        print(f"  indeed:   {', '.join(opt.INDEED_COUNTRIES)}")
        print(f"  linkedin: {', '.join(opt.LINKEDIN_COUNTRIES)}")
        return
    if topic == "classifications":
        for key, name in opt.SEEK_CLASSIFICATIONS.items():
            print(f"{key}  ({name})")
            for sub, sub_name in opt.SEEK_SUBCLASSIFICATIONS.get(key, {}).items():
                print(f"    {sub}  ({sub_name})")
        return
    print(f"job_types:         {', '.join(opt.JOB_TYPES)}")
    print(f"work_arrangements: {', '.join(opt.WORK_ARRANGEMENTS)}")
    print(f"salary_period:     {', '.join(opt.SALARY_PERIODS)}")
    print(f"sites:             {', '.join(opt.SITES)}")
    print(f"classifications:   Seek only, e.g. {', '.join(list(opt.SEEK_CLASSIFICATIONS)[:4])}"
          " (jobflow options classifications)")
    print("countries:         ISO codes, e.g. AU, NZ, GB, US (jobflow options countries)\n")
    filters = list(next(iter(opt.FILTER_SUPPORT.values())))
    print("How each site applies each filter (search = by the site, before you pay;")
    print("local = after import, excluding only jobs whose value is known):")
    print(f"  {'':20}" + "".join(f"{site:>12}" for site in opt.SITES))
    for name in filters:
        print(f"  {name:20}" + "".join(f"{opt.FILTER_SUPPORT[site][name]:>12}"
                                       for site in opt.SITES))
