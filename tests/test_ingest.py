from datetime import date, datetime, timezone

from jobflow.ingest import (canonical_url, company_key, description_similarity, fingerprint,
                            identity, match_key, timestamp)
from jobflow.sources import detect_source, html_to_text, normalise, to_date

DESCRIPTION = ("We are hiring a Data Analyst to build dashboards, write SQL, and automate "
               "reporting in Python for our finance and operations teams.")

LINKEDIN = {"id": "4012345678",
            "link": "https://au.linkedin.com/jobs/view/data-analyst-4012345678?trk=abc&refId=x",
            "title": "Data Analyst", "companyName": "Acme Pty Ltd", "location": "Sydney, NSW",
            "postedAt": "2026-09-20", "descriptionText": DESCRIPTION,
            "inputUrl": "https://www.linkedin.com/jobs/search?keywords=analyst"}

INDEED = {"id": "a1b2c3d4e5f6", "viewJobLink": "/viewjob?jk=a1b2c3d4e5f6&from=serp&tk=zz",
          "title": "Data Analyst", "companyDetails": {"name": "Acme"},
          "formattedLocation": "Sydney NSW 2000", "location": {"countryCode": "AU"},
          "pubDate": 1789862400000, "jobDescriptionHTML": f"<p>{DESCRIPTION}</p>",
          "salary": {"min": 90000, "max": 110000}}

SEEK = {"id": "81234567", "jobLink": "https://www.seek.com.au/job/81234567?type=standard",
        "title": "Data Analyst", "advertiser": {"name": "ACME Limited"},
        "joblocationInfo": {"displayLocation": "Sydney NSW"},
        "listedAt": "2026-09-21T01:30:00Z",
        "content": {"unEditedContent": f"<p>{DESCRIPTION}</p><ul><li>Hybrid</li></ul>"}}


def test_canonical_url_strips_tracking_and_normalises():
    assert canonical_url("HTTPS://Example.com/jobs/1/?utm_source=x&b=2&a=1&trk=y") == \
        "https://example.com/jobs/1?a=1&b=2"
    assert canonical_url("not a url") is None
    assert canonical_url(None) is None


def test_detect_source():
    assert [detect_source(i) for i in (LINKEDIN, INDEED, SEEK)] == ["linkedin", "indeed", "seek"]
    assert detect_source({"link": "https://jobs.example.org/1", "title": "x"}) == "jobs.example.org"


def test_normalise_linkedin():
    p = normalise(LINKEDIN)
    assert (p.source, p.source_id, p.title, p.company, p.location, p.posted_at) == \
        ("linkedin", "4012345678", "Data Analyst", "Acme Pty Ltd", "Sydney, NSW", date(2026, 9, 20))
    assert p.description == DESCRIPTION


def test_normalise_indeed():
    p = normalise(INDEED)
    assert (p.source, p.source_id, p.company, p.location) == \
        ("indeed", "a1b2c3d4e5f6", "Acme", "Sydney NSW 2000")
    assert p.url == "https://au.indeed.com/viewjob?jk=a1b2c3d4e5f6"
    assert p.posted_at == date(2026, 9, 20)
    assert p.description == DESCRIPTION


def test_normalise_seek():
    p = normalise(SEEK)
    assert (p.source, p.source_id, p.company, p.location, p.posted_at) == \
        ("seek", "81234567", "ACME Limited", "Sydney NSW", date(2026, 9, 21))
    assert p.description == DESCRIPTION + " Hybrid"
    assert canonical_url(p.url) == "https://www.seek.com.au/job/81234567"


def test_normalise_tolerates_missing_fields():
    for item in ({"jobLink": None}, {"viewJobLink": "/viewjob?jk=zz"}, {}):
        p = normalise(item)
        assert p.title == "Untitled" and p.description == ""
    assert normalise({"viewJobLink": "/viewjob?jk=zz"}).url == "https://www.indeed.com/viewjob?jk=zz"


def test_to_date_and_html():
    assert to_date("2026-09-01T10:00:00Z") == date(2026, 9, 1)
    assert to_date(1789862400000) == to_date(1789862400) == to_date("1789862400000") == date(2026, 9, 20)
    assert to_date("3 days ago") is None and to_date(None) is None
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><p>Next</p>") == "Hello world Next"
    assert timestamp("2026-09-01T10:00:00.123Z") == \
        datetime(2026, 9, 1, 10, 0, 0, 123000, tzinfo=timezone.utc)


def test_identity_is_namespaced_per_site():
    keys = {identity(normalise(i))[0] for i in (LINKEDIN, INDEED, SEEK)}
    assert keys == {"linkedin:4012345678", "indeed:a1b2c3d4e5f6", "seek:81234567"}
    no_id = normalise({k: v for k, v in LINKEDIN.items() if k != "id"})
    assert identity(no_id)[0].startswith("url:")


def test_same_job_on_three_sites_shares_match_key_and_similar_description():
    postings = [normalise(i) for i in (LINKEDIN, INDEED, SEEK)]
    assert {match_key(p) for p in postings} == {"acme|data analyst"}
    for p in postings[1:]:
        assert description_similarity(postings[0].description, p.description) >= 0.5
    # Exact fingerprints differ because each site formats location differently.
    assert len({fingerprint(p) for p in postings}) == 3


def test_different_jobs_are_not_merged():
    other = normalise({**LINKEDIN, "descriptionText": "Lead our retail store team, manage "
                       "rosters, customer service, and stock ordering."})
    assert description_similarity(normalise(LINKEDIN).description, other.description) < 0.5
    assert match_key(normalise({**SEEK, "title": "Senior Data Analyst"})) != match_key(normalise(SEEK))
    assert match_key(normalise({**SEEK, "advertiser": {}})) is None


def test_company_key_ignores_legal_suffixes():
    assert company_key("Acme Pty. Ltd.") == company_key("ACME Limited") == company_key("The Acme Group") == "acme"
