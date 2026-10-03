"""Similar jobs for the tracker: postings the merge kept apart that may be one role
(a recruiter's re-post, a reworded title, a suburb), and one ad in several countries.
Only reported, never merged."""
from __future__ import annotations

import threading

from .dedupe import (
    HIGHLY_SIMILAR,
    POSSIBLE_DESCRIPTION,
    POSSIBLE_TITLE,
    description_words,
    jaccard,
    same_country,
    similarity_level,
    simplify,
    title_words,
)

# Similar jobs are looked for among jobs first seen this recently.
POSSIBLE_WINDOW_DAYS = 180
# A similar job at one of these may be a role you've already applied for.
IN_PROGRESS = ("applied", "interviewing", "offered", "accepted")
# Each ad's words, by job id, with the ad's length when they were read: the tracker asks
# for similar jobs on every refresh, and fetching every ad each time is slow from a
# database far away. An ad changes only by being replaced with a longer one, or cleared.
_AD_WORDS: dict[int, tuple[int, set[str]]] = {}
_AD_WORDS_MAX = 20_000
_ad_words_lock = threading.Lock()


def _ad_words(conn, job_ids: list[int]) -> dict[int, tuple[set[str], str, str | None]]:
    """{job id: (its ad's words, status, country)}, fetching only ads not read before."""
    rows = conn.execute("""SELECT id, status, country, coalesce(length(description), 0)
                           FROM jobs WHERE id = ANY(%s)""", (job_ids,)).fetchall()
    with _ad_words_lock:
        missing = [pk for pk, _, _, length in rows if _AD_WORDS.get(pk, (None,))[0] != length]
    if missing:
        fetched = conn.execute("""SELECT id, coalesce(length(description), 0), description
                                  FROM jobs WHERE id = ANY(%s)""", (missing,)).fetchall()
        with _ad_words_lock:
            if len(_AD_WORDS) + len(fetched) > _AD_WORDS_MAX:
                _AD_WORDS.clear()
            for pk, length, description in fetched:
                _AD_WORDS[pk] = (length, description_words(description))
    with _ad_words_lock:
        return {pk: (_AD_WORDS[pk][1] if pk in _AD_WORDS else set(), status, country)
                for pk, status, country, _ in rows}


def similar_jobs(conn, job_ids: list[int]) -> dict[int, list[dict]]:
    """Similar ads for each of these jobs, most alike first: {job id: [{id, status,
    country, same_country, level, title_similarity, similarity}]}. Titles first, then
    ads only for jobs with a close title (only those not read before: _ad_words). Only
    same_country matches are similar jobs; a job in another country is listed only when its
    ad is at least HIGHLY_SIMILAR, since the tracker calls it the same ad there (one role
    advertised in several countries)."""
    if not job_ids:
        return {}
    rows = conn.execute(
        """SELECT id, title FROM jobs
           WHERE first_seen_at > now() - make_interval(days => %s) OR id = ANY(%s)""",
        (POSSIBLE_WINDOW_DAYS, job_ids)).fetchall()
    titles = {pk: title for pk, title in rows}
    words = {pk: set(simplify(title).split()) for pk, title in titles.items()}
    by_word: dict[str, set[int]] = {}
    for pk, title_set in words.items():
        for word in title_set:
            by_word.setdefault(word, set()).add(pk)
    pairs: dict[tuple[int, int], float] = {}
    for pk in job_ids:
        # Candidates share one of the title's longer words (or any word of a short title).
        candidates = set().union(*(by_word.get(w, set()) for w in title_words(titles.get(pk))))
        for other in candidates - {pk}:
            alike = jaccard(words[pk], words[other])
            if alike >= POSSIBLE_TITLE:
                pairs[(pk, other)] = alike
    if not pairs:
        return {}
    needed = sorted({pk for pair in pairs for pk in pair})
    details = _ad_words(conn, needed)
    found: dict[int, list[dict]] = {}
    for (pk, other), titles_alike in pairs.items():
        ads_alike = jaccard(details[pk][0], details[other][0])
        home = same_country(details[pk][2], details[other][2])
        if ads_alike >= (POSSIBLE_DESCRIPTION if home else HIGHLY_SIMILAR):
            found.setdefault(pk, []).append({
                "id": other, "status": details[other][1], "country": details[other][2],
                "same_country": home,
                "level": similarity_level(titles_alike, ads_alike),
                "title_similarity": titles_alike, "similarity": ads_alike})
    for similar in found.values():
        similar.sort(key=lambda s: (s["level"] == "highly similar", s["similarity"], -s["id"]),
                     reverse=True)
    return found
