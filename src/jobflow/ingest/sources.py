"""Normalise job records from different Apify scrapers into one shape.

Supported scrapers (detected from each record's fields):
- linkedin: curious_coder/linkedin-jobs-scraper
- indeed:   curious_coder/indeed-scraper
- seek:     websift/seek-job-scraper
Anything else is read with LinkedIn-style field names.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from urllib.parse import urlsplit

from .. import settings
from .options import (
    INDEED_JOB_TYPES,
    INDEED_WORK_ARRANGEMENTS,
    LINKEDIN_JOB_TYPES,
    LINKEDIN_WORK_ARRANGEMENTS,
    SEEK_OUTPUT_JOB_TYPES,
    SEEK_OUTPUT_WORK_ARRANGEMENTS,
)

# Multipliers to an annual amount. Hourly assumes a 38-hour week, daily a 5-day week.
ANNUAL_FACTOR = {"hourly": 38 * 52, "daily": 5 * 52, "weekly": 52, "monthly": 12,
                 "annual": 1}
PERIOD_WORDS = (
    ("hourly", r"hour|hr\b|/h\b|p\.?h\b|ph\b"),
    ("daily", r"\bday|daily|p\.?d\b|/d\b"),
    ("weekly", r"week|/wk|p\.?w\b"),
    ("monthly", r"month|/mo\b|p\.?m\b"),
    ("annual", r"year|annum|annual|p\.?a\b|/yr|\byr\b"),
)
INDEED_PERIODS = {"HOUR": "hourly", "DAY": "daily", "WEEK": "weekly", "MONTH": "monthly",
                  "YEAR": "annual"}
# The amounts each period's pay can plausibly be, in any of the searched currencies (a
# Hong Kong salary is often HK$100,000 a month). A period word next to an amount outside
# its range is about something else, as in "$120k package, flexible hours".
PLAUSIBLE = {"hourly": (5, 1_000), "daily": (50, 10_000), "weekly": (200, 50_000),
             "monthly": (500, 500_000), "annual": (1_000, math.inf)}
# An amount with no period stated below this is monthly pay, and above it annual. Hong Kong
# and Singapore salaries are usually quoted by the month, and in larger numbers.
MONTHLY_BELOW = {"HK": 200_000, "SG": 30_000}
MONTHLY_BELOW_ELSEWHERE = 20_000
# An annual salary above this is a misread, never a real offer; the database's salary
# columns also can't hold much more, and one value too big would fail a whole import.
MAX_ANNUAL = 50_000_000


@dataclass(frozen=True)
class Posting:
    source: str
    source_id: str | None
    url: str | None
    title: str
    company: str | None
    location: str | None
    posted_at: date | None
    description: str
    # Annualised salary range, when the posting states one.
    salary_min: float | None = None
    salary_max: float | None = None
    # The posting gave no pay period, so it was guessed from the amount's size: "$18,000"
    # could be a month's pay or a year's. Such a salary never filters a job out.
    salary_estimated: bool = False
    # Common vocabulary (options.JOB_TYPES / WORK_ARRANGEMENTS); empty = unknown.
    job_types: frozenset[str] = frozenset()
    work_arrangements: frozenset[str] = frozenset()


def _get(item: dict, *paths: str):
    """First non-empty value among dotted paths, e.g. 'advertiser.name'."""
    for path in paths:
        value: object = item
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def html_to_text(value: object) -> str:
    """The text of an ad, one line per paragraph, list item, or heading, so the job panel
    shows its structure. Duplicate matching ignores the line breaks."""
    text = re.sub(r"<(br|/p|/li|/h\d|/div)\b[^>]*>", "\n", str(value or ""), flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def to_date(value: object) -> date | None:
    """ISO strings, ISO datetimes, or Unix timestamps in seconds or milliseconds. A moment
    in time (a timestamp, or a datetime with its UTC offset) is dated in your timezone."""
    if value in (None, ""):
        return None
    # A string of digits is a timestamp only if it's long enough to be one: "20240101"
    # is a date, not 8 hours into 1970.
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()
                                           and len(value) >= 10):
        number = float(value)
        try:
            return datetime.fromtimestamp(number / 1000 if number > 1e11 else number,
                                          tz=settings.timezone()).date()
        except (OverflowError, OSError, ValueError):   # out of range, or nan
            return None
    text = str(value)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = None
    if moment is not None and moment.tzinfo is not None:
        return moment.astimezone(settings.timezone()).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _period_from_amount(low: float, high: float, country: str | None = None) -> str:
    """The period of amounts with no period stated, from their size and the job's country.
    Where pay is quoted by the month (Hong Kong, Singapore), S$2,500 is a month's pay, not a
    day's, and so is a range that starts under the monthly cutoff (S$25,000 - 35,000)."""
    monthly_below = MONTHLY_BELOW.get(country or "")
    if high < 300:
        return "hourly"
    if high < (1000 if monthly_below else 3000):
        return "daily"
    amount = low if monthly_below else high
    return "monthly" if amount < (monthly_below or MONTHLY_BELOW_ELSEWHERE) else "annual"


def _period(text: str, span: tuple[int, int], low: float, high: float,
            ignore: list[tuple[int, int]], country: str | None) -> tuple[str, bool]:
    """The period the salary text states for the amounts at text[span[0]:span[1]]: the
    period word nearest after them (then nearest before), skipping words inside `ignore`
    and any whose period is implausible for the amount. Else from the amounts. With
    whether the text stated it (False: guessed from the amounts)."""
    start, end = span
    found = []
    for period, pattern in PERIOD_WORDS:
        for word in re.finditer(pattern, text, re.I):
            if not any(a <= word.start() < b for a, b in ignore):
                after = word.start() >= end
                distance = word.start() - end if after else start - word.end()
                found.append((not after, distance, period))
    for *_, period in sorted(found):
        least, most = PLAUSIBLE[period]
        if least <= high < most:
            return period, True
    return _period_from_amount(low, high, country), False


def _next_word_end(text: str, start: int) -> int:
    """Where the word after `start` (and the space before it) ends."""
    following = re.match(r"\s*\w*", text[start:])
    return start + (following.end() if following else 0)


# An annualised salary: (min, max, whether its period was guessed from the amounts).
Pay = tuple[float, float, bool]


def _annual(low: float, high: float, period: str, stated: bool = True) -> Pay | None:
    factor = ANNUAL_FACTOR[period]
    return None if high * factor > MAX_ANNUAL else (low * factor, high * factor, not stated)


# One amount in a salary text: an optional currency, the number, and an optional k ("$80k");
# a percentage ("11.5% super") is matched too, so that it is never taken for pay.
AMOUNT = re.compile(r"(?P<cur>[$£€]|\b(?:A|AU|NZ|S|US|HK)\$)?\s*"
                    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kK]\b)?(?P<pct>\s*%)?")
# What joins the two ends of a range: "$60 - $80k", "$120,000 to $140,000", "between $90k
# and $110k".
RANGE_SEPARATOR = re.compile(r"\s*(?:-|to|and)\s*", re.I)


@dataclass(frozen=True)
class _Pay:
    """A salary expression found in the text: one amount, or a range's two ends."""
    low: float
    high: float
    start: int
    end: int
    marked: bool   # a currency or a k on either end: an amount, not a count of something


def _amount(match: re.Match[str]) -> float:
    return float(match["num"].replace(",", "")) * (1000 if match["k"] else 1)


def _expressions(text: str) -> list[_Pay]:
    """Each amount and range in the text, in order. A k on one end of a range is the whole
    range's ("$60 - $80k" is 60,000 to 80,000, "$80k-90" 80,000 to 90,000)."""
    found = [m for m in AMOUNT.finditer(text) if not m["pct"]]
    expressions, i = [], 0
    while i < len(found):
        a = found[i]
        b = found[i + 1] if i + 1 < len(found) else None
        if b is not None and RANGE_SEPARATOR.fullmatch(text[a.end():b.start()]):
            low, high = _amount(a), _amount(b)
            if a["k"] and not b["k"] and high < 1000:
                high *= 1000
            elif b["k"] and not a["k"] and low < 1000:
                low *= 1000
            # "$100k and 2 weeks leave" is no range.
            if low <= high:
                expressions.append(_Pay(low, high, a.start(), b.end(),
                                        bool(a["cur"] or a["k"] or b["cur"] or b["k"])))
                i += 2
                continue
        expressions.append(_Pay(_amount(a), _amount(a), a.start(), a.end(),
                                bool(a["cur"] or a["k"])))
        i += 1
    return expressions


def _parse_text(text: str, country: str | None) -> Pay | None:
    """Annualised (min, max) from one salary text: its largest amount or range, so a sign-on
    bonus beside the salary isn't taken for its bottom end."""
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    expressions = _expressions(text)
    # Once a currency is marked, an unmarked number is something else: "2 weeks", "38 hours".
    if any(e.marked for e in expressions):
        pay = [e for e in expressions if e.marked]
    else:
        pay = expressions
    pay = [e for e in pay if e.high >= 5]
    if not pay:
        return None
    chosen = max(pay, key=lambda e: e.high)
    # A number that isn't pay takes its next word with it, so "2 weeks leave" and
    # "11.5% super" don't read as a period.
    ignore = [(e.start, _next_word_end(text, e.end)) for e in expressions if e not in pay]
    ignore += [(m.start(), _next_word_end(text, m.end()))
               for m in AMOUNT.finditer(text) if m["pct"]]
    period, stated = _period(text, (chosen.start, chosen.end), chosen.low, chosen.high, ignore,
                             country)
    return _annual(chosen.low, chosen.high, period, stated)


def parse_pay(value: object, country: str | None = None) -> Pay | None:
    """Annualised (min, max, estimated) from Indeed's salary object, free-text salary
    strings, or a list of them (each read on its own, then combined), in the job's country
    when known (it decides whether an amount with no period is monthly). estimated: no
    period was stated, so it was guessed from the amounts' size.

    Returns None when no amount can be found (for example "Competitive"), or when the
    amount is too large to be a real salary.
    """
    if isinstance(value, dict):
        low, high = value.get("min"), value.get("max")
        amounts = [float(a) for a in (low, high) if isinstance(a, (int, float)) and a > 0]
        if not amounts:
            return None
        stated = INDEED_PERIODS.get(str(value.get("type") or "").upper())
        period = stated or _period_from_amount(min(amounts), max(amounts), country)
        return _annual(min(amounts), max(amounts), period, bool(stated))
    if isinstance(value, list):
        parts = [parse_pay(v, country) for v in value if isinstance(v, str)]
        found = [part for part in parts if part]
        return (min(p[0] for p in found), max(p[1] for p in found),
                any(p[2] for p in found)) if found else None
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_text(value, country)


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def _vocab(values: object, mapping: dict[str, str]) -> frozenset[str]:
    """Map scraper labels (a string, list of strings, or list of {label}) to our vocabulary."""
    labels = []
    for value in _as_list(values):
        label = value.get("label") or value.get("name") if isinstance(value, dict) else value
        if isinstance(label, str):
            labels.append(label.strip().casefold())
    return frozenset(mapping[label] for label in labels if label in mapping)


def detect_source(item: dict) -> str:
    if any(key in item for key in ("jobLink", "advertiser", "joblocationInfo")):
        return "seek"
    if any(key in item for key in ("viewJobLink", "jobDescription", "companyDetails",
                                   "formattedLocation")):
        return "indeed"
    host = urlsplit(str(item.get("link") or "")).hostname or ""
    if host == "linkedin.com" or host.endswith(".linkedin.com") or "companyName" in item:
        return "linkedin"
    return host.removeprefix("www.") or "unknown"


def _salary(value: object, country: str | None) -> dict:
    parsed = parse_pay(value, country)
    return ({"salary_min": parsed[0], "salary_max": parsed[1], "salary_estimated": parsed[2]}
            if parsed else {})


def _linkedin(item: dict, source: str, country: str | None) -> Posting:
    return Posting(
        source=source,
        source_id=_text(_get(item, "id", "jobId")),
        url=_text(_get(item, "link", "url", "jobUrl")),
        title=_text(_get(item, "title")) or "Untitled",
        company=_text(_get(item, "companyName", "company")),
        location=_text(_get(item, "location")) if isinstance(item.get("location"), str) else None,
        posted_at=to_date(_get(item, "postedAt", "postedDate", "datePosted")),
        description=html_to_text(_get(item, "descriptionText", "descriptionHtml", "description")),
        **_salary(_get(item, "salaryInfo", "salary"), country),
        job_types=_vocab(_get(item, "employmentType", "jobType"), LINKEDIN_JOB_TYPES),
        work_arrangements=_vocab(_get(item, "workplaceTypes", "workplaceType"),
                                 LINKEDIN_WORK_ARRANGEMENTS)
        or (frozenset({"remote"}) if item.get("workRemoteAllowed") is True else frozenset()),
    )


def _indeed(item: dict, country: str | None) -> Posting:
    job_id = _text(_get(item, "id", "jobKey", "jobkey"))
    site = str(_get(item, "location.countryCode", "country") or "").lower()
    # Indeed's UK site is uk.indeed.com, not the ISO code's gb.
    site = {"gb": "uk"}.get(site, site)
    host = "www.indeed.com" if site in ("", "us") else f"{site}.indeed.com"
    link = _text(_get(item, "viewJobLink", "url", "link"))
    url: str | None
    if job_id:
        url = f"https://{host}/viewjob?jk={job_id}"
    else:
        url = f"https://{host}{link}" if link and link.startswith("/") else link
    location = _get(item, "formattedLocation", "location.formatted", "location.fullAddress")
    if location is None and isinstance(item.get("location"), str):
        location = item["location"]
    return Posting(
        source="indeed",
        source_id=job_id,
        url=url,
        title=_text(_get(item, "title", "displayTitle")) or "Untitled",
        company=_text(_get(item, "companyDetails.name", "company", "companyName",
                           "jobSourceName")),
        location=_text(location),
        posted_at=to_date(_get(item, "pubDate", "datePublished", "postedAt")),
        description=html_to_text(_get(item, "jobDescription", "jobDescriptionHTML",
                                      "description")),
        **_salary(item.get("salary"), country),
        job_types=_vocab(_as_list(item.get("jobTypes")) + _as_list(item.get("attributes")),
                         INDEED_JOB_TYPES),
        work_arrangements=_vocab(item.get("attributes"), INDEED_WORK_ARRANGEMENTS),
    )


# Seek's site for each country it covers under its own name; Singapore and Hong Kong
# listings are on other sites, so no link is made up for them.
SEEK_HOSTS = {"AU": "www.seek.com.au", "NZ": "www.seek.co.nz"}


def seek_url(job_id: str, country: str | None) -> str | None:
    """The link to a Seek job with this ID in this country, if known."""
    host = SEEK_HOSTS.get(country or "")
    return f"https://{host}/job/{job_id}" if host else None


def _seek(item: dict, country: str | None) -> Posting:
    job_id = _text(_get(item, "id", "jobId"))
    # Without a link, save_job makes one from the ID once it knows the country (seek_url).
    url = _text(_get(item, "jobLink", "url", "link"))
    location = _get(item, "joblocationInfo.displayLocation", "joblocationInfo.location")
    if location is None and isinstance(item.get("location"), str):
        location = item["location"]
    content = item.get("content")
    description = _get(item, "content.unEditedContent", "description") if isinstance(
        content, dict) or content is None else content
    return Posting(
        source="seek",
        source_id=job_id,
        url=url,
        title=_text(_get(item, "title")) or "Untitled",
        company=_text(_get(item, "advertiser.name", "companyProfile.name", "companyName")),
        location=_text(location),
        posted_at=to_date(_get(item, "listedAt", "listingDate", "postedAt")),
        description=html_to_text(description),
        **_salary(item.get("salary"), country),
        job_types=_vocab(item.get("workTypes") or item.get("workType"), SEEK_OUTPUT_JOB_TYPES),
        work_arrangements=_vocab(item.get("workArrangements"), SEEK_OUTPUT_WORK_ARRANGEMENTS),
    )


def normalise(item: dict, country: str | None = None) -> Posting:
    """The scraped record as a Posting; `country`, the job's ISO code when known, helps
    read a salary with no period stated."""
    source = detect_source(item)
    if source == "seek":
        return _seek(item, country)
    if source == "indeed":
        return _indeed(item, country)
    return _linkedin(item, source, country)
