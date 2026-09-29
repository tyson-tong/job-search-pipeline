"""Against a real PostgreSQL: merging, saving, and migrating, where a silent mistake loses
or corrupts data. Skipped unless TEST_DATABASE_URL names a disposable database; each run
works in its own schema and drops it afterwards."""
import os
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from jobflow.db import connect, migrate, migration_files, set_job_state
from jobflow.ingest.store import archive_old_jobs, prune_archived_jobs, prune_sightings, save_run
from jobflow.web.queries import Conflict, list_jobs, parse_filters, save_changes

AD = ("We are hiring a Data Analyst to build dashboards, write SQL, and automate reporting in "
      "Python for our finance and operations teams across the region.")
LINKEDIN = {"id": "4012345678", "link": "https://au.linkedin.com/jobs/view/4012345678",
            "title": "Data Analyst", "companyName": "Acme Pty Ltd", "location": "Sydney, NSW",
            "descriptionText": AD}
INDEED = {"id": "a1b2c3", "viewJobLink": "/viewjob?jk=a1b2c3", "title": "Data Analyst",
          "companyDetails": {"name": "Acme"}, "formattedLocation": "Sydney NSW 2000",
          "location": {"countryCode": "AU"}, "jobDescriptionHTML": f"<p>{AD}</p>"}
SEEK = {"id": "81234567", "jobLink": "https://www.seek.com.au/job/81234567",
        "title": "Data Analyst", "advertiser": {"name": "ACME Limited"},
        "joblocationInfo": {"displayLocation": "Sydney NSW"},
        "content": {"unEditedContent": f"<p>{AD}</p>\x00"}}   # a NUL, as scraped HTML can have


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database to run these")
    schema = f"jobflow_test_{secrets.token_hex(4)}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setenv("DATABASE_URL", make_conninfo(url, options=f"-c search_path={schema}"))
    try:
        migrate()
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _one(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def _rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_migrations_apply_once_on_an_empty_database(database, capsys):
    migrate()   # already applied by the fixture: nothing runs again
    assert "Applied migration" not in capsys.readouterr().out
    assert _one("SELECT count(*) FROM schema_migrations")[0] == len(migration_files())


def test_one_job_on_three_sites_is_one_row_with_three_copies(database):
    assert save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU") == 1
    assert save_run("indeed:a", "r2", "d2", [INDEED], country="AU") == 0
    assert save_run("seek:a", "r3", "d3", [SEEK], country="AU") == 0   # the NUL didn't fail it
    assert save_run("linkedin:a", "r4", "d4", [LINKEDIN], country="AU") == 0  # seen again
    assert _one("SELECT count(*) FROM jobs")[0] == 1
    assert _one("SELECT count(*) FROM job_sources")[0] == 3
    assert _one("SELECT count(*) FROM job_observations")[0] == 4


def test_two_different_jobs_are_never_merged(database):
    # A wrong merge loses a job: it never reaches the Inbox.
    def ad(job_id, location, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": "Data Analyst", "companyName": "Acme", "location": location,
                "descriptionText": " ".join(words)}
    duties = [f"duty{i}" for i in range(60)]
    edited = duties[:50] + [f"other{i}" for i in range(10)]   # about 70% the same words
    save_run("linkedin:a", "r1", "d1", [ad("1", "Sydney, NSW", duties)], country="AU")
    # The same employer and title with a similar ad, but in another city: another office.
    assert save_run("linkedin:a", "r2", "d2", [ad("2", "Melbourne, VIC", edited)], country="AU") == 1
    # The same ad word for word, for an office in another country: another job, even when
    # its location names no city to tell the two apart.
    assert save_run("linkedin:a", "r3", "d3", [ad("3", "Hong Kong", duties)], country="HK") == 1
    # The check: in the same city, that similar ad is the same job.
    assert save_run("linkedin:a", "r4", "d4", [ad("4", "Sydney NSW 2000", edited)], country="AU") == 0
    assert _one("SELECT count(*) FROM jobs")[0] == 3


def test_the_job_list_and_the_job_panel_show_the_same_similar_jobs(database):
    # They were worked out by two versions of the rules, which could disagree.
    from jobflow.web.queries import job_detail

    def ad(job_id, title, company, location, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": company, "location": location,
                "descriptionText": " ".join(words)}
    duties = [f"duty{i}" for i in range(60)]
    reposted = duties[:45] + [f"other{i}" for i in range(15)]   # about 60% the same words
    save_run("linkedin:a", "r1", "d1", [ad("1", "Data Analyst", "Acme", "Sydney", duties)], country="AU")
    save_run("linkedin:a", "r2", "d2", [ad("2", "Senior Data Analyst", "Hays", "Sydney", reposted)],
             country="AU")
    save_run("linkedin:a", "r3", "d3", [ad("3", "Data Analyst", "Acme", "Hong Kong SAR", duties)], country="HK")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    listed = {job["id"]: job for job in list_jobs(parse_filters({"status": ["new"]}))}
    panel = job_detail(ids["1"])
    assert [d["id"] for d in listed[ids["1"]]["duplicates"]] == [d["id"] for d in panel["duplicates"]] \
        == [ids["2"]]
    assert [d["id"] for d in listed[ids["1"]]["elsewhere"]] == [d["id"] for d in panel["elsewhere"]] \
        == [ids["3"]]
    assert panel["duplicates"][0]["company"] == "Hays" and panel["duplicates"][0]["level"] == "similar"


def test_a_cv_named_later_is_scored_without_rescoring_the_others(database, monkeypatch):
    # Scores were never refreshed, so renaming or adding a CV left every scored job with
    # no score against it: tailor-cv then refused them all.
    import json

    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
    asked = []

    def complete(system, user, **kwargs):
        asked.append(json.loads(user)["cv_variant"])
        return json.dumps({"requirements": [{"requirement": "SQL", "status": "matched",
                                             "evidence": ["write SQL"]}]}), "m"
    monkeypatch.setattr(matching, "complete", complete)
    monkeypatch.setattr(matching, "matching_weights", lambda: (0.75, 0.25))
    cvs = {"dev": "I write SQL daily."}
    monkeypatch.setattr(matching, "chosen_sources", lambda: dict(cvs))
    assert [r["variant"] for r in matching.match_job(job_id)] == ["dev"]
    cvs["strat"] = "I write SQL for strategy work."   # a second CV, named later
    assert matching.jobs_needing_match(list(cvs)) == [job_id]
    assert sorted(r["variant"] for r in matching.match_job(job_id)) == ["dev", "strat"]
    assert asked == ["dev", "strat"]                  # dev was not scored again
    assert matching.jobs_needing_match(list(cvs)) == [] and matching.count_needing_match(list(cvs)) == 0
    matching.match_job(job_id)
    assert asked == ["dev", "strat"]


def test_a_status_change_keeps_the_first_applied_date_and_every_note(database):
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="applied", note="Sent CV", via="test")
    applied_at = _one("SELECT applied_at FROM applications")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interviewing", note="Phone screen", via="test")
    applied_again, notes = _one("SELECT applied_at, notes FROM applications")
    assert applied_at is not None and applied_again == applied_at
    first, second = notes.split("\n")
    assert first.endswith(": Sent CV") and second.endswith(": Phone screen")
    assert _one("SELECT count(*) FROM status_history WHERE job_id=%s", job_id)[0] == 2


def test_a_save_over_a_change_made_elsewhere_writes_nothing(database):
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job = list_jobs(parse_filters({"status": ["new"]}))[0]
    with connect() as conn:   # changed in another tab after this page loaded it
        set_job_state(conn, job["id"], status="interested", via="test")
    with pytest.raises(Conflict):
        save_changes([{"id": job["id"], "status": "not_interested",
                       "expected_version": job["version"]}])
    assert _one("SELECT status FROM jobs")[0] == "interested"


def test_old_filtered_not_interested_and_untriaged_inbox_jobs_are_archived(database):
    from datetime import date

    def ad(job_id, title, posted, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": f"Employer {job_id}", "postedAt": posted,
                "descriptionText": " ".join(f"{words}{i}" for i in range(40))}
    filtered = lambda posting: "title contains 'Intern'"
    save_run("linkedin:a", "r1", "d1", [ad("1", "Intern", "2026-09-10", "old")], job_filter=filtered)
    save_run("linkedin:a", "r2", "d2", [ad("2", "Intern", "2026-09-18", "recent")], job_filter=filtered)
    save_run("linkedin:a", "r3", "d3", [ad("3", "Analyst", "2026-09-01", "inbox")])
    save_run("linkedin:a", "r5", "d5", [ad("5", "Trader", "2026-09-01", "dropped"),
                                         ad("6", "Quant", "2026-09-01", "fresh"),
                                         ad("7", "Engineer", "2026-08-01", "stale"),
                                         ad("8", "Developer", "2026-08-01", "returned")])
    with connect() as conn:   # marked not interested: job 5 on 09-10, job 6 on 09-20
        for job_id, marked in (("5", "2026-09-10"), ("6", "2026-09-20")):
            conn.execute("UPDATE jobs SET status='not_interested', status_updated_at=%s "
                         "WHERE source_job_id=%s", (marked, job_id))
        # In the Inbox since 08-01 (job 7), and moved back to it on 09-20 (job 8).
        for job_id, since in (("7", "2026-08-01"), ("8", "2026-09-20")):
            conn.execute("UPDATE jobs SET status_updated_at=%s WHERE source_job_id=%s",
                         (since, job_id))
    # 2026-09-28: job 1 is 18 days past posting; job 2 is 10; job 5 was marked 18 days ago,
    # job 6 8 days ago, though both were posted weeks ago. Inbox jobs wait 30 days: job 3
    # was posted 27 days ago, job 7 58 days ago and never triaged; job 8 is as old, but
    # was moved back to the Inbox 8 days ago.
    assert archive_old_jobs(date(2026, 9, 28)) == 3
    with connect() as conn:
        statuses = dict(conn.execute("SELECT source_job_id, status FROM jobs").fetchall())
    assert statuses == {"1": "archived", "2": "filtered", "3": "new", "5": "archived",
                        "6": "not_interested", "7": "archived", "8": "new"}
    assert sorted(_rows("SELECT from_status, via FROM status_history")) == [
        ("filtered", "archive"), ("new", "archive"), ("not_interested", "archive")]
    # Still a known job: seeing it again adds no new one, and it stays archived.
    assert save_run("linkedin:a", "r4", "d4", [ad("1", "Intern", "2026-09-10", "old")]) == 0
    assert _one("SELECT status FROM jobs WHERE source_job_id='1'")[0] == "archived"


def test_a_draft_failing_the_check_after_an_earlier_one_passed_is_drafted_again(database, tmp_path,
                                                                                   monkeypatch):
    from jobflow import batch
    from jobflow.candidate import paths

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='interested'")
        # It passed, its output was deleted, and the new draft failed the fact check.
        for status in ("SUCCEEDED", "NEEDS-FIX"):
            conn.execute("INSERT INTO workflow_runs(job_id, kind, status) VALUES (%s, 'tailor', %s)",
                         (job_id, status))
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    folder = paths.job_directory(job_id, {"company": LINKEDIN["companyName"], "title": LINKEDIN["title"]})
    folder.mkdir(parents=True)
    (folder / "CV_dev_draft.yaml").write_text("cv: {}")   # the failed draft
    ran = []
    batch._run_jobs("interested", "tailor", "a tailored CV", ran.append,
                  lambda folder: any(folder.glob("CV_*_draft.yaml")), lambda folder: False)
    assert ran == [job_id]


def test_update_searches_filters_the_inbox_but_not_a_job_moved_back_from_filtered(database):
    from jobflow.ingest.searches import parse_config
    from jobflow.ingest.store import exclude_inbox_jobs

    search = parse_config('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
                          'exclude_title_terms = ["Intern"]\n')[0]

    def ad(job_id, title):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}", "title": title,
                "companyName": f"Employer {job_id}", "descriptionText": f"ad {job_id}"}
    save_run("linkedin:a", "r1", "d1", [ad("1", "Intern Analyst"), ad("2", "Intern Trader"),
                                         ad("3", "Analyst")], search_name="s", country="AU")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    with connect() as conn:   # filtered earlier, then moved back to the Inbox by hand
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=%s", (ids["2"],))
        set_job_state(conn, ids["2"], status="new", via="web")
    assert [job["id"] for job in exclude_inbox_jobs([search], dry_run=True)[0]] == [ids["1"]]
    assert _one("SELECT count(*) FROM jobs WHERE status='filtered'")[0] == 0   # dry run
    exclude_inbox_jobs([search])
    assert dict(_rows("SELECT source_job_id, status FROM jobs")) == {
        "1": "filtered", "2": "new", "3": "new"}


def test_a_job_whose_search_was_renamed_is_judged_by_the_defaults(database):
    from jobflow.ingest.searches import defaults_search, parse_config
    from jobflow.ingest.store import exclude_inbox_jobs

    text = ('[defaults]\nexclude_title_terms = ["Intern"]\n'
            '[[search]]\nname = "renamed"\nterms = ["a"]\nlocations = { AU = [] }\n')
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "title": "Intern Analyst"}],
             search_name="old-name", country="AU")
    matched, _ = exclude_inbox_jobs(parse_config(text), fallback=defaults_search(text))
    assert [job["reason"] for job in matched] == ["title contains 'Intern'"]


def test_one_posting_the_database_rejects_does_not_lose_the_rest_of_a_paid_run(database):
    # A JSON "\ud800" escape is valid JSON, and PostgreSQL rejects it.
    broken = {**SEEK, "id": "999", "title": "Analyst \ud800", "jobLink": "https://www.seek.com.au/job/999"}
    assert save_run("seek:a", "r1", "d1", [broken, LINKEDIN], country="AU") == 1
    assert _one("SELECT title FROM jobs")[0] == "Data Analyst"


def test_old_sightings_lose_only_their_scraped_record(database):
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    save_run("indeed:a", "r2", "d2", [INDEED], country="AU")
    with connect() as conn:   # the LinkedIn sighting is 31 days old
        conn.execute("""UPDATE job_observations SET observed_at = now() - interval '31 days'
                        WHERE scraper_run_id = (SELECT id FROM scraper_runs WHERE apify_run_id='r1')""")
    assert prune_sightings() == 1
    assert _rows("SELECT observed_json IS NULL FROM job_observations ORDER BY observed_at") == [
        (True,), (False,)]
    assert _one("SELECT raw_json->>'id' FROM jobs")[0] == LINKEDIN["id"]   # the job keeps its record
    # A later sighting still merges by the site's ID.
    assert save_run("linkedin:a", "r3", "d3", [LINKEDIN], country="AU") == 0


def test_jobs_archived_over_90_days_ago_lose_their_ad_but_are_still_recognised(database):
    save_run("linkedin:a", "r1", "d1", [LINKEDIN, {**SEEK, "title": "Quant"}], country="AU")
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='archived', status_updated_at = now() - interval '91 days' "
                     "WHERE source_job_id=%s", (LINKEDIN["id"],))
        conn.execute("UPDATE jobs SET status='archived', status_updated_at = now() - interval '89 days' "
                     "WHERE source_job_id=%s", (SEEK["id"],))
    assert prune_archived_jobs() == 1
    assert sorted(_rows("SELECT source_job_id, description IS NULL, raw_json IS NULL FROM jobs")) == [
        (LINKEDIN["id"], True, True), (SEEK["id"], False, False)]
    # Seen again, the cleared job is still the same job, and stays archived.
    assert save_run("linkedin:a", "r2", "d2", [LINKEDIN], country="AU") == 0
    assert _one("SELECT status FROM jobs WHERE source_job_id=%s", LINKEDIN["id"])[0] == "archived"


def test_dates_are_in_your_timezone_in_python_and_the_database(database, monkeypatch):
    # The database's own dates were UTC: a day behind Sydney every morning, so the Posted
    # column, the posted-within filter, and archiving disagreed with today's date.
    from jobflow.settings import today

    monkeypatch.setenv("JOBFLOW_TIMEZONE", "Pacific/Kiritimati")   # UTC+14
    assert _one("SELECT current_setting('TimeZone'), now()::date") == ("Pacific/Kiritimati", today())
