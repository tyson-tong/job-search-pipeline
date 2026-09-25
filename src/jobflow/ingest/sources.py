"""Normalise job records from different Apify scrapers into one shape.

Supported scrapers (detected from each record's fields):
- linkedin: curious_coder/linkedin-jobs-scraper
- indeed:   curious_coder/indeed-scraper
- seek:     websift/seek-job-scraper
Anything else is read with LinkedIn-style field names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urlsplit

from .options import (INDEED_JOB_TYPES, INDEED_WORK_ARRANGEMENTS, LINKEDIN_JOB_TYPES,
                             LINKEDIN_WORK_ARRANGEMENTS, SEEK_OUTPUT_JOB_TYPES,
                             SEEK_OUTPUT_WORK_ARRANGEMENTS)

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
    text = re.sub(r"<(br|/p|/li|/h\d|/div)\b[^>]*>", "\n", str(value or ""), flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def to_date(value: object) -> date | None:
    """ISO strings, ISO datetimes, or Unix timestamps in seconds or milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number,
                                      tz=timezone.utc).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _period(text: str, amount: float) -> str:
    lowered = text.casefold()
    for period, pattern in PERIOD_WORDS:
        if re.search(pattern, lowered):
            return period
    # No period stated: infer it from the size of the amount.
    return "hourly" if amount < 300 else "daily" if amount < 3000 else \
        "monthly" if amount < 20000 else "annual"


def parse_salary(value: object) -> tuple[float, float] | None:
    """Annualised (min, max) from Indeed's salary object or free-text salary strings.

    Returns None when no amount can be found (for example "Competitive").
    """
    if isinstance(value, dict):
        low, high = value.get("min"), value.get("max")
        amounts = [float(a) for a in (low, high) if isinstance(a, (int, float)) and a > 0]
        if not amounts:
            return None
        period = INDEED_PERIODS.get(str(value.get("type") or "").upper()) or \
            _period("", max(amounts))
        factor = ANNUAL_FACTOR[period]
        return min(amounts) * factor, max(amounts) * factor
    if isinstance(value, list):
        value = " - ".join(str(v) for v in value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("\u2013", "-").replace("\u2014", "-")
    matches = list(re.finditer(r"(?P<cur>[$£€]|\b(?:A|AU|NZ|S|US|HK)\$)?\s*"
                               r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kK]\b)?(?P<pct>\s*%)?", text))
    amounts = []
    has_currency = any(m["cur"] for m in matches)
    for m in matches:
        if m["pct"] or (has_currency and not m["cur"] and not m["k"]):
            continue
        amount = float(m["num"].replace(",", "")) * (1000 if m["k"] else 1)
        if amount >= 5:
            amounts.append(amount)
    if not amounts:
        return None
    factor = ANNUAL_FACTOR[_period(text, max(amounts))]
    return min(amounts) * factor, max(amounts) * factor


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


def _salary(value: object) -> dict:
    parsed = parse_salary(value)
    return {"salary_min": parsed[0], "salary_max": parsed[1]} if parsed else {}


def _linkedin(item: dict, source: str) -> Posting:
    return Posting(
        source=source,
        source_id=_text(_get(item, "id", "jobId")),
        url=_text(_get(item, "link", "url", "jobUrl")),
        title=_text(_get(item, "title")) or "Untitled",
        company=_text(_get(item, "companyName", "company")),
        location=_text(_get(item, "location")) if isinstance(item.get("location"), str) else None,
        posted_at=to_date(_get(item, "postedAt", "postedDate", "datePosted")),
        description=html_to_text(_get(item, "descriptionText", "descriptionHtml", "description")),
        **_salary(_get(item, "salaryInfo", "salary")),
        job_types=_vocab(_get(item, "employmentType", "jobType"), LINKEDIN_JOB_TYPES),
        work_arrangements=_vocab(_get(item, "workplaceTypes", "workplaceType"),
                                 LINKEDIN_WORK_ARRANGEMENTS)
        or (frozenset({"remote"}) if item.get("workRemoteAllowed") is True else frozenset()),
    )


def _indeed(item: dict) -> Posting:
    job_id = _text(_get(item, "id", "jobKey", "jobkey"))
    country = str(_get(item, "location.countryCode", "country") or "").lower()
    host = "www.indeed.com" if country in ("", "us") else f"{country}.indeed.com"
    link = _text(_get(item, "viewJobLink", "url", "link"))
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
        **_salary(item.get("salary")),
        job_types=_vocab(_as_list(item.get("jobTypes")) + _as_list(item.get("attributes")),
                         INDEED_JOB_TYPES),
        work_arrangements=_vocab(item.get("attributes"), INDEED_WORK_ARRANGEMENTS),
    )


def _seek(item: dict) -> Posting:
    job_id = _text(_get(item, "id", "jobId"))
    url = _text(_get(item, "jobLink", "url", "link"))
    if not url and job_id:
        url = f"https://www.seek.com.au/job/{job_id}"
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
        **_salary(item.get("salary")),
        job_types=_vocab(item.get("workTypes") or item.get("workType"), SEEK_OUTPUT_JOB_TYPES),
        work_arrangements=_vocab(item.get("workArrangements"), SEEK_OUTPUT_WORK_ARRANGEMENTS),
    )


def normalise(item: dict) -> Posting:
    source = detect_source(item)
    if source == "seek":
        return _seek(item)
    if source == "indeed":
        return _indeed(item)
    return _linkedin(item, source)
