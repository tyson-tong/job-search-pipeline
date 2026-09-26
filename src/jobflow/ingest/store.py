from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg.types.json

from .options import LINKEDIN_COUNTRIES
from .searches import country_from_location, run_country, search_url_country
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
# Similar jobs: copies the merge didn't catch, e.g. a recruiter re-posting an employer's
# ad under its own name. Only reported, never merged, so the bar is lower and employer
# and city are ignored.
POSSIBLE_TITLE = 0.6
POSSIBLE_DESCRIPTION = 0.4
POSSIBLE_WINDOW_DAYS = 180
# Similar jobs (titles at least POSSIBLE_TITLE alike) whose ads are at least this alike
# are shown as "highly similar"; the rest as "similar".
HIGHLY_SIMILAR = 0.8
IN_PROGRESS = ("applied", "interviewing", "offered", "accepted")
# Ads at least this alike under the same title are the same job even when the sites spell
# the employer or place differently, e.g. "Shenwan Hongyuan (H.K.)" on LinkedIn and
# "Shenwan Hongyuan Securities (H.K.)" in Admiralty on Seek. The title must match exactly:
# employers reuse one ad for different roles (Susquehanna's research and trading
# internships). Short ads are left out: boilerplate such as "Apply on our website" is
# identical across unrelated jobs. One ad for offices in two countries stays two jobs.
SAME_AD = 0.95
SAME_AD_MIN_WORDS = 30
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
    # Only web links: a scraped "javascript:" URL would run script when clicked.
    if parts.scheme.casefold() not in ("http", "https") or not parts.netloc:
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


def _description_words(text: str | None) -> set[str]:
    return {w for w in _normalise(text).split() if len(w) > 2}


def description_similarity(a: str | None, b: str | None) -> float:
    words_a, words_b = _description_words(a), _description_words(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def title_similarity(a: str | None, b: str | None) -> float:
    words_a, words_b = set(_normalise(a).split()), set(_normalise(b).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def title_patterns(title: str | None) -> list[str]:
    """ILIKE patterns that narrow titles in SQL before they are compared in Python."""
    words = [w for w in _normalise(title).split() if len(w) > 3] or _normalise(title).split()
    return [f"%{w}%" for w in words]


def same_country(a: str | None, b: str | None) -> bool:
    """False only when both countries are known and differ."""
    return not (a and b and a != b)


def is_same_ad(title_a: str | None, description_a: str | None,
               title_b: str | None, description_b: str | None,
               country_a: str | None = None, country_b: str | None = None) -> bool:
    """True when two postings carry the same ad under the same title (ignoring case and
    punctuation), whoever posted it and wherever in one country it is listed. An unknown
    country doesn't block a match."""
    if not same_country(country_a, country_b):
        return False
    if not _normalise(title_a) or _normalise(title_a) != _normalise(title_b):
        return False
    if min(len(_description_words(description_a)),
           len(_description_words(description_b))) < SAME_AD_MIN_WORDS:
        return False
    return description_similarity(description_a, description_b) >= SAME_AD


def looks_like_same_role(title_a: str | None, description_a: str | None,
                         title_b: str | None, description_b: str | None) -> float | None:
    """Description similarity if two ads look like the same role whoever posted it and
    wherever it is, else None."""
    if title_similarity(title_a, title_b) < POSSIBLE_TITLE:
        return None
    similarity = description_similarity(description_a, description_b)
    return similarity if similarity >= POSSIBLE_DESCRIPTION else None


def similarity_level(title: float, description: float) -> str:
    """How the tracker and tailor-cv describe a pair of similar jobs."""
    return ("highly similar" if title >= POSSIBLE_TITLE and description >= HIGHLY_SIMILAR
            else "similar")


def possible_duplicates(job_id: int, limit: int = 5,
                        other_countries: bool = False) -> list[dict]:
    """Other jobs in the same country that may be the same role as this one but weren't
    merged; with other_countries, the similar ads found in other countries instead (one
    role advertised for several countries' offices, which is not a duplicate)."""
    from ..db import connect

    with connect() as conn:
        job = conn.execute("SELECT title, description, country FROM jobs WHERE id=%s",
                           (job_id,)).fetchone()
        if not job or not job[1]:
            return []
        title, description, country = job
        patterns = title_patterns(title)
        if not patterns:
            return []
        # Cheap title prefilter in SQL; descriptions are only loaded for close titles.
        rows = conn.execute(
            """SELECT id, title FROM jobs
               WHERE id <> %s AND first_seen_at > now() - make_interval(days => %s)
                 AND title ILIKE ANY(%s)""",
            (job_id, POSSIBLE_WINDOW_DAYS, patterns)).fetchall()
        close = [pk for pk, other in rows if title_similarity(title, other) >= POSSIBLE_TITLE]
        if not close:
            return []
        rows = conn.execute(
            """SELECT j.id, j.title, j.company, j.location, j.status, j.description,
                      (SELECT url FROM job_sources s WHERE s.job_id = j.id
                       ORDER BY s.first_seen_at LIMIT 1), j.country
               FROM jobs j WHERE j.id = ANY(%s)""", (close[:100],)).fetchall()
    matches = []
    for pk, other_title, company, location, status, other_description, url, other_country in rows:
        if same_country(country, other_country) == other_countries:
            continue
        similarity = looks_like_same_role(title, description, other_title, other_description)
        if similarity is not None:
            titles = title_similarity(title, other_title)
            matches.append({"id": pk, "title": other_title, "company": company,
                            "location": location, "status": status, "url": url,
                            "country": other_country, "similarity": similarity,
                            "title_similarity": titles,
                            "level": similarity_level(titles, similarity)})
    return sorted(matches, key=lambda m: (m["level"] == "highly similar", m["similarity"]),
                  reverse=True)[:limit]


def possible_duplicate_map(conn, job_ids: list[int]) -> dict[int, list[dict]]:
    """Similar ads for many jobs at once: {job id: [{id, status, country, same_country,
    level}]}, using two queries in total (titles first, then descriptions only for close
    titles).
    Only same_country matches are similar jobs; the others are one role advertised
    in several countries."""
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
    pairs: dict[tuple[int, int], float] = {}
    for pk in job_ids:
        words = [w for w in _normalise(titles.get(pk)).split() if len(w) > 3]
        candidates = set().union(*(by_word.get(w, set()) for w in words)) - {pk}
        for other in candidates:
            similarity = title_similarity(titles[pk], titles[other])
            if similarity >= POSSIBLE_TITLE:
                pairs[(pk, other)] = similarity
    if not pairs:
        return {}
    needed = sorted({pk for pair in pairs for pk in pair})
    details = {pk: (description, status, country) for pk, description, status, country in
               conn.execute("SELECT id, description, status, country FROM jobs WHERE id = ANY(%s)",
                            (needed,)).fetchall()}
    found: dict[int, list[dict]] = {}
    for (pk, other), titles_alike in sorted(pairs.items()):
        ads_alike = description_similarity(details[pk][0], details[other][0])
        if ads_alike >= POSSIBLE_DESCRIPTION:
            found.setdefault(pk, []).append({
                "id": other, "status": details[other][1], "country": details[other][2],
                "same_country": same_country(details[pk][2], details[other][2]),
                "level": similarity_level(titles_alike, ads_alike)})
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


def find_duplicate(conn, key: str, fp: str | None, mkey: str | None, title: str | None,
                   description: str | None, location: str | None,
                   country: str | None = None) -> tuple[int, bool] | None:
    """Return (job id, is_first_copy) for an existing copy of this posting, if any.

    is_first_copy is True when this is the copy the jobs row was created from.
    Tried in order: this site's ID/URL on any known copy (job_sources), exact content
    fingerprint, the same employer and title seen recently in the same place with a
    similar description (another site's copy, or a repost), then a recent job with the
    same title and a near-identical ad under any employer or place in the same country
    (is_same_ad).
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
    patterns = title_patterns(title)
    if description and patterns and title != "Untitled":
        # Titles first; descriptions are only loaded for the same title.
        rows = conn.execute(
            """SELECT id, title FROM jobs
               WHERE first_seen_at > now() - make_interval(days => %s) AND title ILIKE ANY(%s)
               ORDER BY first_seen_at DESC""", (FUZZY_WINDOW_DAYS, patterns)).fetchall()
        close = [pk for pk, other in rows if _normalise(other) == _normalise(title)]
        if close:
            candidates = conn.execute(
                """SELECT id, title, description, country FROM jobs WHERE id = ANY(%s)
                   ORDER BY first_seen_at DESC""", (close[:50],)).fetchall()
            for job_pk, other_title, existing, other_country in candidates:
                if is_same_ad(title, description, other_title, existing, country, other_country):
                    return job_pk, False
    return None


def save_job(conn, run_pk: int, item: dict, job_filter: JobFilter | None = None,
             country: str | None = None) -> bool:
    """Upsert one scraped job and record the observation. Returns True if new.

    country is the one country the run searched, if known; otherwise it comes from the
    job's own LinkedIn search URL, else from its location where that names one.

    A new job failing job_filter is still stored, with status 'filtered' and the reason,
    so it stays out of the Inbox and scoring but remains a known duplicate.
    """
    posting = normalise(item)
    key, url = identity(posting)
    fp, mkey = fingerprint(posting), match_key(posting)
    description = posting.description or None
    raw = psycopg.types.json.Jsonb(item)
    country = country or search_url_country(item) or country_from_location(posting.location)
    found = find_duplicate(conn, key, fp, mkey, posting.title, description, posting.location,
                           country)
    if found is None:
        reason = job_filter(posting) if job_filter else None
        job_pk = conn.execute(
            """INSERT INTO jobs(canonical_key, source_job_id, canonical_url, title, company,
                   location, posted_at, description, raw_json, fingerprint, source, match_key,
                   salary_min, salary_max, status, filter_reason, country)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (key, posting.source_id, url, posting.title, posting.company, posting.location,
             posting.posted_at, description, raw, fp, posting.source, mkey,
             posting.salary_min, posting.salary_max, "filtered" if reason else "new",
             reason, country)).fetchone()[0]
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
                   salary_max=coalesce(salary_max, %s),
                   country=coalesce(country, %s)
               WHERE id=%s""",
            (is_first_copy, raw, description, description, url, posting.posted_at, fp,
             posting.source, mkey, posting.salary_min, posting.salary_max, country, job_pk))
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
             run_input: dict | None = None, job_filter: JobFilter | None = None,
             country: str | None = None) -> int:
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
        return sum(save_job(conn, run_pk, job, job_filter, country) for job in jobs)


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


def exclude_inbox_titles(searches: list, dry_run: bool = False) -> tuple[list[dict], int]:
    """Apply exclude_title_terms to jobs already in the Inbox: each New job whose title
    contains a term of the search that found it, and none of its include_title_terms,
    becomes 'filtered', with the reason an import would record, and the change is
    recorded in status_history. Only New jobs are touched. Returns (the jobs matched, the
    number of Inbox jobs checked); with dry_run, nothing is changed."""
    from ..db import connect
    from .searches import excluded_title_term

    terms_by_search = {search.name: (search.exclude_title_terms, search.include_title_terms)
                       for search in searches}
    with connect() as conn:
        # A job's search is the one whose run first found it.
        rows = conn.execute(
            """SELECT j.id, j.title, j.company, r.search_name FROM jobs j
               LEFT JOIN LATERAL (SELECT scraper_run_id FROM job_observations o
                                  WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
               LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id
               WHERE j.status = 'new' ORDER BY j.id""").fetchall()
        matched = []
        for job_id, title, company, search_name in rows:
            term = excluded_title_term(title, *terms_by_search.get(search_name, ((), ())))
            if term:
                matched.append({"id": job_id, "title": title, "company": company,
                                "reason": f"title contains {term!r}"})
        if not dry_run:
            for job in matched:
                changed = conn.execute(
                    """UPDATE jobs SET status='filtered', filter_reason=%s,
                           status_updated_at=now() WHERE id=%s AND status='new'""",
                    (job["reason"], job["id"])).rowcount
                if changed:
                    conn.execute("""INSERT INTO status_history(job_id, from_status, to_status, via)
                                    VALUES (%s, 'new', 'filtered', 'update-searches')""",
                                 (job["id"],))
    return matched, len(rows)


def backfill_countries(conn) -> int:
    """Fill jobs.country from the run that first found each job (run once, when the
    column is added), falling back to the job's own search URL, then its location."""
    rows = conn.execute(
        """SELECT j.id, j.location, r.actor_id, r.input, first.observed_json FROM jobs j
           LEFT JOIN LATERAL (SELECT scraper_run_id, observed_json FROM job_observations o
                              WHERE o.job_id = j.id ORDER BY observed_at LIMIT 1) first ON true
           LEFT JOIN scraper_runs r ON r.id = first.scraper_run_id
           WHERE j.country IS NULL""").fetchall()
    filled = 0
    for job_pk, location, actor_id, run_input, item in rows:
        site = (actor_id or "").split(":", 1)[0]
        country = (run_country(site, run_input) or search_url_country(item)
                   or country_from_location(location))
        if country:
            conn.execute("UPDATE jobs SET country=%s WHERE id=%s", (country, job_pk))
            filled += 1
    return filled


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
