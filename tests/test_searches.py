from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from jobflow.ingest import apify
from jobflow.ingest.searches import (SearchConfigError, country_from_location, filter_reason,
                                     parse_config, plan, search_url_country)
from jobflow.ingest.sources import normalise, parse_salary

CONFIG = """
[defaults]
posted_within_days = 1
max_results = 40

[[search]]
name = "analyst"
terms = ["data analyst", "bi analyst"]
locations = { AU = ["Sydney", "Melbourne"], NZ = [], SG = ["singapore"], UK = ["London"] }
radius_km = 20
salary_min = 100000
salary_max = 150000
job_types = ["full-time", "contract"]
work_arrangements = ["hybrid", "remote"]
seek_classifications = ["information-communication-technology", "engineering-software"]

[[search]]
name = "counsel"
terms = "legal counsel"
sites = ["seek"]
locations = { AU = [] }
posted_within_days = 30
"""


def searches():
    return parse_config(CONFIG)


def test_defaults_are_inherited_and_overridden():
    analyst, counsel = searches()
    assert analyst.sites == ("linkedin", "indeed", "seek")
    assert analyst.max_results == 40 and counsel.posted_within_days == 30
    # City names are matched case-insensitively and stored as listed.
    assert analyst.places() == [("AU", "Sydney"), ("AU", "Melbourne"), ("NZ", None),
                                ("SG", "Singapore"), ("GB", "London")]


@pytest.mark.parametrize("bad, message", [
    ('job_types = ["fulltime"]', "unknown value.*choose from.*full-time"),
    ("salary_min = 200\nsalary_max = 100", "salary_min is greater"),
    ("keywords = 'x'", "unknown setting"),  # a typo'd setting must not be silently ignored
    ('locations = ["Sydney"]', "locations must map"),
    # A misspelt city or an unsupported country would otherwise search for nothing.
    ('locations = { AU = ["Syndey"] }', "unknown AU city.*choose from Sydney"),
    ('locations = { FR = [] }', "unknown country 'FR'; choose from AU, NZ, SG, HK, GB, US"),
    ("sites = []", "at least one"),  # an empty list would silently run nothing
    ("posted_within_days = 3", "must be one of \\[1, 7, 14, 30\\]"),
    # A quoted number crashed instead of stopping update-searches with a reason.
    ('salary_min = "100k"', "salary_min must be a number"),
    # 0 was sent to Apify as no cost cap at all.
    ("max_cost_usd = 0", "max_cost_usd must be more than 0"),
    ("max_cost_usd = nan", "max_cost_usd must be a number"),
    ("max_results = 2.5", "max_results must be a whole number"),
])
def test_invalid_config_is_rejected(bad, message):
    base = '[[search]]\nname = "x"\nterms = ["a"]\n'
    text = base + (bad if bad.startswith("locations") else f"locations = {{ AU = [] }}\n{bad}")
    with pytest.raises(SearchConfigError, match=message):
        parse_config(text + "\n")


SEARCH = 'name = "x"\nterms = ["a"]\nlocations = { AU = [] }\n'


@pytest.mark.parametrize("text, message", [
    # Each crashed with a traceback (or a 500 in the Searches tab) instead of saying why.
    ("[search]\n" + SEARCH, "double brackets"),
    ("defaults = 3\n[[search]]\n" + SEARCH, "defaults must be"),
    ("[default]\nmax_results = 5\n[[search]]\n" + SEARCH, "unknown section"),
    ('[[search]]\nname = "x"\nterms = 5\nlocations = { AU = [] }\n', "terms must be"),
    ("[[search]]\n" + SEARCH + 'seek_classifications = [["a"]]\n', "must be a list"),
    ("[[search]]\n" + SEARCH + "posted_within_days = true\n", "posted_within_days must be"),
])
def test_a_malformed_file_is_rejected_with_a_reason(text, message):
    with pytest.raises(SearchConfigError, match=message):
        parse_config(text)


# --- Translation to each actor's input ---------------------------------------------------

def test_linkedin_is_one_run_with_a_url_per_term_and_place():
    (run,) = plan(searches(), "analyst", "linkedin")
    urls = run.run_input["urls"]
    assert len(urls) == 2 * 5 and run.run_input["limitPerSource"] == 40
    assert parse_qs(urlsplit(urls[0]).query) == {
        "keywords": ["data analyst"], "location": ["Sydney, Australia"],
        "f_TPR": ["r86400"], "distance": ["12"]}
    assert parse_qs(urlsplit(urls[2]).query)["location"] == ["New Zealand"]



def test_indeed_runs_per_term_and_place():
    runs = plan(searches(), "analyst", "indeed")
    assert len(runs) == 10
    assert runs[0].run_input == {"country": "au", "query": "data analyst", "location": "Sydney",
                                 "postedWithinDays": "1", "count": 40, "radiusKm": 20}
    assert runs[2].run_input["location"] == "" and runs[2].run_input["country"] == "nz"


@pytest.mark.parametrize("days, linkedin, indeed, seek", [
    (14, "r2592000", "14", 14),   # LinkedIn has no 2 weeks: widened to its month
    (30, "r2592000", "", 30),     # Indeed has no month: widened to any time
])
def test_posted_windows_are_never_narrowed(days, linkedin, indeed, seek):
    config = CONFIG.replace("posted_within_days = 1", f"posted_within_days = {days}")
    runs = {r.site: r for r in plan(parse_config(config), "analyst")}
    assert parse_qs(urlsplit(runs["linkedin"].run_input["urls"][0]).query)["f_TPR"] == [linkedin]
    assert runs["indeed"].run_input["postedWithinDays"] == indeed
    assert runs["seek"].run_input["dateRange"] == seek
    widened = {site for site, run in runs.items() if any("isn't available" in n for n in run.notes)}
    assert widened == {14: {"linkedin"}, 30: {"indeed"}}[days]


def test_seek_uses_native_filters_and_skips_countries_it_lacks():
    runs = plan(searches(), "analyst", "seek")
    assert len(runs) == 8  # the UK is not on Seek
    assert any("country GB" in note for note in runs[0].notes)
    sydney = runs[0].run_input
    assert (sydney["country"], sydney["location"]) == ("australia", "Sydney")
    assert sydney["workTypes"] == ["fulltime", "contract"]
    assert sydney["workArrangements"] == ["hybrid", "remote"]
    assert "salaryMin" not in sydney and "salaryMax" not in sydney
    assert sydney["information-communication-technology"] is sydney["engineering-software"] is True
    assert "location" not in runs[2].run_input  # NZ nationwide


# --- Local filters -----------------------------------------------------------------------

def _linkedin(**fields):
    return normalise({"id": "1", "link": "https://www.linkedin.com/jobs/view/1",
                      "title": "Data Analyst", "companyName": "Acme", **fields})


def test_unknown_values_always_pass_local_filters():
    analyst = searches()[0]
    assert filter_reason(_linkedin(), analyst, "linkedin") is None
    # A Seek posting with no salary must not be filtered by salary bounds.
    seek = normalise({"jobLink": "https://www.seek.com.au/job/1", "title": "Data Analyst",
                      "workTypes": "Part time"})
    assert filter_reason(seek, analyst, "seek") is None


def test_local_salary_filter():
    analyst = searches()[0]
    low = _linkedin(salaryInfo=["$60,000.00/yr", "$70,000.00/yr"])
    assert "outside 100,000-150,000/yr" in filter_reason(low, analyst, "linkedin")
    overlapping = _linkedin(salaryInfo=["$140,000.00/yr", "$170,000.00/yr"])
    assert filter_reason(overlapping, analyst, "linkedin") is None
    seek_low = normalise({"jobLink": "https://www.seek.com.au/job/2", "title": "Data Analyst",
                         "salary": "$60,000/year"})
    assert "outside 100,000-150,000/yr" in filter_reason(seek_low, analyst, "seek")
    hourly = normalise({"viewJobLink": "/viewjob?jk=1", "title": "Data Analyst",
                        "salary": {"min": 60, "max": 70, "type": "HOUR"}})
    assert filter_reason(hourly, analyst, "indeed") is None  # ~118k-138k a year


def test_widened_window_drops_older_jobs_after_import():
    from datetime import date
    two_weeks = parse_config(CONFIG.replace("posted_within_days = 1", "posted_within_days = 14"))[0]
    today = date(2026, 9, 26)
    old = _linkedin(postedAt="2026-09-01")      # 25 days old: inside LinkedIn's month, not 2 weeks
    recent = _linkedin(postedAt="2026-09-20")
    assert "older than 2 weeks" in filter_reason(old, two_weeks, "linkedin", today)
    assert filter_reason(recent, two_weeks, "linkedin", today) is None
    assert filter_reason(_linkedin(), two_weeks, "linkedin", today) is None  # no date: kept
    # Indeed has an exact 2-week window, so its results aren't re-checked.
    assert filter_reason(normalise({"viewJobLink": "/viewjob?jk=1", "title": "Data Analyst",
                                    "pubDate": "2026-09-01"}), two_weeks, "indeed", today) is None


def test_local_job_type_and_arrangement_filters():
    analyst = searches()[0]
    assert "job type part-time" in filter_reason(_linkedin(employmentType="Part-time"),
                                                 analyst, "linkedin")
    onsite = normalise({"viewJobLink": "/viewjob?jk=1", "title": "Data Analyst",
                        "jobTypes": ["Full-time"], "attributes": [{"label": "In-person"}]})
    assert "work arrangement on-site" in filter_reason(onsite, analyst, "indeed")


def test_parse_salary_formats():
    assert parse_salary("$90k - $120k + super + bonus") == (90000, 120000)
    assert parse_salary("$120,000 - $140,000 + 11.5% super") == (120000, 140000)
    assert parse_salary("$120,000 - 140,000 + 11.5% super") == (120000, 140000)
    assert parse_salary("$95,000 plus 2 weeks bonus") == (95000, 95000)
    assert parse_salary(["$17.00", "$19.00"]) == (17 * 1976, 19 * 1976)
    assert parse_salary({"min": 28, "max": 32, "type": "HOUR"}) == (28 * 1976, 32 * 1976)
    assert parse_salary("$55 - $65 p.h.") == (55 * 1976, 65 * 1976)
    assert parse_salary("$8,000 per month") == (96000, 96000)
    assert parse_salary("Competitive") is None
    # Words about working hours aren't the pay period: these were read as hourly and weekly
    # pay, a salary in the millions that filtered the jobs out.
    assert parse_salary("$120k package, flexible hours") == (120000, 120000)
    assert parse_salary("$100,000 - $120,000 per annum, 38 hours per week") == (100000, 120000)
    assert parse_salary("$5,000 per month, 38 hours per week") == (60000, 60000)
    # Too large to be a salary: one such value once failed the import of a whole paid run.
    assert parse_salary("Up to $1,000,000,000 per month") is None


# --- Running on Apify --------------------------------------------------------------------

def _run(status="SUCCEEDED", message=None, cost=None):
    return SimpleNamespace(id="r1", status=status, status_message=message,
                           default_dataset_id="d1", started_at=None, finished_at=None,
                           usage_total_usd=cost)


class FakeClient:
    def __init__(self, result, items=()):
        self.result, self.items, self.calls, self.aborted = result, list(items), [], []

    def run(self, run_id):
        client = self

        class Run:
            def abort(self):
                client.aborted.append(run_id)
        return Run()

    def actor(self, actor_id):
        client = self

        class Actor:
            def call(self, **kwargs):
                client.calls.append((actor_id, kwargs))
                return client.result
        return Actor()

    def dataset(self, dataset_id):
        items = self.items

        class Dataset:
            def iterate_items(self, clean):
                return iter(items)
        return Dataset()


def test_run_passes_cost_cap_and_applies_the_search_filter(monkeypatch):
    import jobflow.ingest.store as store
    saved = []
    monkeypatch.setattr(store, "save_run", lambda *a, **k: saved.append(k) or 1)
    capped = parse_config(CONFIG.replace("max_results = 40", "max_results = 40\nmax_cost_usd = 0.5"))
    planned = plan(capped, "analyst", "indeed")[0]
    client = FakeClient(_run(cost=0.02), [{"viewJobLink": "/viewjob?jk=1", "title": "x"}])
    apify.run_search(client, planned, timedelta(minutes=5))
    (_, call), = client.calls
    assert str(call["max_total_charge_usd"]) == "0.5"
    part_time = normalise({"viewJobLink": "/viewjob?jk=2", "title": "x", "jobTypes": ["Part-time"]})
    assert "part-time" in saved[0]["job_filter"](part_time)


@pytest.mark.parametrize("result, message", [
    (_run("FAILED", message="bad input"), "ended with FAILED: bad input"),
    (_run("RUNNING"), "still running after"),  # partial results must not be imported
])
def test_unsuccessful_runs_are_recorded_not_imported(monkeypatch, result, message):
    import jobflow.ingest.store as store
    recorded = []
    monkeypatch.setattr(store, "record_failed_run", lambda *a: recorded.append(a))
    monkeypatch.setattr(store, "save_run", lambda *a, **k: pytest.fail("imported"))
    planned = plan(searches(), "counsel")[0]
    client = FakeClient(result)
    with pytest.raises(RuntimeError, match=message):
        apify.run_search(client, planned, timedelta(minutes=5))
    assert recorded[0][-2:] == ("counsel", planned.run_input)
    # A run still going would keep charging for results never imported: it is stopped.
    assert client.aborted == (["r1"] if result.status == "RUNNING" else [])


def test_a_run_whose_import_fails_is_not_recorded_as_succeeded(monkeypatch):
    import jobflow.ingest.store as store
    recorded = []
    monkeypatch.setattr(store, "record_failed_run", lambda *a: recorded.append(a))

    def save_run(*args, **kwargs):
        raise RuntimeError("numeric field overflow")
    monkeypatch.setattr(store, "save_run", save_run)
    with pytest.raises(RuntimeError, match="overflow"):
        apify.run_search(FakeClient(_run()), plan(searches(), "counsel")[0], timedelta(minutes=5))
    run_id, dataset_id, status = recorded[0][1:4]
    assert (run_id, dataset_id, status) == ("r1", "d1", "IMPORT_FAILED")


def test_one_failed_run_does_not_stop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setenv("JOBFLOW_SEARCHES", CONFIG)
    seen = []

    def fake_run(client, planned, max_wait):
        seen.append(planned.site)
        if planned.site == "linkedin":
            raise RuntimeError("boom")

    monkeypatch.setattr(apify, "run_search", fake_run)
    import jobflow.ingest.store as store
    monkeypatch.setattr(store, "archive_filtered", lambda: 0)
    with pytest.raises(SystemExit, match="1 of 20 Apify runs failed"):
        apify.ingest_searches()
    assert seen.count("indeed") == 10 and seen.count("seek") == 9
    assert "FAILED — boom" in capsys.readouterr().err


def test_each_run_knows_its_country_so_one_ad_in_two_countries_stays_two_jobs():
    runs = plan(searches())
    for run in runs:
        if run.site != "linkedin":
            assert run.country == {"au": "AU", "nz": "NZ", "sg": "SG", "uk": "GB", "australia": "AU",
                                   "new zealand": "NZ", "singapore": "SG"}[run.run_input["country"]]
    # One LinkedIn run covers every place in a search, so a multi-country search has no
    # single country; a one-country search does.
    assert next(r for r in runs if r.site == "linkedin" and r.search.name == "analyst").country is None
    single = parse_config(CONFIG.replace(', NZ = [], SG = ["singapore"], UK = ["London"]', ""))
    assert next(r for r in plan(single) if r.site == "linkedin").country == "AU"
    # Without a run country, LinkedIn-style locations still give one.
    assert country_from_location("Sydney, New South Wales, Australia") == "AU"
    assert country_from_location("Hong Kong SAR") == "HK"
    assert country_from_location("Admiralty, Central and Western District") is None
    # A multi-country LinkedIn run: each job's own search URL still gives its country,
    # even when its location names none.
    singapore_url = next(url for url in next(r for r in runs if r.site == "linkedin"
                                             and r.search.name == "analyst").run_input["urls"]
                         if "Singapore" in url)
    assert search_url_country({"inputUrl": singapore_url}) == "SG"
    assert search_url_country({}) is None


def test_excluded_title_terms_keep_jobs_out_of_the_inbox():
    config = CONFIG.replace("max_results = 40",
                            'max_results = 40\nexclude_title_terms = ["Internship", "Fresh Grad", "Graduate"]')
    search = parse_config(config)[0]
    def reason(title):
        return filter_reason(normalise({"title": title, "companyName": "Acme"}), search, "linkedin")
    # Any capitalisation or punctuation, anywhere in the title.
    assert reason("Quant INTERNSHIP - Summer 2027") == "title contains 'Internship'"
    assert reason("Fresh-Grad Trader") == "title contains 'Fresh Grad'"
    assert reason("2027 Graduate Program") == "title contains 'Graduate'"
    # Whole words only: a longer word containing a term is kept.
    assert reason("Postgraduate Researcher") is None
    assert reason("Quant Analyst") is None
    # Symbols count: "C++" and "C#" once both matched any title with the word "C".
    cpp = parse_config(CONFIG.replace("max_results = 40", 'max_results = 40\nexclude_title_terms = ["C++", "C#"]'))[0]
    assert filter_reason(normalise({"title": "Senior C++ Developer", "companyName": "Acme"}), cpp,
                         "linkedin") == "title contains 'C++'"
    assert filter_reason(normalise({"title": "Series C Fintech Quant", "companyName": "Acme"}), cpp,
                         "linkedin") is None


def test_included_title_terms_override_excluded_ones():
    config = CONFIG.replace("max_results = 40", 'max_results = 40\nexclude_title_terms = ["Analyst"]\n'
                            'include_title_terms = ["Quant Analyst", "Quantitative Analyst"]')
    search = parse_config(config)[0]
    def reason(title):
        return filter_reason(normalise({"title": title, "companyName": "Acme"}), search, "linkedin")
    assert reason("Credit Analyst") == "title contains 'Analyst'"
    assert reason("Quant Analyst - Equities") is None
    assert reason("MGR/AVP, Quantitative Analyst") is None


def test_excluded_employers_keep_jobs_out_even_with_a_kept_title():
    config = CONFIG.replace("max_results = 40", 'max_results = 40\nexclude_companies = ["Acme", "Big Bank"]\n'
                            'include_title_terms = ["Quant Analyst"]')
    search = parse_config(config)[0]
    def reason(company):
        return filter_reason(normalise({"title": "Quant Analyst", "companyName": company}), search, "linkedin")
    assert reason("ACME Pty Ltd") == "employer matches 'Acme'"
    assert reason("Big-Bank Group") == "employer matches 'Big Bank'"
    # Whole words only, and a job with no employer listed is kept.
    assert reason("Acmera") is None
    assert reason(None) is None


def test_old_setting_names_still_work():
    # The GitHub copy of a searches file may predate a rename; the daily import must still
    # read it. Setting both names at once is an error rather than a silent choice.
    old = parse_config(CONFIG.replace("seek_classifications = ", "classifications = "))
    assert old == parse_config(CONFIG)
    with pytest.raises(SearchConfigError, match="set seek_classifications only"):
        parse_config(CONFIG.replace("seek_classifications = [", 'classifications = ["engineering"]\nseek_classifications = ['))
