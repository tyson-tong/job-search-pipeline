"""Database reads and writes behind the web app."""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timedelta

from ..candidate.matching import score_sql, score_weights
from ..db import CLOSED_STATUSES, STATUSES, connect, set_job_state
from ..ingest.options import CURRENCIES, LINKEDIN_COUNTRIES, USD_PER_UNIT
from ..ingest.similar import IN_PROGRESS, similar_jobs
from ..settings import today

DEFAULT_STATUSES = ["interested", "applied", "interviewing", "offered", "accepted"]
# Statuses a person can choose; "filtered" and "archived" are only set by jobflow itself.
EDITABLE_STATUSES = [s for s in STATUSES if s not in ("filtered", "archived")]
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
    from ..candidate.matching import current_digests
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
        return {"required_weight": 0.75, "preferred_weight": 0.25}


def _outdated(conn, job_ids: list[int], digests: dict[str, str]) -> dict[int, list[str]]:
    """{job id: the CVs whose latest score for it is from an earlier version of the CV}."""
    if not job_ids or not digests:
        return {}
    outdated: dict[int, list[str]] = {}
    for job_id, variant, digest in conn.execute(
            """SELECT DISTINCT ON (job_id, cv_variant) job_id, cv_variant, cv_sha256
               FROM match_assessments WHERE job_id = ANY(%s)
               ORDER BY job_id, cv_variant, created_at DESC""", (job_ids,)).fetchall():
        if variant in digests and digest != digests[variant]:
            outdated.setdefault(job_id, []).append(variant)
    return outdated


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
                        SELECT DISTINCT ON (cv_variant) cv_variant, {SCORE} AS score
                        FROM match_assessments WHERE job_id = j.id
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
            {**filters, **_weights(), "pattern": f"%{_like_escape(filters['q'])}%",
             "unknown": UNKNOWN_COUNTRY,
             "fx_codes": FX_CODES, "fx_rates": FX_RATES,
             # Today in your timezone, as the rows' overdue flags use.
             "today": today(),
             "within_days": POSTED_WITHIN.get(filters["posted"]),
             "older_than_days": POSTED_OLDER_THAN if filters["posted"] == "older" else None}
        ).fetchall()
        keys = ("id", "title", "company", "location", "posted_at", "first_seen_at", "status",
                "salary_min", "salary_max", "score", "cv_variant", "sites", "url",
                "follow_up_on", "applied_at", "has_notes", "filter_reason", "country", "version")
        jobs = [dict(zip(keys, row, strict=True)) for row in rows]
        for job in jobs:
            job["currency"] = CURRENCIES.get(job["country"])
        duplicates = similar_jobs(conn, [job["id"] for job in jobs])
        outdated = _outdated(conn, [job["id"] for job in jobs if job["score"] is not None],
                             _digests_now())
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
            (today(), CLOSED_STATUSES)).fetchone()[0]
        # The searches look back 24 hours, so a day the import didn't run loses its jobs.
        last_import = conn.execute("SELECT max(finished_at) FROM scraper_runs "
                                   "WHERE status = 'SUCCEEDED'").fetchone()[0]
    # Scoring reads your CV files; a missing CV folder shouldn't break the rest of the page.
    from ..candidate.cvs import chosen_sources
    from ..candidate.matching import count_needing_match, current_digests, outdated_scores
    try:
        score_weights()   # a bad weight in config/profile.toml would rank every job wrongly
        variants = list(chosen_sources())
        unscored, scoring_problem = count_needing_match(variants), None
        # Scores against a CV edited since, which Rescore edited CVs updates: one LLM
        # call per (job, CV).
        pairs = outdated_scores(current_digests())
        outdated = {"jobs": len({job_id for job_id, _ in pairs}), "calls": len(pairs)}
    except Exception as exc:
        variants, unscored, scoring_problem = [], None, str(exc)
        outdated = {"jobs": 0, "calls": 0}
    return {"counts": {s: counts.get(s, 0) for s in STATUSES}, "overdue": overdue,
            "last_import": last_import,
            "statuses": list(STATUSES), "editable": EDITABLE_STATUSES,
            "default_statuses": DEFAULT_STATUSES, "cv_variants": variants, "unscored": unscored,
            "outdated": outdated,
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
        job = dict(zip(keys, row, strict=True))
        job["currency"] = CURRENCIES.get(job["country"])
        job["sources"] = [dict(zip(("source", "url", "first_seen_at"), r, strict=True)) for r in conn.execute(
            """SELECT source, url, first_seen_at FROM job_sources WHERE job_id=%s
               ORDER BY first_seen_at""", (job_id,)).fetchall()]
        job["matches"] = [
            {"variant": v, "score": score, "matched": matched, "partial": partial,
             "not_evidenced": gaps, "rationale": (evidence or {}).get("rationale", ""),
             "assessed_at": created}
            for v, score, matched, partial, gaps, evidence, created in conn.execute(
                f"""SELECT DISTINCT ON (cv_variant) cv_variant, {SCORE}, matched, partial,
                           gaps, evidence, created_at
                    FROM match_assessments WHERE job_id=%(job_id)s
                    ORDER BY cv_variant, created_at DESC""",
                {"job_id": job_id, **_weights()}).fetchall()]
        job["matches"].sort(key=lambda m: (m["score"], m["variant"]), reverse=True)
        stale = _outdated(conn, [job_id], _digests_now()).get(job_id, [])
        for match in job["matches"]:
            match["outdated"] = match["variant"] in stale
        job["history"] = [dict(zip(("from_status", "to_status", "via", "changed_at"), r, strict=True))
                          for r in conn.execute(
            """SELECT from_status, to_status, via, changed_at FROM status_history
               WHERE job_id=%s ORDER BY changed_at""", (job_id,)).fetchall()]
        similar = similar_jobs(conn, [job_id]).get(job_id, [])
        shown = {row[0]: row[1:] for row in conn.execute(
            """SELECT j.id, j.title, j.company, j.location,
                      (SELECT url FROM job_sources s WHERE s.job_id = j.id
                       ORDER BY s.first_seen_at LIMIT 1)
               FROM jobs j WHERE j.id = ANY(%s)""", ([s["id"] for s in similar],)).fetchall()}
    for other in similar:
        other.update(zip(("title", "company", "location", "url"), shown[other["id"]], strict=True))
    job["duplicates"] = [s for s in similar if s["same_country"]][:SIMILAR_SHOWN]
    job["elsewhere"] = [s for s in similar if not s["same_country"]][:SIMILAR_SHOWN]
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
    jobs = list_jobs({**filters, "limit": None})   # LIMIT NULL: every job, however many
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
