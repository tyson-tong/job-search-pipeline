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
from .salary import parse_pay


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
