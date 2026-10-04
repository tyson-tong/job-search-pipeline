"""The filters a site can't apply itself, applied to each posting after import: excluded
title terms and employers, salary, job type, work arrangement, and a widened date window."""
from __future__ import annotations

from datetime import date, timedelta

from .. import settings
from ..text import words
from . import options as opt
from .runs import POSTED_WITHIN, WINDOW_NAMES, site_window
from .searches import Search
from .sources import ANNUAL_FACTOR, Posting


def search_warnings(search: Search) -> list[str]:
    """Settings that are allowed but probably not what you meant: no cost cap, a term
    listed twice, and an excluded title term that never excludes anything because it
    contains a kept one."""
    warnings = []
    if search.max_cost_usd is None:
        warnings.append(f"no max_cost_usd: each run is capped only at {search.max_results} "
                        "results, so a run that goes wrong can cost more than you expect; "
                        "set one under [defaults]")
    for key in ("exclude_title_terms", "include_title_terms", "exclude_companies"):
        terms = [words(t) for t in getattr(search, key)]
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
    padded = f" {words(title)} "

    def found(term: str) -> bool:
        return f" {words(term)} " in padded
    if any(found(term) for term in keep):
        return None
    return next((term for term in terms if found(term)), None)


# How the reasons for the two exclusions begin. update-searches (refilter.py) tells them from
# the other reasons by these, so they're shared rather than written out twice.
EXCLUDED_EMPLOYER = "employer matches "
EXCLUDED_TITLE = "title contains "


def excluded_reason(title: str | None, company: str | None, search: Search) -> str | None:
    """Why the search's excluded employers or title terms keep a job out, or None. An
    excluded employer is matched like a title term (whole words, so "Acme" excludes "Acme
    Pty Ltd" but not "Acmera") and include_title_terms don't override it."""
    employer = excluded_title_term(company, search.exclude_companies)
    if employer:
        return f"{EXCLUDED_EMPLOYER}{employer!r}"
    term = excluded_title_term(title, search.exclude_title_terms, search.include_title_terms)
    return f"{EXCLUDED_TITLE}{term!r}" if term else None


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
    posted date listed means the job is kept, and so does a salary with no pay period.
    """
    active = local_filters(site, search)
    reason = excluded_reason(posting.title, posting.company, search)
    if reason:
        return reason
    if "posted_date" in active and posting.posted_at is not None:
        # One day of slack: sites and time zones disagree on what "today" is.
        oldest = (today or settings.today()) - timedelta(days=search.posted_within_days + 1)
        if posting.posted_at < oldest:
            return (f"posted {posting.posted_at.isoformat()}, older than "
                    f"{WINDOW_NAMES[search.posted_within_days]}")
    # A salary whose period was guessed (no "per year" or "per hour" in the ad) may be far
    # off, so it never filters a job out.
    if "salary" in active and posting.salary_min is not None and posting.salary_max is not None \
            and not posting.salary_estimated:
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
