"""Search option vocabularies, and how each job site's Apify actor expresses them.

Search configs (config/searches.toml) use the common vocabulary; ingest/runs.py translates it
per site. Values come from each actor's published input schema:
- LinkedIn: curious_coder/linkedin-jobs-scraper
- Indeed:   curious_coder/indeed-scraper
- Seek:     websift/seek-job-scraper
Re-check them against the actors' input schemas if a run rejects its input.
"""
from __future__ import annotations

import json
from importlib import resources

SITES = ("linkedin", "indeed", "seek")

ACTORS = {
    "linkedin": "curious_coder/linkedin-jobs-scraper",
    "indeed": "curious_coder/indeed-scraper",
    "seek": "websift/seek-job-scraper",
}

# --- Common vocabulary ---------------------------------------------------------------

# posted_within_days: 24 hours, 1 week, 2 weeks, 1 month.
POSTED_WITHIN_DAYS = (1, 7, 14, 30)
JOB_TYPES = ("full-time", "part-time", "contract", "casual")
WORK_ARRANGEMENTS = ("on-site", "hybrid", "remote")
SALARY_PERIODS = ("annual", "monthly", "hourly")

# --- Which filters each site applies itself --------------------------------------------
# Filters a site can't apply are applied locally when jobs are imported, and only
# exclude jobs whose value is known: a job with no salary, job type, or work arrangement
# listed is always kept. "search" means the site filters before charging for results.
# Classification is only available on Seek; on other sites, use search terms instead.

FILTER_SUPPORT = {
    "linkedin": {"country": "search", "city": "search", "posted_within_days": "search",
                 "salary": "local", "job_types": "local", "work_arrangements": "local",
                 "seek_classifications": "unsupported"},
    "indeed": {"country": "search", "city": "search", "posted_within_days": "search",
               "salary": "local", "job_types": "local", "work_arrangements": "local",
               "seek_classifications": "unsupported"},
    "seek": {"country": "search", "city": "search", "posted_within_days": "search",
             "salary": "local", "job_types": "search", "work_arrangements": "search",
             "seek_classifications": "search"},
}

# --- Countries and cities --------------------------------------------------------------

# The countries a search can use and their major cities; a search names cities from here,
# or [] for the whole country. Seek covers only AU, NZ, SG, and HK (SEEK_COUNTRIES). To
# search somewhere else, add it here and to the site tables below.
CITIES = {
    "AU": ("Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra"),
    "NZ": ("Auckland", "Christchurch", "Wellington"),
    "SG": ("Singapore",),
    "HK": ("Hong Kong",),
    "GB": ("London", "Manchester", "Birmingham", "Edinburgh", "Glasgow", "Leeds", "Bristol"),
    "US": ("New York", "San Francisco", "Los Angeles", "Chicago", "Boston", "Seattle",
           "Houston", "San Jose", "Dallas", "Atlanta", "Washington D.C.", "Minneapolis",
           "Philadelphia", "Charlotte", "Denver", "Miami", "San Antonio", "Omaha"),
}
# Other spellings of a country code accepted in searches.
COUNTRY_ALIASES = {"UK": "GB"}

# Each country's currency, and roughly how many US dollars one unit is worth. Salaries are
# stored in the posting's currency; the tracker converts them with these fixed rates to
# compare and sort in one currency. Approximate: update them if rates move a lot.
CURRENCIES = {"AU": "AUD", "NZ": "NZD", "SG": "SGD", "HK": "HKD", "GB": "GBP", "US": "USD"}
USD_PER_UNIT = {"AUD": 0.66, "NZD": 0.60, "SGD": 0.77, "HKD": 0.128, "GBP": 1.30, "USD": 1.0}

# --- LinkedIn ------------------------------------------------------------------------

# LinkedIn takes the location as free text, "City, Country"; these are its country names.
LINKEDIN_COUNTRIES = {"AU": "Australia", "NZ": "New Zealand", "SG": "Singapore",
                      "HK": "Hong Kong", "GB": "United Kingdom", "US": "United States"}
# Cities LinkedIn reads as a smaller place of the same name unless the state is given
# ("Charlotte, United States" searches Charlotte, Arkansas).
LINKEDIN_CITY_NAMES = {"Charlotte": "Charlotte, North Carolina", "Miami": "Miami, Florida",
                       "Denver": "Denver, Colorado"}

# Search URL f_TPR values (seconds) for LinkedIn's own date filter: past 24 hours,
# past week, past month. LinkedIn has no 2-week option.
LINKEDIN_POSTED_WITHIN = {1: "r86400", 7: "r604800", 30: "r2592000"}
LINKEDIN_POSTED_ANY_TIME = None  # no f_TPR parameter

# How LinkedIn output reports job types and workplace types (lower-cased).
LINKEDIN_JOB_TYPES = {"full-time": "full-time", "part-time": "part-time",
                      "contract": "contract", "temporary": "casual"}
LINKEDIN_WORK_ARRANGEMENTS = {"on-site": "on-site", "hybrid": "hybrid", "remote": "remote"}

# --- Indeed --------------------------------------------------------------------------

# ISO country code -> Indeed "country" input value.
INDEED_COUNTRIES = {"AU": "au", "NZ": "nz", "SG": "sg", "HK": "hk", "GB": "uk", "US": "us"}

# postedWithinDays input (the actor also accepts 3). Indeed has no 1-month option.
INDEED_POSTED_WITHIN = {1: "1", 7: "7", 14: "14"}
INDEED_POSTED_ANY_TIME = ""

# How Indeed output reports job types (jobTypes / attributes labels, lower-cased).
INDEED_JOB_TYPES = {"full-time": "full-time", "part-time": "part-time",
                    "contract": "contract", "casual": "casual", "temporary": "casual"}
# Indeed attribute labels that indicate a work arrangement (lower-cased).
INDEED_WORK_ARRANGEMENTS = {"remote": "remote", "work from home": "remote",
                            "hybrid work": "hybrid", "hybrid remote": "hybrid",
                            "hybrid": "hybrid", "in-person": "on-site", "office": "on-site",
                            "on-site": "on-site"}

# --- Seek ----------------------------------------------------------------------------

# ISO country code -> Seek "country" input value. Seek has no UK or US site.
SEEK_COUNTRIES = {"AU": "australia", "NZ": "new zealand", "SG": "singapore", "HK": "hongkong"}

SEEK_MAX_RESULTS = 550
# dateRange input takes any number of days (up to 365), so every window is exact.
SEEK_POSTED_WITHIN = {1: 1, 7: 7, 14: 14, 30: 30}
SEEK_POSTED_ANY_TIME = 365

SEEK_JOB_TYPES = {"full-time": "fulltime", "part-time": "parttime", "contract": "contract",
                  "casual": "casual"}
SEEK_WORK_ARRANGEMENTS = {"on-site": "on-site", "hybrid": "hybrid", "remote": "remote"}

# How Seek output reports work types and arrangements (lower-cased).
SEEK_OUTPUT_JOB_TYPES = {"full time": "full-time", "part time": "part-time",
                         "contract/temp": "contract", "casual/vacation": "casual"}
SEEK_OUTPUT_WORK_ARRANGEMENTS = {"on-site": "on-site", "hybrid": "hybrid", "remote": "remote"}

# Seek's classifications, from seek_classifications.json: top-level key -> name shown on
# Seek (selecting one includes all its subclassifications), and top-level key ->
# {subclassification key: name} (any of these keys can also be selected on its own).
_SEEK = json.loads((resources.files("jobflow.ingest") / "seek_classifications.json").read_text())
SEEK_CLASSIFICATIONS: dict[str, str] = _SEEK["classifications"]
SEEK_SUBCLASSIFICATIONS: dict[str, dict[str, str]] = _SEEK["subclassifications"]
