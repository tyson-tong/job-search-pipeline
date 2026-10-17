"""The database reads behind the web app: the job list, its filters, counts, and a job's
details. grouping.py places similar jobs together, saving.py writes, and export.py makes
the CSV."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from psycopg.rows import dict_row

from .. import db
from ..candidate.matching import (
    ASSESSMENT_FORMAT,
    count_needing_match,
    current_digests,
    outdated_scores,
    score_sql,
    score_weights,
)
from ..db import (
    CLOSED_STATUSES,
    DEFAULT_MY_JOBS,
    EDITABLE_STATUSES,
    IN_PROGRESS,
    PURSUING,
    STATUSES,
    TRIAGE_CHOICES,
    UNTRIAGED,
)
from ..ingest.options import CURRENCIES, LINKEDIN_COUNTRIES, USD_PER_UNIT
from ..ingest.similar import similar_jobs
from ..ingest.store import RECENT_RUNS, recent_capped_runs, recent_failed_runs
from ..settings import DEFAULT_WEIGHTS, today
from .grouping import group_duplicates

# The date the Posted column shows: the posted date, else the date first seen.
POSTED_DATE = "coalesce(j.posted_at, j.first_seen_at::date)"
SORTS = {
    "score": "best.score DESC NULLS LAST, j.first_seen_at DESC",
    "posted": f"{POSTED_DATE} DESC, j.first_seen_at DESC",
    "posted_asc": f"{POSTED_DATE} ASC, j.first_seen_at ASC",
    # Salary in US dollars by the top of the range; jobs without one always come last.
    "salary": "usd.salary_max DESC NULLS LAST, j.first_seen_at DESC",
    "salary_asc": "usd.salary_max ASC NULLS LAST, j.first_seen_at DESC",
    "added": "j.first_seen_at DESC",
    "follow_up": "a.follow_up_on ASC NULLS LAST, j.first_seen_at DESC",
}
# Each country code with its currency's value in US dollars, for converting salaries in SQL.
FX_CODES = list(CURRENCIES)
FX_RATES = [USD_PER_UNIT[CURRENCIES[code]] for code in FX_CODES]
STALE_AFTER = timedelta(days=21)
# The most jobs one list shows; the page says so when a list reaches it.
MAX_ROWS = 2000
MAX_NOTE = 2000
# The Country filter's value for a job whose country isn't known.
UNKNOWN_COUNTRY = "none"
# The most similar jobs, and same ads in other countries, the job panel lists.
SIMILAR_SHOWN = 5
# An assessment's score with the profile's weights now, as tailor-cv works it out.
SCORE = score_sql("evidence", "score")
# A job's tracking version: when its status, notes, or follow-up last changed (in
# microseconds). Imports never change these, so they can't cause false conflicts.
VERSION_SQL = """(extract(epoch from greatest(j.status_updated_at,
                                              coalesce(a.updated_at, j.status_updated_at)))
                  * 1000000)::bigint::text"""


def _digests_now() -> dict[str, str]:
    """Each named CV's digest now, to tell which scores are from an earlier version of it;
    empty when the CVs can't be read (the page explains that problem separately)."""
    try:
        return current_digests()
    except Exception:
        return {}


def _weights() -> dict[str, float]:
    """The profile's scoring weights for score_sql; the defaults when they can't be read
    (the page explains that problem separately)."""
    try:
        return score_weights()
    except Exception:
        return dict(zip(("required_weight", "preferred_weight"), DEFAULT_WEIGHTS, strict=True))


def _outdated(conn, job_ids: list[int], digests: dict[str, str]) -> dict[int, list[str]]:
    """{job id: the CVs whose latest score for it is from an earlier version of the CV}."""
    if not job_ids or not digests:
        return {}
    outdated: dict[int, list[str]] = {}
    for job_id, variant, digest in conn.execute(
            # Only scores in the current format, as Rescore edited CVs counts them: an older
            # one counts as no score, and is scored again by Score all jobs.
            f"""SELECT DISTINCT ON (job_id, cv_variant) job_id, cv_variant, cv_sha256
               FROM match_assessments
               WHERE job_id = ANY(%s) AND evidence->>'format' = '{ASSESSMENT_FORMAT}'
               ORDER BY job_id, cv_variant, created_at DESC""", (job_ids,)).fetchall():
        if variant in digests and digest != digests[variant]:
            outdated.setdefault(job_id, []).append(variant)
    return outdated


def _number(params: dict[str, list[str]], key: str) -> float | None:
    try:
        value = float((params.get(key) or [""])[0])
    except ValueError:
        return None
    return value if value and math.isfinite(value) else None


# The Posted column filter: posted in the last this many days (today counts as day 0),
# or "older": more than a month ago. The two month choices together cover every job.
POSTED_WITHIN = {"1": 1, "7": 7, "14": 14, "30": 30}
POSTED_OLDER_THAN = 30


def parse_filters(params: dict[str, list[str]]) -> dict:
    """Query-string filters -> validated values (unknown or malformed values are ignored)."""
    statuses = [s for s in params.get("status", []) if s in STATUSES] or list(DEFAULT_MY_JOBS)
    sort = (params.get("sort") or ["score"])[0]
    posted = (params.get("posted") or [""])[0]
    try:
        limit = max(1, min(int((params.get("limit") or [str(MAX_ROWS)])[0]), MAX_ROWS))
    except ValueError:
        limit = MAX_ROWS
    return {"statuses": statuses, "q": (params.get("q") or [""])[0].strip(),
            # Chosen sites and countries; none chosen means all.
            "sites": [s for s in params.get("site", []) if 0 < len(s) <= 20][:10],
            "countries": [c for c in params.get("country", []) if 0 < len(c) <= 10][:50],
            "sort": sort if sort in SORTS else "score",
            # One filter per column of the job list.
            "score_min": _number(params, "score_min"),
            "posted": posted if posted in POSTED_WITHIN or posted == "older" else "",
            # In US dollars a year, compared after converting each job's own currency.
            "salary_min_usd": _number(params, "salary_min_usd"),
            "limit": limit}


def _like_escape(text: str) -> str:
    """Search text matched literally by ILIKE: % and _ are otherwise wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def job_rows(conn, filters: dict, digests: dict[str, str] | None = None) -> list[dict]:
    """The jobs the filters find, in their sort order, with what each row shows. `digests`
    (_digests_now(), read here if not given) names the CVs whose scores count."""
    if digests is None:
        digests = _digests_now()
    jobs = conn.cursor(row_factory=dict_row).execute(
        f"""SELECT j.id, j.title, j.company, j.location, j.posted_at, j.first_seen_at,
                   j.status, j.salary_min, j.salary_max, j.salary_estimated, best.score,
                   best.cv_variant,
                   coalesce((SELECT array_agg(DISTINCT s.source ORDER BY s.source)
                             FROM job_sources s WHERE s.job_id = j.id), '{{}}') AS sites,
                   (SELECT s.url FROM job_sources s WHERE s.job_id = j.id
                    ORDER BY s.first_seen_at LIMIT 1) AS url,
                   a.follow_up_on, a.applied_at, a.notes IS NOT NULL AS has_notes,
                   j.filter_reason, j.country, {VERSION_SQL} AS version
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            -- The job's salary in US dollars; NULL when its country (so currency) is unknown.
            LEFT JOIN LATERAL (
                SELECT j.salary_max * fx.rate AS salary_max
                FROM unnest(%(fx_codes)s::text[], %(fx_rates)s::numeric[]) AS fx(code, rate)
                WHERE fx.code = j.country) usd ON true
            LEFT JOIN LATERAL (
                SELECT latest.score, latest.cv_variant FROM (
                    SELECT DISTINCT ON (cv_variant) cv_variant, {SCORE} AS score
                    FROM match_assessments WHERE job_id = j.id
                      -- Only the CVs named in cvs, which tailor-cv picks from.
                      AND (%(variants)s::text[] IS NULL OR cv_variant = ANY(%(variants)s))
                    ORDER BY cv_variant, created_at DESC) latest
                -- The same CV tailor-cv picks for this job (best_variant).
                ORDER BY latest.score DESC, latest.cv_variant DESC LIMIT 1) best ON true
            WHERE j.status = ANY(%(statuses)s)
              AND (%(q)s = '' OR j.title ILIKE %(pattern)s OR j.company ILIKE %(pattern)s
                   OR j.location ILIKE %(pattern)s)
              AND (cardinality(%(countries)s::text[]) = 0
                   OR coalesce(j.country, %(unknown)s) = ANY(%(countries)s::text[]))
              AND (cardinality(%(sites)s::text[]) = 0
                   OR EXISTS (SELECT 1 FROM job_sources s
                              WHERE s.job_id = j.id AND s.source = ANY(%(sites)s::text[])))
              -- Jobs without a salary, without a known currency, or whose salary's pay
              -- period was guessed, are always kept.
              AND (%(salary_min_usd)s::numeric IS NULL OR usd.salary_max IS NULL
                   OR j.salary_estimated
                   OR usd.salary_max >= %(salary_min_usd)s::numeric)
              AND (%(score_min)s::numeric IS NULL OR best.score >= %(score_min)s::numeric)
              -- The Posted column shows the posted date, else the date first seen.
              AND (%(within_days)s::int IS NULL OR coalesce(j.posted_at, j.first_seen_at::date)
                   >= %(today)s::date - %(within_days)s::int)
              AND (%(older_than_days)s::int IS NULL OR coalesce(j.posted_at, j.first_seen_at::date)
                   < %(today)s::date - %(older_than_days)s::int)
            ORDER BY {SORTS[filters['sort']]}
            LIMIT %(limit)s""",
        {**filters, **_weights(), "variants": list(digests) or None,
         "pattern": f"%{_like_escape(filters['q'])}%",
         "unknown": UNKNOWN_COUNTRY,
         "fx_codes": FX_CODES, "fx_rates": FX_RATES,
         # Today in your timezone, as the rows' overdue flags use.
         "today": today(),
         "within_days": POSTED_WITHIN.get(filters["posted"]),
         "older_than_days": POSTED_OLDER_THAN if filters["posted"] == "older" else None}
    ).fetchall()
    for job in jobs:
        job["currency"] = CURRENCIES.get(job["country"])
    return jobs


def list_jobs(filters: dict) -> list[dict]:
    """job_rows, with each job's similar jobs, outdated scores, and flags, and similar
    jobs placed together."""
    digests = _digests_now()   # reads and hashes each CV: once per refresh
    with db.connect() as conn:
        jobs = job_rows(conn, filters, digests)
        duplicates = similar_jobs(conn, [job["id"] for job in jobs])
        outdated = _outdated(conn, [job["id"] for job in jobs if job["score"] is not None],
                             digests)
    now_today = today()
    for job in jobs:
        # CVs edited since this job was scored against them: its score may be out of date.
        job["outdated"] = outdated.get(job["id"], [])
        similar = duplicates.get(job["id"], [])
        dups = [d for d in similar if d["same_country"]]
        job["duplicates"] = dups
        # The same ad in another country: not a duplicate, but listed next to it.
        job["elsewhere"] = [d for d in similar if not d["same_country"]]
        job["duplicate_in_progress"] = next((d for d in dups if d["status"] in IN_PROGRESS), None)
        job["overdue"] = bool(job["follow_up_on"] and job["follow_up_on"] < now_today
                              and job["status"] not in CLOSED_STATUSES)
        job["stale"] = bool(job["status"] == "applied" and job["applied_at"]
                            and datetime.now(job["applied_at"].tzinfo) - job["applied_at"]
                            > STALE_AFTER)
    return group_duplicates(jobs)


def countries(filters: dict) -> dict:
    """The countries among jobs with the chosen statuses, with job counts, for the Country
    filter to offer; and the number of those jobs on each site, for the Sites filter."""
    params = {"statuses": filters["statuses"], "unknown": UNKNOWN_COUNTRY}
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT coalesce(country, %(unknown)s), count(*) FROM jobs
               WHERE status = ANY(%(statuses)s) GROUP BY 1 ORDER BY 1""", params).fetchall()
        sites = conn.execute(
            """SELECT s.source, count(DISTINCT s.job_id) FROM job_sources s
               JOIN jobs j ON j.id = s.job_id WHERE j.status = ANY(%(statuses)s)
               GROUP BY 1""", params).fetchall()
    return {"counts": dict(rows), "names": LINKEDIN_COUNTRIES, "unknown": UNKNOWN_COUNTRY,
            "sites": dict(sites)}


def summary() -> dict:
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
        overdue = conn.execute(
            """SELECT count(*) FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.follow_up_on < %s AND NOT (j.status = ANY(%s))""",
            (today(), list(CLOSED_STATUSES))).fetchone()[0]
        # The searches look back about a day, so a day the import didn't run loses its jobs.
        last_import = conn.execute("SELECT max(finished_at) FROM scraper_runs "
                                   "WHERE status = 'SUCCEEDED'").fetchone()[0]
        # A site failing every day (a rented actor expired, say) while others succeed
        # would otherwise only show in GitHub's email.
        failed_runs = recent_failed_runs(conn)
        # Runs that returned all max_results allows, and so probably left jobs behind.
        capped_runs = recent_capped_runs(conn)
    # Scoring reads your CV files; a missing CV folder shouldn't break the rest of the page.
    # Only that is reported as a scoring problem: a database error fails the request, and
    # the page shows it as one.
    digests: dict[str, str] | None = None
    scoring_problem: str | None = None
    try:
        score_weights()   # a bad weight in config/profile.toml would rank every job wrongly
        digests = current_digests()   # any CV that can't be read, or cvs set wrongly
    except Exception as exc:
        scoring_problem = str(exc)
    variants, unscored, outdated = [], None, {"jobs": 0, "calls": 0}
    if digests is not None:
        variants = list(digests)
        unscored = count_needing_match(variants)
        # Scores against a CV edited since, which Rescore edited CVs updates: one LLM
        # call per (job, CV).
        pairs = outdated_scores(digests)
        outdated = {"jobs": len({job_id for job_id, _ in pairs}), "calls": len(pairs)}
    return {"counts": {s: counts.get(s, 0) for s in STATUSES}, "overdue": overdue,
            "last_import": last_import, "failed_runs": failed_runs, "capped_runs": capped_runs,
            # The status groups the page uses (see db.py).
            "statuses": list(STATUSES), "editable": list(EDITABLE_STATUSES),
            "default_statuses": list(DEFAULT_MY_JOBS), "in_progress": list(IN_PROGRESS),
            "pursuing": list(PURSUING), "untriaged": list(UNTRIAGED),
            "triage_choices": list(TRIAGE_CHOICES),
            "cv_variants": variants, "unscored": unscored,
            "outdated": outdated,
            "scoring_problem": scoring_problem, "max_rows": MAX_ROWS, "max_note": MAX_NOTE,
            # The header flags a late import after this long, the window jobflow status uses.
            "late_after_hours": RECENT_RUNS.total_seconds() / 3600}


def job_detail(job_id: int) -> dict | None:
    """Everything the job panel shows. Its scores are against the CVs named in cvs only
    (all of them when those can't be read), as tailor-cv chooses from."""
    digests = _digests_now()
    with db.connect() as conn:
        rows = conn.cursor(row_factory=dict_row)
        job = rows.execute(
            f"""SELECT j.id, j.title, j.company, j.location, j.posted_at, j.first_seen_at,
                      j.status, j.status_updated_at, j.salary_min, j.salary_max,
                      j.salary_estimated, j.description, j.filter_reason,
                      a.notes, a.applied_at, a.follow_up_on, j.country, {VERSION_SQL} AS version
               FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE j.id=%s""",
            (job_id,)).fetchone()
        if not job:
            return None
        job["currency"] = CURRENCIES.get(job["country"])
        job["sources"] = rows.execute(
            """SELECT source, url, first_seen_at FROM job_sources WHERE job_id=%s
               ORDER BY first_seen_at""", (job_id,)).fetchall()
        job["matches"] = rows.execute(
            f"""SELECT DISTINCT ON (cv_variant) cv_variant AS variant, {SCORE} AS score,
                       matched, partial, gaps AS not_evidenced,
                       coalesce(evidence->>'rationale', '') AS rationale,
                       created_at AS assessed_at
                FROM match_assessments WHERE job_id=%(job_id)s
                  AND (%(variants)s::text[] IS NULL OR cv_variant = ANY(%(variants)s))
                ORDER BY cv_variant, created_at DESC""",
            {"job_id": job_id, **_weights(), "variants": list(digests) or None}).fetchall()
        job["matches"].sort(key=lambda m: (m["score"], m["variant"]), reverse=True)
        stale = _outdated(conn, [job_id], digests).get(job_id, [])
        for match in job["matches"]:
            match["outdated"] = match["variant"] in stale
        job["history"] = rows.execute(
            """SELECT from_status, to_status, via, changed_at FROM status_history
               WHERE job_id=%s ORDER BY changed_at""", (job_id,)).fetchall()
        similar = similar_jobs(conn, [job_id]).get(job_id, [])
        shown = {row.pop("id"): row for row in rows.execute(
            """SELECT j.id, j.title, j.company, j.location,
                      (SELECT url FROM job_sources s WHERE s.job_id = j.id
                       ORDER BY s.first_seen_at LIMIT 1) AS url
               FROM jobs j WHERE j.id = ANY(%s)""", ([s["id"] for s in similar],)).fetchall()}
    for other in similar:
        other.update(shown[other["id"]])
    job["duplicates"] = [s for s in similar if s["same_country"]][:SIMILAR_SHOWN]
    job["elsewhere"] = [s for s in similar if not s["same_country"]][:SIMILAR_SHOWN]
    return job
