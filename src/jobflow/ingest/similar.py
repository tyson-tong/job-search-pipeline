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
    title_key,
    title_words,
)

# Similar jobs are looked for among jobs first seen this recently.
POSSIBLE_WINDOW_DAYS = 180
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


class _TitleIndex:
    """Recent jobs' title words, and the title-alike jobs found for each job asked about,
    kept between the tracker's refreshes: rebuilding them each time read every recent title
    and compared each listed job with all of them. A title never changes once saved, so only
    new jobs are read and compared; jobs that leave the window are dropped."""

    def __init__(self) -> None:
        self.words: dict[int, set[str]] = {}
        self.by_word: dict[str, set[int]] = {}
        # {job id: {other job id: how alike the titles are}}, at least POSSIBLE_TITLE alike,
        # against every job in `words`: worked out when a job is first asked about, then
        # kept up to date as jobs are added.
        self.alike: dict[int, dict[int, float]] = {}
        self.lock = threading.Lock()

    def _candidates(self, pk: int) -> set[int]:
        """Jobs sharing one of the title's longer words (or any word of a short title)."""
        return set().union(*(self.by_word.get(w, set())
                             for w in title_words(" ".join(self.words[pk])))) - {pk}

    def _alike(self, pk: int, others) -> dict[int, float]:
        found = {}
        for other in others:
            alike = jaccard(self.words[pk], self.words[other])
            if alike >= POSSIBLE_TITLE:
                found[other] = alike
        return found

    def update(self, conn, job_ids: list[int]) -> set[int]:
        """Bring the index in line with the jobs first seen in POSSIBLE_WINDOW_DAYS, and
        job_ids; returns their ids. Call with the lock held."""
        current = {pk for (pk,) in conn.execute(
            """SELECT id FROM jobs
               WHERE first_seen_at > now() - make_interval(days => %s) OR id = ANY(%s)""",
            (POSSIBLE_WINDOW_DAYS, job_ids)).fetchall()}
        for pk in self.words.keys() - current:   # out of the window
            for word in self.words.pop(pk):
                self.by_word[word].discard(pk)
            self.alike.pop(pk, None)
        added = sorted(current - self.words.keys())
        for pk, title in conn.execute("SELECT id, title FROM jobs WHERE id = ANY(%s)",
                                      (added,)).fetchall():
            self.words[pk] = set(title_key(title).split())
            for word in self.words[pk]:
                self.by_word.setdefault(word, set()).add(pk)
        # Each new job joins the alike jobs of the jobs already worked out.
        for pk in added:
            for other, alike in self._alike(pk, self._candidates(pk) & self.alike.keys()).items():
                self.alike[other][pk] = alike
        return current

    def similar(self, pk: int, current: set[int]) -> dict[int, float]:
        """{job id: how alike its title is} for this job's title-alike jobs in `current`.
        Call with the lock held, after update."""
        if pk not in self.alike:
            self.alike[pk] = self._alike(pk, self._candidates(pk))
        return {other: alike for other, alike in self.alike[pk].items() if other in current}


_TITLES = _TitleIndex()


def similar_jobs(conn, job_ids: list[int]) -> dict[int, list[dict]]:
    """Similar ads for each of these jobs, most alike first: {job id: [{id, status,
    country, same_country, level, title_similarity, similarity}]}. Titles first (from
    _TITLES), then ads only for jobs with a close title (only those not read before:
    _ad_words). Only same_country matches are similar jobs; a job in another country is
    listed only when its ad is at least HIGHLY_SIMILAR, since the tracker calls it the same
    ad there (one role advertised in several countries)."""
    if not job_ids:
        return {}
    pairs: dict[tuple[int, int], float] = {}
    with _TITLES.lock:
        current = _TITLES.update(conn, job_ids)
        for pk in job_ids:
            if pk in current:
                pairs.update(((pk, other), alike)
                             for other, alike in _TITLES.similar(pk, current).items())
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
