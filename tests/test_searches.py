from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from jobflow.ingest import apify
from jobflow.ingest.filters import filter_reason
from jobflow.ingest.runs import country_from_location, plan, search_url_country
from jobflow.ingest.searches import SearchConfigError, parse_config
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


SEARCH = 'name = "x"\nterms = ["a"]\nlocations = { AU = [] }\n'
NO_LOCATIONS = '[[search]]\nname = "x"\nterms = ["a"]\n'


@pytest.mark.parametrize("text, message", [
    ("[[search]]\n" + SEARCH + 'job_types = ["fulltime"]\n', "unknown value.*choose from.*full-time"),
    # A typo'd setting must not be silently ignored.
    ("[[search]]\n" + SEARCH + "keywords = 'x'\n", "unknown setting"),
    # A misspelt city would otherwise search for nothing.
    (NO_LOCATIONS + 'locations = { AU = ["Syndey"] }\n', "unknown AU city.*choose from Sydney"),
    # Each of these would run nothing, and say nothing: an empty sites list, and a search on
    # Seek alone for a country Seek doesn't cover.
    ("[[search]]\n" + SEARCH + "sites = []\n", "at least one"),
    (NO_LOCATIONS + 'sites = ["seek"]\nlocations = { US = [] }\n', "would never run"),
    # A quoted number crashed instead of stopping update-searches with a reason.
    ("[[search]]\n" + SEARCH + 'salary_min = "100k"\n', "salary_min must be a number"),
    # 0 was sent to Apify as no cost cap at all.
    ("[[search]]\n" + SEARCH + "max_cost_usd = 0\n", "max_cost_usd must be more than 0"),
    ("[[search]]\n" + SEARCH + "max_cost_usd = nan\n", "max_cost_usd must be a number"),
    # Each crashed with a traceback (or a 500 in the Searches tab) instead of saying why.
    ("[search]\n" + SEARCH, "double brackets"),
    ("defaults = 3\n[[search]]\n" + SEARCH, "defaults must be"),
    ("[default]\nmax_results = 5\n[[search]]\n" + SEARCH, "unknown section"),
    ('[[search]]\nname = "x"\nterms = 5\nlocations = { AU = [] }\n', "terms must be"),
    ("[[search]]\n" + SEARCH + 'seek_classifications = [["a"]]\n', "must be a list"),
    ("[[search]]\n" + SEARCH + "posted_within_days = true\n", "posted_within_days must be"),
])
def test_invalid_config_is_rejected_with_a_reason(text, message):
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
    # With no period stated, Hong Kong and Singapore amounts are usually monthly: read as
    # annual they were a tenth of the real pay, and filtered out by a salary minimum.
    assert parse_salary("HK$35,000", "HK") == (420000, 420000)
    assert parse_salary("$8,000 - $10,000", "SG") == (96000, 120000)
    assert parse_salary("HK$600,000", "HK") == (600000, 600000)
    assert parse_salary("$35,000") == (35000, 35000)   # elsewhere, still annual
    # Under 3,000 was daily pay everywhere: S$2,500 a month read as S$650,000 a year.
    assert parse_salary("$2,000 - $2,800", "SG") == (24000, 33600)
    assert parse_salary("$800") == (800 * 260, 800 * 260)   # elsewhere, still a day rate
    # The k is the range's: read as 60 to 80,000.
    assert parse_salary("$60 - $80k") == (60000, 80000)
    assert parse_salary("$60-80K + super") == (60000, 80000)
    assert parse_salary("$80k-90") == (80000, 90000)   # once 90 to 80,000
    # A Singapore range starting under the monthly cutoff is monthly, though its top is
    # over it: read as annual, S$25-35k a month failed a S$100k minimum.
    assert parse_salary("25000-35000", "SG") == (300000, 420000)
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
    # The cost cap binds only a pay-per-event actor: the result cap still holds if an actor
    # moves to pay-per-result pricing (Indeed's changed in September 2026).
    assert call["max_items"] == 40
    # Apify stops the run itself too, in case this process is killed while it waits.
    assert call["run_timeout"] == call["wait_duration"] == timedelta(minutes=5)
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
    import jobflow.ingest.upkeep as upkeep
    monkeypatch.setattr(upkeep, "archive_old_jobs", lambda: 0)
    monkeypatch.setattr(upkeep, "prune_sightings", lambda: 0)
    monkeypatch.setattr(upkeep, "prune_archived_jobs", lambda: 0)
    with pytest.raises(SystemExit, match="1 of 20 Apify runs failed"):
        apify.ingest_searches()
    assert seen.count("indeed") == 10 and seen.count("seek") == 9
    assert "FAILED — boom" in capsys.readouterr().err
    # An error every run would hit (the account's usage limit) skips the runs not started:
    # each site's first run at most, not all 20.
    seen.clear()

    def over_limit(client, planned, max_wait):
        seen.append(planned.site)
        raise RuntimeError("Monthly usage hard limit exceeded")
    monkeypatch.setattr(apify, "run_search", over_limit)
    with pytest.raises(SystemExit, match="weren't started"):
        apify.ingest_searches()
    assert 1 <= len(seen) <= 3


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


def test_excluded_titles_and_employers_keep_jobs_out_of_the_inbox():
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
    # A kept title overrides an excluded term, but not an excluded employer.
    kept = parse_config(CONFIG.replace("max_results = 40", 'max_results = 40\n'
                                       'exclude_title_terms = ["Analyst"]\n'
                                       'include_title_terms = ["Quant Analyst", "Quantitative Analyst"]\n'
                                       'exclude_companies = ["Acme", "Big Bank"]'))[0]

    def judged(title, company):
        return filter_reason(normalise({"title": title, "companyName": company}), kept, "linkedin")
    assert judged("Credit Analyst", "Beta") == "title contains 'Analyst'"
    assert judged("MGR/AVP, Quantitative Analyst", "Beta") is None
    assert judged("Quant Analyst", "ACME Pty Ltd") == "employer matches 'Acme'"
    assert judged("Quant Analyst", "Big-Bank Group") == "employer matches 'Big Bank'"
    # Whole words only, and a job with no employer listed is kept.
    assert judged("Quant Analyst", "Acmera") is None and judged("Quant Analyst", None) is None


def test_a_term_or_city_listed_twice_is_searched_and_paid_for_once():
    searches = parse_config('[[search]]\nname = "a"\nsites = ["indeed"]\n'
                            'terms = ["Data analyst", "data Analyst"]\n'
                            'locations = { AU = ["Sydney", "sydney"] }\n')
    assert len(plan(searches)) == 1
    with pytest.raises(SearchConfigError, match="GB is listed twice"):
        parse_config('[[search]]\nname = "a"\nterms = ["x"]\nlocations = { UK = [], GB = ["London"] }\n')


def test_runs_that_could_outlast_the_daily_job_are_not_started(monkeypatch):
    # GitHub stops the daily job at 180 minutes: the runs not yet started were lost unseen,
    # and the archiving and pruning after them never ran.
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setenv("JOBFLOW_SEARCHES", CONFIG)
    clock = [0.0]
    monkeypatch.setattr(apify.time, "monotonic", lambda: clock[0])
    seen, recorded = [], []

    def slow_run(client, planned, max_wait):
        seen.append(planned.label)
        clock[0] += 25 * 60   # each run takes 25 minutes
    monkeypatch.setattr(apify, "run_search", slow_run)
    import jobflow.ingest.store as store
    import jobflow.ingest.upkeep as upkeep
    monkeypatch.setattr(store, "record_failed_run", lambda *a, **k: recorded.append(a[3]))
    upkept = []
    monkeypatch.setattr(upkeep, "archive_old_jobs", lambda: upkept.append(1) or 0)
    monkeypatch.setattr(upkeep, "prune_sightings", lambda: 0)
    monkeypatch.setattr(upkeep, "prune_archived_jobs", lambda: 0)
    # 10 Indeed runs, each allowed 30 minutes, within 150: a run may start up to minute 120.
    # At 25 minutes each, 5 start (at 0 to 100), and 5 are recorded as not started.
    with pytest.raises(SystemExit, match="5 weren't started, to finish within 150 minutes"):
        apify.ingest_searches(only_site="indeed")
    assert len(seen) == 5 and recorded == ["NOT_STARTED"] * 5 and upkept
