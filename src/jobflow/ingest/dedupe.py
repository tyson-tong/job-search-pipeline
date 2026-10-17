"""The rules for recognising one job in several postings: links without tracking
parameters, employer and title keys, fingerprints, and how alike two ads are.
Pure functions, used when saving postings (store.py) and finding similar jobs (similar.py)."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..text import words
from .options import LINKEDIN_COUNTRIES
from .sources import Posting

TRACKING_PARAMS = {"trk", "trackingid", "refid", "position", "pagenum", "from", "tk", "vjk",
                   "type", "ref", "origin"}
# Words that differ between sites' spellings of the same employer.
COMPANY_NOISE = {"pty", "ltd", "limited", "inc", "llc", "llp", "plc", "pte", "gmbh", "sdn",
                 "bhd", "co", "corp", "corporation", "company", "group", "holdings", "the",
                 "australia", "nz", "singapore", "hk"}
# Two postings with the same employer, title, and city are the same job when nearly all of
# the shorter ad's words are in the longer one: a site's cut-down or padded copy of one ad.
# Word overlap alone can't tell that from two roles sharing an employer's standard text
# (about us, benefits, diversity), which made up 85% of two different banking roles' words;
# each role's own duties keep it below this. An edited repost falls below it too, and is
# shown as a similar job instead: a missed merge leaves a flagged duplicate, a wrong one
# loses a job.
SAME_JOB_CONTAINMENT = 0.95
# A short ad (under SAME_AD_MIN_WORDS words) is a copy only when the two share at least this
# much of their words: a site may add a word or two ("Hybrid"), not a whole other ad.
SHORT_AD_SHARE = 0.8
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
# the employer differently, e.g. "Shenwan Hongyuan (H.K.)" on LinkedIn and "Shenwan
# Hongyuan Securities (H.K.)" on Seek. The title must match exactly: employers reuse one ad
# for different roles (Susquehanna's research and trading internships). Short ads are left
# out: boilerplate such as "Apply on our website" is identical across unrelated jobs. One
# ad for offices in two cities, or two countries, stays two jobs (see same_place).
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
# Words many different places' names share ("North Sydney" and "North Melbourne", "Port
# Melbourne" and "Port Adelaide", "San Francisco" and "San Jose"): shared, they don't make two
# locations one place, or one employer's ad for two such offices would merge. A location named
# by these alone ("Central") still counts as a place.
GENERIC_PLACE_WORDS = {"north", "south", "east", "west", "upper", "lower", "inner", "outer",
                       "port", "park", "hill", "hills", "mount", "beach", "bay", "point",
                       "heights", "valley", "island", "lake", "river", "creek", "harbour",
                       "harbor", "gardens", "grove", "springs", "ridge", "village", "san",
                       "santa", "saint", "fort", "old"}
# Australian states and territories, by their codes and names: two locations in different
# ones are two places, even when their suburbs share a name (Richmond VIC and Richmond NSW).
AU_STATES = {"nsw": "nsw", "new south wales": "nsw", "vic": "vic", "victoria": "vic",
             "qld": "qld", "queensland": "qld", "tas": "tas", "tasmania": "tas", "act": "act",
             "australian capital territory": "act", "wa": "wa", "western australia": "wa",
             "sa": "sa", "south australia": "sa", "nt": "nt", "northern territory": "nt"}
_AU_STATE = re.compile(r"\b(" + "|".join(sorted(AU_STATES, key=len, reverse=True)) + r")\b")


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
    """Like text.words, but dropping + and # as well. Kept apart because the fingerprints
    stored for every job are made from it: changing it would change every new fingerprint,
    so a repost of a job already saved would no longer match it. Titles, where "C++" and
    "C#" must stay apart, go through title_key instead."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def title_key(title: object) -> str:
    """A title as the rules compare it: ignoring case and punctuation, but not + and #, so
    "C++ Developer" and "C# Developer" stay two titles (simplify would make both "c
    developer", and merge one ad reused for the two roles)."""
    return words(title)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def company_key(company: str | None) -> str:
    return " ".join(w for w in simplify(company).split() if w not in COMPANY_NOISE)


def match_key(posting: Posting) -> str | None:
    """Employer + title, spelled the same way whichever site it came from."""
    company, title = company_key(posting.company), title_key(posting.title)
    if not company or not title or title == "untitled":
        return None
    return f"{company}|{title}"


def fingerprint(posting: Posting) -> str | None:
    """Exact content fingerprint; None when company, title, or description is missing."""
    company, title = simplify(posting.company), title_key(posting.title)
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


def is_copy_of_ad(a: str | None, b: str | None) -> bool:
    """True when one ad is a copy of the other, cut down or padded: at least
    SAME_JOB_CONTAINMENT of the shorter ad's words are in the longer one. A short ad
    (under SAME_AD_MIN_WORDS words) must also be most of the longer one (SHORT_AD_SHARE of
    their words together), not just inside it: a stub such as "Join Acme as an analyst,
    apply now" is inside every Acme analyst ad."""
    words_a, words_b = description_words(a), description_words(b)
    if not words_a or not words_b:
        return False
    shorter = min(len(words_a), len(words_b))
    if len(words_a & words_b) / shorter < SAME_JOB_CONTAINMENT:
        return False
    return shorter >= SAME_AD_MIN_WORDS or jaccard(words_a, words_b) >= SHORT_AD_SHARE


def title_words(title: str | None) -> list[str]:
    """The words that find titles like this one: those over 3 letters, or every word of a
    title with none."""
    found = title_key(title).split()
    return [w for w in found if len(w) > 3] or found


def title_patterns(title: str | None) -> list[str]:
    """ILIKE patterns that narrow titles in SQL before they are compared in Python."""
    return [f"%{w}%" for w in title_words(title)]


def same_country(a: str | None, b: str | None) -> bool:
    """False only when both countries are known and differ."""
    return not (a and b and a != b)


def is_same_ad(title_a: str | None, description_a: str | None,
               title_b: str | None, description_b: str | None,
               country_a: str | None = None, country_b: str | None = None,
               location_a: str | None = None, location_b: str | None = None) -> bool:
    """True when two postings carry the same ad under the same title (ignoring case and
    punctuation), whoever posted it, in the same city of the same country. An unknown
    country, or a location naming no city, doesn't block a match."""
    if not same_country(country_a, country_b) or not same_place(location_a, location_b):
        return False
    if not title_key(title_a) or title_key(title_a) != title_key(title_b):
        return False
    if min(len(description_words(description_a)),
           len(description_words(description_b))) < SAME_AD_MIN_WORDS:
        return False
    return description_similarity(description_a, description_b) >= SAME_AD


def similarity_level(title: float, description: float) -> str:
    """How the tracker describes a pair of similar jobs."""
    return ("highly similar" if title >= POSSIBLE_TITLE and description >= HIGHLY_SIMILAR
            else "similar")


def place_words(location: str | None) -> set[str]:
    """City and suburb words in a location, e.g. {"sydney"} for "Sydney NSW 2000" and for
    "North Sydney NSW": generic words such as "north" only when the location has no other."""
    found = {w for w in re.findall(r"[a-z]{3,}", (location or "").casefold())
             if w not in NOT_PLACE_WORDS}
    return found - GENERIC_PLACE_WORDS or found


def au_states(location: str | None) -> set[str]:
    """The Australian states a location names, by code: {"vic"} for "Richmond VIC"."""
    return {AU_STATES[m] for m in _AU_STATE.findall((location or "").casefold())}


def same_place(a: str | None, b: str | None) -> bool:
    """False only when both locations name places and none are shared (Sydney vs
    Melbourne, North Sydney vs North Melbourne), or both name Australian states and they
    differ (Richmond VIC vs Richmond NSW). Unknown or country-only locations never block a
    match."""
    states_a, states_b = au_states(a), au_states(b)
    if states_a and states_b and not states_a & states_b:
        return False
    words_a, words_b = place_words(a), place_words(b)
    return not words_a or not words_b or bool(words_a & words_b)


def identity(posting: Posting) -> tuple[str, str | None]:
    url = canonical_url(posting.url)
    if posting.source_id:
        return f"{posting.source}:{posting.source_id}", url
    if url:
        return f"url:{_digest(url)}", url
    parts = "|".join((simplify(posting.company), title_key(posting.title),
                      simplify(posting.location)))
    return f"fingerprint:{_digest(parts + '|' + simplify(posting.description)[:1500])}", None
