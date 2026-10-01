from datetime import date

from jobflow.ingest.dedupe import canonical_url, is_same_ad, same_place
from jobflow.ingest.sources import normalise, to_date

DESCRIPTION = ("We are hiring a Data Analyst to build dashboards, write SQL, and automate "
               "reporting in Python for our finance and operations teams.")

LINKEDIN = {"id": "4012345678",
            "link": "https://au.linkedin.com/jobs/view/data-analyst-4012345678?trk=abc&refId=x",
            "title": "Data Analyst", "companyName": "Acme Pty Ltd", "location": "Sydney, NSW",
            "postedAt": "2026-09-20", "descriptionText": DESCRIPTION}

INDEED = {"id": "a1b2c3d4e5f6", "viewJobLink": "/viewjob?jk=a1b2c3d4e5f6&from=serp&tk=zz",
          "title": "Data Analyst", "companyDetails": {"name": "Acme"},
          "formattedLocation": "Sydney NSW 2000", "location": {"countryCode": "AU"},
          "pubDate": 1789862400000, "jobDescriptionHTML": f"<p>{DESCRIPTION}</p>"}

SEEK = {"id": "81234567", "jobLink": "https://www.seek.com.au/job/81234567?type=standard",
        "title": "Data Analyst", "advertiser": {"name": "ACME Limited"},
        "joblocationInfo": {"displayLocation": "Sydney NSW"},
        "listedAt": "2026-09-21T01:30:00Z", "workTypes": "Contract/Temp",
        "workArrangements": "Hybrid",
        "content": {"unEditedContent": f"<p>{DESCRIPTION}</p><ul><li>Hybrid</li></ul>"}}


def test_canonical_url_drops_tracking_so_repeat_links_match():
    assert canonical_url("HTTPS://Example.com/jobs/1/?utm_source=x&b=2&a=1&trk=y") == \
        "https://example.com/jobs/1?a=1&b=2"
    assert canonical_url("not a url") is None
    # A scraped script link must never become a clickable link in the tracker or briefing.
    assert canonical_url("javascript://example.com/%0Aalert(1)") is None


def test_each_scraper_format_is_normalised():
    linkedin, indeed, seek = (normalise(i) for i in (LINKEDIN, INDEED, SEEK))
    assert (linkedin.source, linkedin.company, linkedin.location, linkedin.posted_at) == \
        ("linkedin", "Acme Pty Ltd", "Sydney, NSW", date(2026, 9, 20))
    assert (indeed.source, indeed.company, indeed.location, indeed.posted_at) == \
        ("indeed", "Acme", "Sydney NSW 2000", date(2026, 9, 20))
    assert indeed.url == "https://au.indeed.com/viewjob?jk=a1b2c3d4e5f6"
    assert (seek.source, seek.company, seek.location, seek.posted_at) == \
        ("seek", "ACME Limited", "Sydney NSW", date(2026, 9, 21))
    assert seek.job_types == {"contract"} and seek.work_arrangements == {"hybrid"}
    assert linkedin.description == indeed.description == DESCRIPTION


def test_partial_records_do_not_crash():
    for item in ({"jobLink": None}, {"viewJobLink": "/viewjob?jk=zz"}, {}):
        assert normalise(item).title == "Untitled"


def test_to_date_handles_iso_and_unix_seconds_or_milliseconds():
    assert to_date("2026-09-01T10:00:00Z") == date(2026, 9, 1)
    assert to_date(1789862400000) == to_date(1789862400) == date(2026, 9, 20)
    assert to_date("3 days ago") is None
    assert to_date("20260920") == date(2026, 9, 20)   # a compact date, not a 1970 timestamp


def test_same_role_in_different_cities_is_not_merged():
    # The fuzzy step ignores location formatting, but not a different city.
    assert same_place("Sydney NSW 2000", "Greater Sydney Area, New South Wales, Australia")
    assert not same_place("Sydney, NSW", "Melbourne VIC")
    # Unknown or country-only locations never block a match.
    assert same_place(None, "Perth WA") and same_place("Singapore, Singapore", "Sydney NSW")


LONG_AD = " ".join(f"duty{i}" for i in range(60))


def test_identical_ad_under_the_same_title_merges_whoever_posts_it():
    # Seen live: LinkedIn and Seek spelled the employer and district differently, so the
    # employer-and-city step missed one ad posted on both.
    assert is_same_ad("VP-Director, Portfolio Management (Equity)", LONG_AD,
                      "VP Director Portfolio Management Equity", LONG_AD + " apply")
    # Susquehanna reused one ad for two different internships: never merged.
    assert not is_same_ad("Quantitative Research Internship", LONG_AD,
                          "Quantitative Systematic Trading Internship", LONG_AD)
    # A short boilerplate ad is not enough, nor is an ad that is only mostly the same.
    assert not is_same_ad("Analyst", "Apply on our website today", "Analyst",
                          "Apply on our website today")
    edited = " ".join(f"duty{i}" for i in range(54)) + " " + " ".join(f"other{i}" for i in range(6))
    assert not is_same_ad("Analyst", LONG_AD, "Analyst", edited)
    # CPP Investments posted one ad for Sydney and Hong Kong: two jobs, one per country.
    assert not is_same_ad("Analyst", LONG_AD, "Analyst", LONG_AD, "AU", "HK")
    assert is_same_ad("Analyst", LONG_AD, "Analyst", LONG_AD, "HK", None)
