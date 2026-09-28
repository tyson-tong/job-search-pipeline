"""Similar jobs for the tracker: postings the merge kept apart that may be one role
(a recruiter's re-post, a reworded title, a suburb), and one ad in several countries.
Only reported, never merged."""
from __future__ import annotations

from .dedupe import (POSSIBLE_DESCRIPTION, POSSIBLE_TITLE, description_similarity,
                     looks_like_same_role, same_country, similarity_level, simplify,
                     title_patterns, title_similarity)

# Similar jobs are looked for among jobs first seen this recently.
POSSIBLE_WINDOW_DAYS = 180
# A similar job at one of these may be a role you've already applied for.
IN_PROGRESS = ("applied", "interviewing", "offered", "accepted")


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
        for word in simplify(title).split():
            if len(word) > 3:
                by_word.setdefault(word, set()).add(pk)
    pairs: dict[tuple[int, int], float] = {}
    for pk in job_ids:
        words = [w for w in simplify(titles.get(pk)).split() if len(w) > 3]
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
