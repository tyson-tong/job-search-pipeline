"""Database reads and writes behind the web app."""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timedelta

from ..db import CLOSED_STATUSES, STATUSES, connect, set_job_state
from ..ingest.options import CURRENCIES, LINKEDIN_COUNTRIES, USD_PER_UNIT
from ..ingest.store import IN_PROGRESS, possible_duplicate_map, possible_duplicates

DEFAULT_STATUSES = ["interested", "applied", "interviewing", "offered", "accepted"]
# Statuses a person can choose; "filtered" is only set by imports.
EDITABLE_STATUSES = [s for s in STATUSES if s != "filtered"]
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
# A job's tracking version: when its status, notes, or follow-up last changed (in
# microseconds). Imports never change these, so they can't cause false conflicts.
VERSION_SQL = """(extract(epoch from greatest(j.status_updated_at,
                                              coalesce(a.updated_at, j.status_updated_at)))
                  * 1000000)::bigint::text"""


class InvalidChange(ValueError):
    pass


class Conflict(Exception):
    """Some jobs changed after the page loaded them; nothing was saved."""

    def __init__(self, conflicts: list[dict]):
        super().__init__(f"{len(conflicts)} job(s) changed since you loaded them")
        self.conflicts = conflicts


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
    statuses = [s for s in params.get("status", []) if s in STATUSES] or DEFAULT_STATUSES
    sort = (params.get("sort") or ["score"])[0]
    posted = (params.get("posted") or [""])[0]
    try:
        limit = max(1, min(int((params.get("limit") or [MAX_ROWS])[0]), MAX_ROWS))
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


def list_jobs(filters: dict) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT j.id, j.title, j.company, j.location, j.posted_at, j.first_seen_at,
                       j.status, j.salary_min, j.salary_max, best.score, best.cv_variant,
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
                        SELECT DISTINCT ON (cv_variant) cv_variant, score
                        FROM match_assessments WHERE job_id = j.id
                        ORDER BY cv_variant, created_at DESC) latest
                    ORDER BY latest.score DESC LIMIT 1) best ON true
                WHERE j.status = ANY(%(statuses)s)
                  AND (%(q)s = '' OR j.title ILIKE %(pattern)s OR j.company ILIKE %(pattern)s
                       OR j.location ILIKE %(pattern)s)
                  AND (cardinality(%(countries)s::text[]) = 0
                       OR coalesce(j.country, %(unknown)s) = ANY(%(countries)s::text[]))
                  AND (cardinality(%(sites)s::text[]) = 0
                       OR EXISTS (SELECT 1 FROM job_sources s
                                  WHERE s.job_id = j.id AND s.source = ANY(%(sites)s::text[])))
                  -- Jobs without a salary, or without a known currency, are always kept.
                  AND (%(salary_min_usd)s::numeric IS NULL OR usd.salary_max IS NULL
                       OR usd.salary_max >= %(salary_min_usd)s::numeric)
                  AND (%(score_min)s::numeric IS NULL OR best.score >= %(score_min)s::numeric)
                  -- The Posted column shows the posted date, else the date first seen.
                  AND (%(within_days)s::int IS NULL OR coalesce(j.posted_at, j.first_seen_at::date)
                       >= %(today)s::date - %(within_days)s::int)
                  AND (%(older_than_days)s::int IS NULL OR coalesce(j.posted_at, j.first_seen_at::date)
                       < %(today)s::date - %(older_than_days)s::int)
                ORDER BY {SORTS[filters['sort']]}
                LIMIT %(limit)s""",
            {**filters, "pattern": f"%{_like_escape(filters['q'])}%", "unknown": UNKNOWN_COUNTRY,
             "fx_codes": FX_CODES, "fx_rates": FX_RATES,
             # This machine's date, as the rows' overdue flags use: the database's date is
             # UTC, a day behind Sydney every morning.
             "today": date.today(),
             "within_days": POSTED_WITHIN.get(filters["posted"]),
             "older_than_days": POSTED_OLDER_THAN if filters["posted"] == "older" else None}
        ).fetchall()
        keys = ("id", "title", "company", "location", "posted_at", "first_seen_at", "status",
                "salary_min", "salary_max", "score", "cv_variant", "sites", "url",
                "follow_up_on", "applied_at", "has_notes", "filter_reason", "country", "version")
        jobs = [dict(zip(keys, row)) for row in rows]
        for job in jobs:
            job["currency"] = CURRENCIES.get(job["country"])
        duplicates = possible_duplicate_map(conn, [job["id"] for job in jobs])
    today = date.today()
    for job in jobs:
        similar = duplicates.get(job["id"], [])
        dups = [d for d in similar if d["same_country"]]
        job["duplicates"] = dups
        # The same ad in another country: not a duplicate, but listed next to it.
        job["elsewhere"] = [d for d in similar if not d["same_country"]]
        job["duplicate_in_progress"] = next((d for d in dups if d["status"] in IN_PROGRESS), None)
        job["overdue"] = bool(job["follow_up_on"] and job["follow_up_on"] < today
                              and job["status"] not in CLOSED_STATUSES)
        job["stale"] = bool(job["status"] == "applied" and job["applied_at"]
                            and datetime.now(job["applied_at"].tzinfo) - job["applied_at"]
                            > STALE_AFTER)
    return group_duplicates(jobs)


def _sets(jobs: list[dict], fields: tuple[str, ...]) -> dict[int, int]:
    """{job id: id of its set} for jobs linked (directly or via others) through the given
    lists of similar jobs, counting only jobs in this list."""
    parent = {job["id"]: job["id"] for job in jobs}

    def root(pk: int) -> int:
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    for job in jobs:
        for field in fields:
            for other in job[field]:
                if other["id"] in parent:
                    parent[root(other["id"])] = root(job["id"])
    return {pk: root(pk) for pk in parent}


def group_duplicates(jobs: list[dict]) -> list[dict]:
    """Place similar jobs next to each other, at the place of the best-ranked one,
    keeping the sort order otherwise: similar jobs, and the same ad in other
    countries. Each set of similar jobs stays together and its members get the
    same "group" number (the first member's id), else None. Jobs joined by the same ad in
    another country get the same "area" number (the first job's id), else None, so the
    page can mark the two relations differently."""
    near = _sets(jobs, ("duplicates", "elsewhere"))
    duplicate_set = _sets(jobs, ("duplicates",))
    members: dict[int, list[dict]] = {}
    for job in jobs:
        members.setdefault(duplicate_set[job["id"]], []).append(job)
    # A set of similar jobs always lies within one set of related jobs (any country).
    by_area: dict[int, list[dict]] = {}
    for job in jobs:
        by_area.setdefault(near[job["id"]], []).append(job)
    ordered, placed = [], set()
    for area_jobs in by_area.values():
        abroad = len({duplicate_set[job["id"]] for job in area_jobs}) > 1
        for job in area_jobs:
            job["area"] = area_jobs[0]["id"] if abroad else None
        for job in area_jobs:
            key = duplicate_set[job["id"]]
            if key in placed:
                continue
            placed.add(key)
            group = members[key]
            for member in group:
                member["group"] = group[0]["id"] if len(group) > 1 else None
            ordered.extend(group)
    return ordered


def countries(filters: dict) -> dict:
    """The countries among jobs with the chosen statuses, with job counts, for the Country
    filter to offer; and the number of those jobs on each site, for the Sites filter."""
    params = {"statuses": filters["statuses"], "unknown": UNKNOWN_COUNTRY}
    with connect() as conn:
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
    with connect() as conn:
        counts = dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
        overdue = conn.execute(
            """SELECT count(*) FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.follow_up_on < %s AND NOT (j.status = ANY(%s))""",
            (date.today(), CLOSED_STATUSES)).fetchone()[0]
    # Scoring reads your CV files; a missing CV folder shouldn't break the rest of the page.
    from ..candidate.cvs import cv_sources
    from ..candidate.matching import count_needing_match
    try:
        cv_count, unscored, scoring_problem = len(cv_sources()), count_needing_match(), None
    except Exception as exc:
        cv_count, unscored, scoring_problem = 0, None, str(exc)
    return {"counts": {s: counts.get(s, 0) for s in STATUSES}, "overdue": overdue,
            "statuses": list(STATUSES), "editable": EDITABLE_STATUSES,
            "default_statuses": DEFAULT_STATUSES, "cv_count": cv_count, "unscored": unscored,
            "scoring_problem": scoring_problem, "max_rows": MAX_ROWS}


def job_detail(job_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT j.id, j.title, j.company, j.location, j.posted_at, j.first_seen_at,
                      j.status, j.salary_min, j.salary_max, j.description, j.filter_reason,
                      a.notes, a.applied_at, a.follow_up_on, j.country, {VERSION_SQL} AS version
               FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE j.id=%s""",
            (job_id,)).fetchone()
        if not row:
            return None
        keys = ("id", "title", "company", "location", "posted_at", "first_seen_at", "status",
                "salary_min", "salary_max", "description", "filter_reason", "notes",
                "applied_at", "follow_up_on", "country", "version")
        job = dict(zip(keys, row))
        job["currency"] = CURRENCIES.get(job["country"])
        job["sources"] = [dict(zip(("source", "url", "first_seen_at"), r)) for r in conn.execute(
            """SELECT source, url, first_seen_at FROM job_sources WHERE job_id=%s
               ORDER BY first_seen_at""", (job_id,)).fetchall()]
        job["matches"] = [
            {"variant": v, "score": score, "matched": matched, "partial": partial,
             "not_evidenced": gaps, "rationale": (evidence or {}).get("rationale", ""),
             "assessed_at": created}
            for v, score, matched, partial, gaps, evidence, created in conn.execute(
                """SELECT DISTINCT ON (cv_variant) cv_variant, score, matched, partial, gaps,
                          evidence, created_at
                   FROM match_assessments WHERE job_id=%s
                   ORDER BY cv_variant, created_at DESC""", (job_id,)).fetchall()]
        job["matches"].sort(key=lambda m: m["score"], reverse=True)
        job["history"] = [dict(zip(("from_status", "to_status", "via", "changed_at"), r))
                          for r in conn.execute(
            """SELECT from_status, to_status, via, changed_at FROM status_history
               WHERE job_id=%s ORDER BY changed_at""", (job_id,)).fetchall()]
    job["duplicates"] = possible_duplicates(job_id)
    job["elsewhere"] = possible_duplicates(job_id, other_countries=True)
    return job


def validate_changes(raw: object) -> list[dict]:
    """Check a save request before anything is written; raises InvalidChange."""
    if not isinstance(raw, list) or not raw:
        raise InvalidChange("Nothing to save")
    changes, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidChange("Each change must be an object")
        unknown = set(item) - {"id", "status", "note", "follow_up_on", "expected_version"}
        if unknown:
            raise InvalidChange(f"Unknown field(s): {', '.join(sorted(unknown))}")
        job_id = item.get("id")
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id in seen:
            raise InvalidChange(f"Invalid or repeated job id: {job_id!r}")
        seen.add(job_id)
        change: dict = {"id": job_id}
        if "status" in item:
            if item["status"] not in EDITABLE_STATUSES:
                raise InvalidChange(f"Job {job_id}: status must be one of {EDITABLE_STATUSES}")
            change["status"] = item["status"]
        if "note" in item:
            note = item["note"]
            if not isinstance(note, str) or len(note) > MAX_NOTE:
                raise InvalidChange(f"Job {job_id}: a note must be text of at most {MAX_NOTE} characters")
            if note.strip():
                change["note"] = note
        if "follow_up_on" in item:
            value = item["follow_up_on"]
            try:
                change["follow_up_on"] = date.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                raise InvalidChange(f"Job {job_id}: follow-up date must be YYYY-MM-DD") from None
        if len(change) == 1:
            raise InvalidChange(f"Job {job_id}: nothing to change")
        version = item.get("expected_version")
        if version is not None:
            if not isinstance(version, str) or not version.isdigit():
                raise InvalidChange(f"Job {job_id}: invalid expected_version")
            change["expected_version"] = version
        changes.append(change)
    return changes


def find_conflicts(conn, changes: list[dict]) -> list[dict]:
    """Jobs whose tracking version differs from the one the page loaded. Locks the rows,
    so nothing can change them between this check and the save."""
    expected = {c["id"]: c["expected_version"] for c in changes if "expected_version" in c}
    if not expected:
        return []
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company, j.status, a.follow_up_on,
                   {VERSION_SQL} AS version,
                   (SELECT via FROM status_history h WHERE h.job_id = j.id
                    ORDER BY changed_at DESC LIMIT 1) AS last_via
            FROM jobs j LEFT JOIN applications a ON a.job_id = j.id
            WHERE j.id = ANY(%s) FOR UPDATE OF j""", (list(expected),)).fetchall()
    return [{"id": pk, "title": title, "company": company, "status": status,
             "follow_up_on": follow_up_on, "changed_via": via}
            for pk, title, company, status, follow_up_on, version, via in rows
            if version != expected[pk]]


def save_changes(raw: object, force: bool = False) -> list[int]:
    """Validate, check for conflicting edits made elsewhere (unless force), then apply
    every change in one transaction: all are saved or none."""
    changes = validate_changes(raw)
    with connect() as conn:
        conflicts = [] if force else find_conflicts(conn, changes)
        if conflicts:
            raise Conflict(conflicts)  # leaving the block rolls back
        for change in changes:
            fields = {k: v for k, v in change.items() if k not in ("id", "expected_version")}
            set_job_state(conn, change["id"], via="web", **fields)
    return [change["id"] for change in changes]


def _csv_cell(value: object) -> str:
    """Text for a CSV cell. Scraped text starting with = + - @ would run as a formula in
    a spreadsheet, so it's prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(filters: dict, ids: set[int] | None = None) -> str:
    """The jobs the filters find as CSV, or only those of them in ids (a page's rows)."""
    jobs = list_jobs({**filters, "limit": 5000})
    if ids is not None:
        jobs = [job for job in jobs if job["id"] in ids]
    with connect() as conn:
        notes = dict(conn.execute("SELECT job_id, notes FROM applications WHERE job_id = ANY(%s)",
                                  ([j["id"] for j in jobs],)).fetchall())
    out = io.StringIO()
    columns = ["id", "status", "title", "company", "location", "country", "score",
               "cv_variant", "posted_at", "first_seen_at", "salary_min", "salary_max", "currency",
               "sites", "url", "applied_at", "follow_up_on", "notes"]
    writer = csv.writer(out)
    writer.writerow(columns)
    for job in jobs:
        job = {**job, "sites": " ".join(job["sites"]), "notes": notes.get(job["id"]) or ""}
        writer.writerow([_csv_cell(job.get(c)) for c in columns])
    return out.getvalue()
