from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from jobflow import apify, search_options as opt
from jobflow.searches import SearchConfigError, filter_reason, parse_config, plan
from jobflow.sources import normalise, parse_salary

CONFIG = """
[defaults]
posted_within_days = 1
max_results = 40

[[search]]
name = "analyst"
terms = ["data analyst", "bi analyst"]
locations = { AU = ["Sydney", "Melbourne"], NZ = [], US = ["Austin"] }
radius_km = 20
salary_min = 100000
salary_max = 150000
job_types = ["full-time", "contract"]
work_arrangements = ["hybrid", "remote"]
classifications = ["information-communication-technology", "engineering-software"]

[[search]]
name = "counsel"
terms = "legal counsel"
sites = ["seek"]
locations = { AU = [] }
posted_within_days = 5
"""


def searches():
    return parse_config(CONFIG)


def test_parse_applies_defaults_and_normalises():
    analyst, counsel = searches()
    assert analyst.sites == ("linkedin", "indeed", "seek")
    assert analyst.max_results == 40 and counsel.posted_within_days == 5
    assert counsel.terms == ("legal counsel",)
    assert analyst.places() == [("AU", "Sydney"), ("AU", "Melbourne"), ("NZ", None),
                                ("US", "Austin")]


@pytest.mark.parametrize("bad, message", [
    ('job_types = ["fulltime"]', "job_types: unknown value"),
    ('work_arrangements = ["wfh"]', "work_arrangements: unknown value"),
    ('classifications = ["cooking"]', "classifications: unknown value"),
    ('sites = ["glassdoor"]', "sites: unknown value"),
    ('salary_period = "weekly"', "salary_period: unknown value"),
    ("salary_min = 200\nsalary_max = 100", "salary_min is greater"),
    ("keywords = 'x'", "unknown setting"),
])
def test_invalid_options_are_rejected_with_choices(bad, message):
    text = f'[[search]]\nname = "x"\nterms = ["a"]\nlocations = {{ AU = [] }}\n{bad}\n'
    with pytest.raises(SearchConfigError, match=message):
        parse_config(text)


def test_invalid_structure_is_rejected():
    for text, message in [
        ("", "no \\[\\[search\\]\\] entries"),
        ('[[search]]\nterms = ["a"]\nlocations = { AU = [] }', "name is required"),
        ('[[search]]\nname = "x"\nlocations = { AU = [] }', "terms must be"),
        ('[[search]]\nname = "x"\nterms = ["a"]', "locations must map"),
        ('[[search]]\nname = "x"\nterms = ["a"]\nlocations = { XX = [] }', "unknown country"),
        ('[[search]]\nname = "x"\nterms = ["a"]\nlocations = { AU = [] }\n'
         '[[search]]\nname = "x"\nterms = ["b"]\nlocations = { AU = [] }', "duplicate"),
        ("not toml [", "not valid TOML"),
    ]:
        with pytest.raises(SearchConfigError, match=message):
            parse_config(text)


def test_linkedin_one_run_with_a_url_per_term_and_place():
    (run,) = plan(searches(), "analyst", "linkedin")
    urls = run.run_input["urls"]
    assert len(urls) == 2 * 4 and run.run_input["limitPerSource"] == 40
    query = parse_qs(urlsplit(urls[0]).query)
    assert query == {"keywords": ["data analyst"], "location": ["Sydney, Australia"],
                     "f_TPR": ["r86400"], "distance": ["12"]}
    assert parse_qs(urlsplit(urls[2]).query)["location"] == ["New Zealand"]
    assert "classifications (Seek only; use search terms instead)" in run.skipped


def test_indeed_runs_per_term_and_place_with_rounded_date_window():
    runs = plan(searches(), "analyst", "indeed")
    assert len(runs) == 8
    first = runs[0].run_input
    assert first == {"country": "au", "query": "data analyst", "location": "Sydney",
                     "postedWithinDays": "1", "count": 40, "radiusKm": 20}
    assert runs[2].run_input["location"] == "" and runs[2].run_input["country"] == "nz"
    assert runs[3].run_input["country"] == "us"
    counsel = parse_config(CONFIG.replace('sites = ["seek"]', 'sites = ["indeed"]'))
    (run,) = plan(counsel, "counsel")
    assert run.run_input["postedWithinDays"] == "7"


def test_seek_runs_use_native_filters_and_skip_unsupported_countries():
    runs = plan(searches(), "analyst", "seek")
    assert len(runs) == 6  # US is not on Seek
    assert any("country US" in note for note in runs[0].skipped)
    sydney = runs[0].run_input
    assert sydney["country"] == "australia" and sydney["location"] == "Sydney"
    assert sydney["workTypes"] == ["fulltime", "contract"]
    assert sydney["workArrangements"] == ["hybrid", "remote"]
    assert (sydney["salaryType"], sydney["salaryMin"], sydney["salaryMax"]) == \
        ("Annual", 100000, 150000)
    assert sydney["information-communication-technology"] is True
    assert sydney["engineering-software"] is True
    assert "location" not in runs[2].run_input  # NZ nationwide
    assert runs[2].run_input["country"] == "new zealand"


def test_plan_filters_by_search_and_site():
    assert {r.site for r in plan(searches(), "counsel")} == {"seek"}
    assert plan(searches(), "counsel", "linkedin") == []
    with pytest.raises(SearchConfigError, match="No search named"):
        plan(searches(), "missing")


def test_every_seek_classification_key_is_known():
    keys = set(opt.SEEK_CLASSIFICATIONS)
    assert keys == set(opt.SEEK_SUBCLASSIFICATIONS)
    assert "engineering-software" in opt.SEEK_SUBCLASSIFICATIONS["information-communication-technology"]


# --- Local filters ---------------------------------------------------------------------

def _linkedin(**fields):
    return normalise({"id": "1", "link": "https://www.linkedin.com/jobs/view/1",
                      "title": "Data Analyst", "companyName": "Acme", **fields})


def test_unknown_values_always_pass_local_filters():
    analyst = searches()[0]
    assert filter_reason(_linkedin(), analyst, "linkedin") is None


def test_local_salary_filter():
    analyst = searches()[0]
    low = _linkedin(salaryInfo=["$60,000.00/yr", "$70,000.00/yr"])
    assert "outside 100,000-150,000/yr" in filter_reason(low, analyst, "linkedin")
    overlapping = _linkedin(salaryInfo=["$140,000.00/yr", "$170,000.00/yr"])
    assert filter_reason(overlapping, analyst, "linkedin") is None
    hourly = normalise({"viewJobLink": "/viewjob?jk=1", "title": "Data Analyst",
                        "salary": {"min": 60, "max": 70, "type": "HOUR"}})
    assert filter_reason(hourly, analyst, "indeed") is None  # ~118k-138k a year


def test_local_job_type_and_arrangement_filters():
    analyst = searches()[0]
    part_time = _linkedin(employmentType="Part-time")
    assert "job type part-time" in filter_reason(part_time, analyst, "linkedin")
    onsite = normalise({"viewJobLink": "/viewjob?jk=1", "title": "Data Analyst",
                        "jobTypes": ["Full-time"],
                        "attributes": [{"label": "In-person"}]})
    assert onsite.job_types == {"full-time"} and onsite.work_arrangements == {"on-site"}
    assert "work arrangement on-site" in filter_reason(onsite, analyst, "indeed")
    assert filter_reason(_linkedin(workplaceTypes=["Remote"]), analyst, "linkedin") is None


def test_seek_is_not_filtered_locally():
    analyst = searches()[0]
    seek = normalise({"jobLink": "https://www.seek.com.au/job/1", "title": "Data Analyst",
                      "salary": "$50k", "workTypes": "Part time"})
    assert filter_reason(seek, analyst, "seek") is None


def test_parse_salary_formats():
    assert parse_salary("$90k - $120k + super + bonus") == (90000, 120000)
    assert parse_salary(["$17.00", "$19.00"]) == (17 * 1976, 19 * 1976)
    assert parse_salary({"min": 28, "max": 32, "type": "HOUR"}) == (28 * 1976, 32 * 1976)
    assert parse_salary("$120,000 - $140,000 + 11.5% super") == (120000, 140000)
    assert parse_salary("$55 - $65 p.h.") == (55 * 1976, 65 * 1976)
    assert parse_salary("$8,000 per month") == (96000, 96000)
    assert parse_salary("Competitive") is None and parse_salary(None) is None


def test_seek_output_job_type_and_arrangement():
    p = normalise({"jobLink": "https://www.seek.com.au/job/1", "title": "x",
                   "workTypes": "Contract/Temp", "workArrangements": "Hybrid"})
    assert p.job_types == {"contract"} and p.work_arrangements == {"hybrid"}


# --- Running on Apify -------------------------------------------------------------------

def _run(status="SUCCEEDED", message=None, cost=None):
    return SimpleNamespace(id="r1", status=status, status_message=message,
                           default_dataset_id="d1", started_at=None, finished_at=None,
                           usage_total_usd=cost)


class FakeClient:
    def __init__(self, result, items=()):
        self.result, self.items, self.calls = result, list(items), []

    def actor(self, actor_id):
        client = self

        class Actor:
            def call(self, **kwargs):
                client.calls.append((actor_id, kwargs))
                if isinstance(client.result, Exception):
                    raise client.result
                return client.result
        return Actor()

    def dataset(self, dataset_id):
        items = self.items

        class Dataset:
            def iterate_items(self, clean):
                return iter(items)
        return Dataset()


def test_run_search_imports_with_filter_provenance_and_cost_cap(monkeypatch):
    import jobflow.ingest as ingest
    saved = []
    monkeypatch.setattr(ingest, "save_run", lambda *a, **k: saved.append((a, k)) or 1)
    analyst = parse_config(CONFIG.replace("max_results = 40", "max_results = 40\nmax_cost_usd = 0.5"))
    planned = plan(analyst, "analyst", "indeed")[0]
    client = FakeClient(_run(cost=0.02), [{"viewJobLink": "/viewjob?jk=1", "title": "x"}])
    apify.run_search(client, planned, timedelta(minutes=5))
    (actor, kwargs), = client.calls
    assert actor == "curious_coder/indeed-scraper"
    assert kwargs["run_input"] == planned.run_input and str(kwargs["max_total_charge_usd"]) == "0.5"
    (args, save_kwargs), = saved
    assert args[0] == "indeed:curious_coder/indeed-scraper"
    assert save_kwargs["search_name"] == "analyst" and save_kwargs["cost_usd"] == 0.02
    part_time = normalise({"viewJobLink": "/viewjob?jk=2", "title": "x", "jobTypes": ["Part-time"]})
    assert "part-time" in save_kwargs["job_filter"](part_time)


@pytest.mark.parametrize("result, message", [
    (_run("FAILED", message="bad input"), "ended with FAILED: bad input"),
    (_run("RUNNING"), "still running after"),
    (None, "no run"),
    (RuntimeError("network down"), "network down"),
])
def test_run_search_records_failures(monkeypatch, result, message):
    import jobflow.ingest as ingest
    recorded = []
    monkeypatch.setattr(ingest, "record_failed_run", lambda *a: recorded.append(a))
    planned = plan(searches(), "counsel")[0]
    with pytest.raises(RuntimeError, match=message):
        apify.run_search(FakeClient(result), planned, timedelta(minutes=5))
    assert recorded[0][0] == "seek:websift/seek-job-scraper"
    assert recorded[0][-2:] == ("counsel", planned.run_input)


def test_one_failed_run_does_not_stop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setenv("JOBFLOW_SEARCHES", CONFIG)
    seen = []

    def fake_run(client, planned, max_wait):
        seen.append(planned.site)
        if planned.site == "linkedin":
            raise RuntimeError("boom")

    monkeypatch.setattr(apify, "run_search", fake_run)
    with pytest.raises(SystemExit, match="1 of 16 Apify runs failed"):
        apify.ingest_searches()
    assert seen.count("linkedin") == 1 and seen.count("indeed") == 8 and seen.count("seek") == 7
    assert "FAILED — boom" in capsys.readouterr().err


def test_missing_configuration_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBFLOW_SEARCHES", raising=False)
    monkeypatch.setenv("JOBFLOW_PROFILE", str(tmp_path / "profile.toml"))
    (tmp_path / "profile.toml").write_text("[paths]\nsearches_file = './none.toml'\n")
    with pytest.raises(SearchConfigError, match="No searches config"):
        apify.ingest_searches()
    monkeypatch.setenv("JOBFLOW_SEARCHES", CONFIG)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
        apify.ingest_searches()
