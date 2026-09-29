"""Similar jobs for the tracker: postings the merge kept apart that may be one role
(a recruiter's re-post, a reworded title, a suburb), and one ad in several countries.
Only reported, never merged."""
from __future__ import annotations

from .dedupe import (
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


def similar_jobs(conn, job_ids: list[int]) -> dict[int, list[dict]]:
    """Similar ads for each of these jobs, most alike first: {job id: [{id, status,
    country, same_country, level, title_similarity, similarity}]}. Two queries in all:
    titles first, then ads only for jobs with a close title. Only same_country matches
    are similar jobs; the others are one role advertised in several countries."""
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
    details = {pk: (description_words(description), status, country)
               for pk, description, status, country in conn.execute(
                   "SELECT id, description, status, country FROM jobs WHERE id = ANY(%s)",
                   (needed,)).fetchall()}
    found: dict[int, list[dict]] = {}
    for (pk, other), titles_alike in pairs.items():
        ads_alike = jaccard(details[pk][0], details[other][0])
        if ads_alike >= POSSIBLE_DESCRIPTION:
            found.setdefault(pk, []).append({
                "id": other, "status": details[other][1], "country": details[other][2],
                "same_country": same_country(details[pk][2], details[other][2]),
                "level": similarity_level(titles_alike, ads_alike),
                "title_similarity": titles_alike, "similarity": ads_alike})
    for similar in found.values():
        similar.sort(key=lambda s: (s["level"] == "highly similar", s["similarity"], -s["id"]),
                     reverse=True)
    return found
