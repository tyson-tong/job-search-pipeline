"""The rules for recognising one job in several postings: links without tracking
parameters, employer and title keys, fingerprints, and how alike two ads or titles are.
Pure functions, used when saving postings (store.py) and finding similar jobs (similar.py)."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .options import LINKEDIN_COUNTRIES
from .sources import Posting

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
# Similar jobs (titles at least POSSIBLE_TITLE alike) whose ads are at least this alike
# are shown as "highly similar"; the rest as "similar".
HIGHLY_SIMILAR = 0.8
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


def simplify(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def company_key(company: str | None) -> str:
    return " ".join(w for w in simplify(company).split() if w not in COMPANY_NOISE)


def match_key(posting: Posting) -> str | None:
    """Employer + title, spelled the same way whichever site it came from."""
    company, title = company_key(posting.company), simplify(posting.title)
    if not company or not title or title == "untitled":
        return None
    return f"{company}|{title}"


def fingerprint(posting: Posting) -> str | None:
    """Exact content fingerprint; None when company, title, or description is missing."""
    company, title = simplify(posting.company), simplify(posting.title)
    description = simplify(posting.description)[:1500]
    if not (company and title and description):
        return None
    return _digest("|".join((company, title, simplify(posting.location), description)))


def description_words(text: str | None) -> set[str]:
    return {w for w in simplify(text).split() if len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    """How alike two word sets are: the words they share over all their words."""
    return len(a & b) / len(a | b) if a and b else 0.0


def description_similarity(a: str | None, b: str | None) -> float:
    return jaccard(description_words(a), description_words(b))


def title_similarity(a: str | None, b: str | None) -> float:
    return jaccard(set(simplify(a).split()), set(simplify(b).split()))


def title_words(title: str | None) -> list[str]:
    """The words that find titles like this one: those over 3 letters, or every word of a
    title with none."""
    words = simplify(title).split()
    return [w for w in words if len(w) > 3] or words


def title_patterns(title: str | None) -> list[str]:
    """ILIKE patterns that narrow titles in SQL before they are compared in Python."""
    return [f"%{w}%" for w in title_words(title)]


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
    if not simplify(title_a) or simplify(title_a) != simplify(title_b):
        return False
    if min(len(description_words(description_a)),
           len(description_words(description_b))) < SAME_AD_MIN_WORDS:
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
    parts = "|".join(simplify(v) for v in (posting.company, posting.title, posting.location))
    return f"fingerprint:{_digest(parts + '|' + simplify(posting.description)[:1500])}", None
