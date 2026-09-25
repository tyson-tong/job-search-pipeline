from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg.types.json

from .options import LINKEDIN_COUNTRIES
from .sources import Posting, normalise

JobFilter = Callable[[Posting], "str | None"]

TRACKING_PARAMS = {"trk", "trackingid", "refid", "position", "pagenum", "from", "tk", "vjk",
                   "type", "ref", "origin"}
# Words that differ between sites' spellings of the same employer.
COMPANY_NOISE = {"pty", "ltd", "limited", "inc", "llc", "co", "corp", "corporation",
                 "company", "group", "holdings", "australia", "the"}
# Two postings with the same employer and title are the same job when their
# description word sets overlap at least this much (Jaccard similarity).
SIMILAR_DESCRIPTION = 0.5
FUZZY_WINDOW_DAYS = 90
# Possible duplicates the merge didn't catch, e.g. a recruiter re-posting an employer's
# ad under its own name. Only reported, never merged, so the bar is lower and employer
# and city are ignored.
POSSIBLE_TITLE = 0.6
POSSIBLE_DESCRIPTION = 0.4
POSSIBLE_WINDOW_DAYS = 180
IN_PROGRESS = ("applied", "interviewing", "offered", "accepted")
# Location words that don't identify a city: states, countries, and generic terms.
NOT_PLACE_WORDS = ({"nsw", "vic", "qld", "tas", "act", "wa", "sa", "nt", "area", "greater",
                    "metropolitan", "metro", "region", "remote", "hybrid", "onsite", "site",
                    "cbd", "the", "and", "city", "state", "district", "province", "county",
                    # Australian states and territories written out in full.
                    "new", "south", "wales", "victoria", "queensland", "tasmania", "western",
                    "northern", "territory", "capital", "australian",
                    "england", "scotland"}
                   | {word for name in LINKEDIN_COUNTRIES.values()
                      for word in name.casefold().split()})


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.casefold() not in TRACKING_PARAMS and not k.casefold().startswith("utm_")]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def _normalise(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def company_key(company: str | None) -> str:
    return " ".join(w for w in _normalise(company).split() if w not in COMPANY_NOISE)


def match_key(posting: Posting) -> str | None:
    """Employer + title, spelled the same way whichever site it came from."""
    company, title = company_key(posting.company), _normalise(posting.title)
    if not company or not title or title == "untitled":
        return None
    return f"{company}|{title}"


def fingerprint(posting: Posting) -> str | None:
    """Exact content fingerprint; None when company, title, or description is missing."""
    company, title = _normalise(posting.company), _normalise(posting.title)
    description = _normalise(posting.description)[:1500]
    if not (company and title and description):
        return None
    return _digest("|".join((company, title, _normalise(posting.location), description)))


def description_similarity(a: str | None, b: str | None) -> float:
    words_a = {w for w in _normalise(a).split() if len(w) > 2}
    words_b = {w for w in _normalise(b).split() if len(w) > 2}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def title_similarity(a: str | None, b: str | None) -> float:
    words_a, words_b = set(_normalise(a).split()), set(_normalise(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def looks_like_same_role(title_a: str | None, description_a: str | None,
                         title_b: str | None, description_b: str | None) -> float | None:
    """Description similarity if two ads look like the same role whoever posted it and
    wherever it is, else None."""
    if title_similarity(title_a, title_b) < POSSIBLE_TITLE:
        return None
    similarity = description_similarity(description_a, description_b)
    return similarity if similarity >= POSSIBLE_DESCRIPTION else None


def possible_duplicates(job_id: int, limit: int = 5) -> list[dict]:
    """Other jobs that may be the same role as this one but weren't merged."""
    from ..db import connect

    with connect() as conn:
        job = conn.execute("SELECT title, description FROM jobs WHERE id=%s",
                           (job_id,)).fetchone()
        if not job or not job[1]:
            return []
        title, description = job
        words = [w for w in _normalise(title).split() if len(w) > 3] or _normalise(title).split()
        if not words:
            return []
        # Cheap title prefilter in SQL; descriptions are only loaded for close titles.
        rows = conn.execute(
            """SELECT id, title FROM jobs
               WHERE id <> %s AND first_seen_at > now() - make_interval(days => %s)
                 AND title ILIKE ANY(%s)""",
            (job_id, POSSIBLE_WINDOW_DAYS, [f"%{w}%" for w in words])).fetchall()
        close = [pk for pk, other in rows if title_similarity(title, other) >= POSSIBLE_TITLE]
        if not close:
            return []
        rows = conn.execute(
            """SELECT j.id, j.title, j.company, j.location, j.status, j.description,
                      (SELECT url FROM job_sources s WHERE s.job_id = j.id
                       ORDER BY s.first_seen_at LIMIT 1)
               FROM jobs j WHERE j.id = ANY(%s)""", (close[:100],)).fetchall()
    matches = []
    for pk, other_title, company, location, status, other_description, url in rows:
        similarity = looks_like_same_role(title, description, other_title, other_description)
        if similarity is not None:
            matches.append({"id": pk, "title": other_title, "company": company,
                            "location": location, "status": status, "url": url,
                            "similarity": similarity})
    return sorted(matches, key=lambda m: m["similarity"], reverse=True)[:limit]


def possible_duplicate_map(conn, job_ids: list[int]) -> dict[int, list[dict]]:
    """possible_duplicates for many jobs at once: {job id: [{id, status}]}, using two
    queries in total (titles first, then descriptions only for close titles)."""
    if not job_ids:
        return {}
    rows = conn.execute(
        """SELECT id, title FROM jobs
           WHERE first_seen_at > now() - make_interval(days => %s) OR id = ANY(%s)""",
        (POSSIBLE_WINDOW_DAYS, job_ids)).fetchall()
    titles = {pk: title for pk, title in rows}
    by_word: dict[str, set[int]] = {}
    for pk, title in titles.items():
        for word in _normalise(title).split():
            if len(word) > 3:
                by_word.setdefault(word, set()).add(pk)
    pairs = set()
    for pk in job_ids:
        words = [w for w in _normalise(titles.get(pk)).split() if len(w) > 3]
        candidates = set().union(*(by_word.get(w, set()) for w in words)) - {pk}
        pairs.update((pk, other) for other in candidates
                     if title_similarity(titles[pk], titles[other]) >= POSSIBLE_TITLE)
    if not pairs:
        return {}
    needed = sorted({pk for pair in pairs for pk in pair})
    details = {pk: (description, status) for pk, description, status in conn.execute(
        "SELECT id, description, status FROM jobs WHERE id = ANY(%s)", (needed,)).fetchall()}
    found: dict[int, list[dict]] = {}
    for pk, other in sorted(pairs):
        if description_similarity(details[pk][0], details[other][0]) >= POSSIBLE_DESCRIPTION:
            found.setdefault(pk, []).append({"id": other, "status": details[other][1]})
    return found


def place_words(location: str | None) -> set[str]:
    """City and suburb words in a location, e.g. {"sydney"} for "Sydney NSW 2000"."""
    return {w for w in re.findall(r"[a-z]{3,}", (location or "").casefold())
            if w not in NOT_PLACE_WORDS}


def same_place(a: str | None, b: str | None) -> bool:
    """False only when both locations name places and none are shared (e.g. Sydney vs
    Melbourne). Unknown or country-only locations never block a match."""
    words_a, words_b = place_words(a), place_words(b)
    return not words_a or not words_b or bool(words_a & words_b)


def identity(posting: Posting) -> tuple[str, str | None]:
    url = canonical_url(posting.url)
    if posting.source_id:
        return f"{posting.source}:{posting.source_id}", url
    if url:
        return f"url:{_digest(url)}", url
    parts = "|".join(_normalise(v) for v in (posting.company, posting.title, posting.location))
    return f"fingerprint:{_digest(parts + '|' + _normalise(posting.description)[:1500])}", None


def find_duplicate(conn, key: str, fp: str | None, mkey: str | None,
                   description: str | None, location: str | None) -> tuple[int, bool] | None:
    """Return (job id, is_first_copy) for an existing copy of this posting, if any.

    is_first_copy is True when this is the copy the jobs row was created from.
    Tried in order: this site's ID/URL on any known copy (job_sources), exact content
    fingerprint, then the same employer and title seen recently in the same place with
    a similar description (another site's copy, or a repost).
    """
    row = conn.execute("""SELECT s.job_id, j.canonical_key = s.source_key
                          FROM job_sources s JOIN jobs j ON j.id = s.job_id
                          WHERE s.source_key=%s""", (key,)).fetchone()
    if row:
        return row[0], row[1]
    if fp:
        row = conn.execute("SELECT id FROM jobs WHERE fingerprint=%s ORDER BY id LIMIT 1",
                           (fp,)).fetchone()
        if row:
            return row[0], False
    if mkey and description:
        candidates = conn.execute(
            """SELECT id, description, location FROM jobs
               WHERE match_key=%s AND first_seen_at > now() - make_interval(days => %s)
               ORDER BY first_seen_at DESC LIMIT 20""", (mkey, FUZZY_WINDOW_DAYS)).fetchall()
        for job_pk, existing, existing_location in candidates:
            if same_place(location, existing_location) and \
                    description_similarity(description, existing) >= SIMILAR_DESCRIPTION:
                return job_pk, False
    return None


def save_job(conn, run_pk: int, item: dict, job_filter: JobFilter | None = None) -> bool:
    """Upsert one scraped job and record the observation. Returns True if new.

    A new job failing job_filter is still stored, with status 'filtered' and the reason,
    so it stays out of the default view and scoring but remains a known duplicate.
    """
    posting = normalise(item)
    key, url = identity(posting)
    fp, mkey = fingerprint(posting), match_key(posting)
    description = posting.description or None
    raw = psycopg.types.json.Jsonb(item)
    found = find_duplicate(conn, key, fp, mkey, description, posting.location)
    if found is None:
        reason = job_filter(posting) if job_filter else None
        job_pk = conn.execute(
            """INSERT INTO jobs(canonical_key, source_job_id, canonical_url, title, company,
                   location, posted_at, description, raw_json, fingerprint, source, match_key,
                   salary_min, salary_max, status, filter_reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (key, posting.source_id, url, posting.title, posting.company, posting.location,
             posting.posted_at, description, raw, fp, posting.source, mkey,
             posting.salary_min, posting.salary_max, "filtered" if reason else "new",
             reason)).fetchone()[0]
    else:
        job_pk, is_first_copy = found
        # Another site's copy keeps the first site's record; its own copy is kept in
        # job_sources and as an observation. A longer description from any site is preferred.
        conn.execute(
            """UPDATE jobs SET last_seen_at=now(),
                   raw_json=CASE WHEN %s THEN %s ELSE raw_json END,
                   description=CASE WHEN length(coalesce(%s, '')) > length(coalesce(description, ''))
                                    THEN %s ELSE description END,
                   canonical_url=coalesce(canonical_url, %s),
                   posted_at=coalesce(posted_at, %s),
                   fingerprint=coalesce(fingerprint, %s),
                   source=coalesce(source, %s),
                   match_key=coalesce(match_key, %s),
                   salary_min=coalesce(salary_min, %s),
                   salary_max=coalesce(salary_max, %s)
               WHERE id=%s""",
            (is_first_copy, raw, description, description, url, posting.posted_at, fp,
             posting.source, mkey, posting.salary_min, posting.salary_max, job_pk))
    record_source(conn, job_pk, key, posting, url)
    conn.execute(
        """INSERT INTO job_observations(job_id, scraper_run_id, observed_json)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
        (job_pk, run_pk, raw))
    return found is None


def record_source(conn, job_pk: int, key: str, posting: Posting, url: str | None) -> None:
    conn.execute(
        """INSERT INTO job_sources(source_key, job_id, source, source_job_id, url)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (source_key) DO UPDATE SET last_seen_at=now(),
               url=coalesce(EXCLUDED.url, job_sources.url)""",
        (key, job_pk, posting.source, posting.source_id, url))


def save_run(actor_id: str, run_id: str, dataset_id: str | None, jobs: list[dict], *,
             started_at: datetime | None = None, finished_at: datetime | None = None,
             cost_usd: float | None = None, search_name: str | None = None,
             run_input: dict | None = None, job_filter: JobFilter | None = None) -> int:
    from ..db import connect

    with connect() as conn:
        run_pk = conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, items_seen, cost_usd, search_name, input)
               VALUES (%s, %s, %s, 'SUCCEEDED', coalesce(%s, now()), coalesce(%s, now()),
                       %s, %s, %s, %s)
               ON CONFLICT (apify_run_id) DO UPDATE SET dataset_id=EXCLUDED.dataset_id,
                   status='SUCCEEDED', started_at=EXCLUDED.started_at,
                   finished_at=EXCLUDED.finished_at, items_seen=EXCLUDED.items_seen,
                   cost_usd=EXCLUDED.cost_usd, error=NULL
               RETURNING id""",
            (actor_id, run_id, dataset_id, started_at, finished_at, len(jobs), cost_usd,
             search_name, psycopg.types.json.Jsonb(run_input) if run_input else None)
        ).fetchone()[0]
        return sum(save_job(conn, run_pk, job, job_filter) for job in jobs)


def record_failed_run(actor_id: str, run_id: str | None, dataset_id: str | None, status: str,
                      error: str, started_at: datetime | None = None,
                      finished_at: datetime | None = None, cost_usd: float | None = None,
                      search_name: str | None = None, run_input: dict | None = None) -> None:
    from ..db import connect

    with connect() as conn:
        conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, error, cost_usd, search_name, input)
               VALUES (%s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s)
               ON CONFLICT (apify_run_id) DO UPDATE SET status=EXCLUDED.status,
                   finished_at=EXCLUDED.finished_at, error=EXCLUDED.error,
                   cost_usd=EXCLUDED.cost_usd""",
            (actor_id, run_id, dataset_id, status, started_at, finished_at, error[:4000],
             cost_usd, search_name,
             psycopg.types.json.Jsonb(run_input) if run_input else None))


def backfill_sources(conn) -> int:
    """Record every earlier sighting's site copy in job_sources (run once, when the
    table is created; the migration itself only knows each job's first copy)."""
    rows = conn.execute("SELECT job_id, observed_json FROM job_observations").fetchall()
    before = conn.execute("SELECT count(*) FROM job_sources").fetchone()[0]
    for job_pk, raw in rows:
        posting = normalise(raw or {})
        key, url = identity(posting)
        conn.execute(
            """INSERT INTO job_sources(source_key, job_id, source, source_job_id, url)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_key) DO NOTHING""",
            (key, job_pk, posting.source, posting.source_id, url))
    return conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] - before


def backfill_keys(conn) -> int:
    """Fill fingerprint, source, and match_key for jobs imported before they existed."""
    rows = conn.execute("""SELECT id, raw_json FROM jobs
                           WHERE fingerprint IS NULL OR source IS NULL OR match_key IS NULL
                           """).fetchall()
    filled = 0
    for job_pk, raw in rows:
        posting = normalise(raw or {})
        values = (fingerprint(posting), posting.source, match_key(posting))
        conn.execute("""UPDATE jobs SET fingerprint=coalesce(fingerprint, %s),
                            source=coalesce(source, %s), match_key=coalesce(match_key, %s)
                        WHERE id=%s""", (*values, job_pk))
        filled += any(values)
    return filled
