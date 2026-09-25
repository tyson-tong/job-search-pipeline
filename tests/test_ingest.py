from datetime import date

from jobflow.ingest.sources import normalise, to_date
from jobflow.ingest.store import (canonical_url, description_similarity, fingerprint, identity,
                                  match_key, same_place)

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


def test_ids_from_different_sites_cannot_collide():
    keys = {identity(normalise({**item, "id": "123"}))[0] for item in (LINKEDIN, INDEED, SEEK)}
    assert keys == {"linkedin:123", "indeed:123", "seek:123"}


def test_same_job_on_three_sites_is_recognised():
    postings = [normalise(i) for i in (LINKEDIN, INDEED, SEEK)]
    assert {match_key(p) for p in postings} == {"acme|data analyst"}
    for p in postings[1:]:
        assert description_similarity(postings[0].description, p.description) >= 0.5
    # Exact fingerprints differ (locations are formatted differently), which is why
    # cross-site matching needs the fuzzy step.
    assert len({fingerprint(p) for p in postings}) == 3


def test_different_jobs_are_not_merged():
    other = normalise({**LINKEDIN, "descriptionText": "Lead our retail store team, manage "
                       "rosters, customer service, and stock ordering."})
    assert description_similarity(normalise(LINKEDIN).description, other.description) < 0.5
    assert match_key(normalise({**SEEK, "title": "Senior Data Analyst"})) != match_key(normalise(SEEK))
    assert match_key(normalise({**SEEK, "advertiser": {}})) is None


def test_same_role_in_different_cities_is_not_merged():
    # The fuzzy step ignores location formatting, but not a different city.
    assert same_place("Sydney NSW 2000", "Greater Sydney Area, New South Wales, Australia")
    assert not same_place("Sydney, NSW", "Melbourne VIC")
    # Unknown or country-only locations never block a match.
    assert same_place(None, "Perth WA") and same_place("Singapore, Singapore", "Sydney NSW")
