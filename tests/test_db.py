"""Against a real PostgreSQL: merging, saving, and migrating, where a silent mistake loses
or corrupts data. Skipped unless TEST_DATABASE_URL names a disposable database; each run
works in its own schema and drops it afterwards."""
import json
import os
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from jobflow.db import connect, migrate, migration_files, set_job_state
from jobflow.ingest import similar
from jobflow.ingest.store import save_run
from jobflow.ingest.upkeep import archive_old_jobs, prune_archived_jobs, prune_sightings
from jobflow.web.queries import list_jobs, parse_filters
from jobflow.web.saving import Conflict, save_changes

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
    # Ads read in another test's schema, whose job ids this one's reuse.
    monkeypatch.setattr(similar, "_AD_WORDS", {})
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
                "title": "Software Engineer", "companyName": "Big Bank", "location": location,
                "descriptionText": " ".join(words)}
    # Two roles sharing the employer's standard text (about us, benefits), which is most
    # of each ad: 85% of the shorter ad's words, and 74% of both ads' words together.
    standard = [f"benefit{i}" for i in range(90)]
    payments = standard + [f"payments{i}" for i in range(20)]
    lending = standard + [f"lending{i}" for i in range(20)]
    save_run("linkedin:a", "r1", "d1", [ad("1", "Sydney, NSW", payments)], country="AU")
    assert save_run("linkedin:a", "r2", "d2", [ad("2", "Sydney NSW", lending)], country="AU") == 1
    # The same ad word for word in another city is another office's job; in another country
    # too, even when its location names no city to tell the two apart.
    assert save_run("linkedin:a", "r3", "d3", [ad("3", "Melbourne, VIC", payments)], country="AU") == 1
    assert save_run("linkedin:a", "r4", "d4", [ad("4", "Hong Kong", payments)], country="HK") == 1
    # Word for word with no location at all, in two countries: the exact-copy rule too.
    markets = standard + [f"markets{i}" for i in range(20)]
    assert save_run("linkedin:a", "r6", "d6", [ad("6", None, markets)], country="AU") == 1
    assert save_run("linkedin:a", "r7", "d7", [ad("7", None, markets)], country="SG") == 1
    # The check: another site's cut-down copy of the first ad, in the same city, is that job.
    seek = {"id": "5", "jobLink": "https://www.seek.com.au/job/5", "title": "Software Engineer",
            "advertiser": {"name": "Big Bank Limited"}, "joblocationInfo": {"displayLocation": "Sydney NSW"},
            "content": {"unEditedContent": " ".join(payments[:80] + payments[90:])}}
    assert save_run("seek:a", "r5", "d5", [seek], country="AU") == 0
    assert _one("SELECT count(*) FROM jobs")[0] == 6


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


def test_rescoring_redoes_only_the_edited_cv_and_only_when_asked(database, monkeypatch):
    # Scores were never redone, so a skill you added to your CV stayed a gap in skill-gaps.
    # Each rescore is an LLM call per job per CV, so an unedited CV must not be sent again.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
    asked = []

    def complete(system, user, **kwargs):
        prompt = json.loads(user)
        asked.append(prompt["cv_variant"])
        status = "matched" if "SQL" in prompt["cv_source"] else "not_evidenced"
        return json.dumps({"requirements": [{"requirement": "SQL", "status": status,
                                             "evidence": ["write SQL"]}]}), "m"
    monkeypatch.setattr(matching, "complete", complete)
    monkeypatch.setattr(matching, "matching_weights", lambda: (0.75, 0.25))
    cvs = {"dev": "I build dashboards.", "strat": "I plan strategy."}
    monkeypatch.setattr(matching, "chosen_sources", lambda: dict(cvs))
    matching.match_job(job_id)
    cvs["dev"] = "I build dashboards and write SQL."   # the gap, now shown in the CV
    assert matching.outdated_scores(matching.current_digests()) == [(job_id, "dev")]
    matching.match_job(job_id)                        # not asked to rescore: nothing sent
    assert asked == ["dev", "strat"]
    scores = {r["variant"]: r["score"] for r in matching.match_job(job_id, rescore=True)}
    assert asked == ["dev", "strat", "dev"] and scores == {"dev": 100, "strat": 0}
    assert matching.outdated_scores(matching.current_digests()) == []


@pytest.mark.parametrize("cvs, failure", [
    # Saving an answer listing no requirements would fix the score at 0 until the CV changes.
    (["dev"], "no requirements"),
    # Saving the one CV that worked would leave the job scored without the other.
    (["dev", "strat"], "timed out"),
])
def test_a_failed_assessment_saves_no_score(database, monkeypatch, cvs, failure):
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")

    def complete(system, user, **kwargs):
        if failure == "no requirements":
            return '{"requirements": []}', "m"
        if json.loads(user)["cv_variant"] == "strat":
            raise RuntimeError("Read timed out")
        return json.dumps({"requirements": [{"requirement": "SQL", "status": "matched",
                                             "evidence": ["SQL"]}]}), "m"
    monkeypatch.setattr(matching, "complete", complete)
    monkeypatch.setattr(matching, "matching_weights", lambda: (0.75, 0.25))
    monkeypatch.setattr(matching, "chosen_sources", lambda: {cv: "I write SQL." for cv in cvs})
    with pytest.raises(RuntimeError, match=failure):
        matching.match_job(job_id)
    assert _one("SELECT count(*) FROM match_assessments")[0] == 0


def test_the_tracker_and_tailor_cv_rank_cvs_alike_after_the_weights_change(database, monkeypatch):
    # The tracker showed the score stored at scoring time while tailor-cv recomputed it with
    # the weights now, so after a weights change they could pick different CVs.
    from jobflow.candidate import matching
    from jobflow.web.queries import job_detail

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
        # Scored by an earlier version, in a format that can't be recomputed.
        conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score, evidence)
                        VALUES (%s, 'dev', 'old', 90, '{"format": 1}')""", (job_id,))

    def complete(system, user, **kwargs):
        dev = json.loads(user)["cv_variant"] == "dev"
        # dev shows the required skill but not the preferred one; strat the other way round.
        return json.dumps({"requirements": [
            {"requirement": "SQL", "importance": "required",
             "status": "matched" if dev else "not_evidenced", "evidence": ["SQL"]},
            {"requirement": "Tableau", "importance": "preferred",
             "status": "not_evidenced" if dev else "matched", "evidence": ["Tableau"]}]}), "m"
    monkeypatch.setattr(matching, "complete", complete)
    monkeypatch.setattr(matching, "chosen_sources", lambda: {"dev": "SQL", "strat": "Tableau"})
    weights = [(0.75, 0.25)]
    monkeypatch.setattr(matching, "matching_weights", lambda: weights[0])
    # An old-format score once made the job unscorable, so tailor-cv refused it for good.
    assert sorted(job_id for job_id in matching.jobs_needing_match(["dev", "strat"])) == [job_id]
    assert matching.best_variant(matching.match_job(job_id))["variant"] == "dev"
    weights[0] = (0.2, 0.8)   # now the preferred skill matters most
    # A perfect score against a CV no longer named in cvs isn't the best, either.
    with connect() as conn:
        conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score, evidence)
                        VALUES (%s, 'retired', 'x', 100, %s)""", (job_id, json.dumps(
            {"format": 2, "breakdown": {"required": {"count": 1, "weight": 1, "credit": 1.0}}})))
    monkeypatch.setattr(matching, "current_digests", lambda: {"dev": "d", "strat": "s"})
    best = matching.best_variant([r for r in matching.match_job(job_id) if r["variant"] != "retired"])
    listed = list_jobs(parse_filters({"status": ["interested"]}))[0]
    assert (best["variant"], best["score"]) == (listed["cv_variant"], listed["score"]) == ("strat", 80)
    assert [(m["variant"], m["score"]) for m in job_detail(job_id)["matches"]] == [
        ("strat", 80), ("dev", 20)]


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


def test_a_save_changes_only_what_it_names(database):
    from datetime import date

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='filtered', filter_reason=%s WHERE id=%s",
                     ("title contains 'Data'", job_id))
    save_changes([{"id": job_id, "status": "new", "follow_up_on": "2026-10-09"}])
    # Moved to the Inbox, a job kept its old reason, and the panel still said "filtered".
    assert _one("SELECT status, filter_reason FROM jobs") == ("new", None)
    save_changes([{"id": job_id, "note": "Called the recruiter"}])   # a note alone
    assert _one("SELECT follow_up_on FROM applications")[0] == date(2026, 10, 9)
    save_changes([{"id": job_id, "follow_up_on": None}])             # cleared on purpose
    assert _one("SELECT follow_up_on FROM applications")[0] is None


def test_a_save_over_a_change_made_elsewhere_writes_nothing(database):
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job = list_jobs(parse_filters({"status": ["new"]}))[0]
    with connect() as conn:   # changed in another tab after this page loaded it
        set_job_state(conn, job["id"], status="interested", via="test")
    with pytest.raises(Conflict) as caught:
        save_changes([{"id": job["id"], "status": "not_interested",
                       "expected_version": job["version"]}])
    assert _one("SELECT status FROM jobs")[0] == "interested"
    # Once you've seen the change, saving against its version works: Cancel in the conflict
    # dialog kept the old version, so every later Save conflicted again.
    save_changes([{"id": job["id"], "status": "not_interested",
                   "expected_version": caught.value.conflicts[0]["version"]}])
    assert _one("SELECT status FROM jobs")[0] == "not_interested"


def test_old_filtered_not_interested_and_untriaged_inbox_jobs_are_archived(database):
    from datetime import date

    def ad(job_id, title, posted, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": f"Employer {job_id}", "postedAt": posted,
                "descriptionText": " ".join(f"{words}{i}" for i in range(40))}
    filtered = lambda posting: "title contains 'Intern'"
    save_run("linkedin:a", "r1", "d1", [ad("1", "Intern", "2026-09-10", "old")], job_filter=filtered)
    save_run("linkedin:a", "r2", "d2", [ad("2", "Intern", "2026-09-18", "recent")], job_filter=filtered)
    save_run("linkedin:a", "r9", "d9", [ad("9", "Intern", "2026-08-20", "late")], job_filter=filtered)
    save_run("linkedin:a", "r3", "d3", [ad("3", "Analyst", "2026-09-01", "inbox")])
    save_run("linkedin:a", "r5", "d5", [ad("5", "Trader", "2026-09-01", "dropped"),
                                         ad("6", "Quant", "2026-09-01", "fresh"),
                                         ad("7", "Engineer", "2026-08-01", "stale"),
                                         ad("8", "Developer", "2026-08-01", "returned")])
    with connect() as conn:
        # Each first seen on its posted date, except job 9: posted on 08-20, first seen on
        # 09-20 (in a month-long search), so it still gets its 2 weeks in Filtered.
        conn.execute("UPDATE jobs SET first_seen_at = posted_at WHERE source_job_id <> '9'")
        conn.execute("UPDATE jobs SET first_seen_at = '2026-09-20' WHERE source_job_id = '9'")
        # Marked not interested: job 5 on 09-10, job 6 on 09-20.
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
                        "6": "not_interested", "7": "archived", "8": "new", "9": "filtered"}
    assert sorted(_rows("SELECT from_status, via FROM status_history")) == [
        ("filtered", "archive"), ("new", "archive"), ("not_interested", "archive")]
    # Still a known job: seeing it again adds no new one, and it stays archived.
    assert save_run("linkedin:a", "r4", "d4", [ad("1", "Intern", "2026-09-10", "old")]) == 0
    assert _one("SELECT status FROM jobs WHERE source_job_id='1'")[0] == "archived"


def _tailor(run_one, output_exists, **more):
    """tailor-cv's JobCommand, with test doubles for its work."""
    from jobflow import batch
    return batch.JobCommand(command="tailor-cv", status="interested", kind="tailor",
                            what="a tailored CV", run_one=run_one, output_exists=output_exists,
                            **more)


def test_a_job_is_done_only_while_its_output_exists_and_its_last_draft_passed(database, tmp_path,
                                                                            monkeypatch):
    # clean-up deletes folders but not the runs that made them: a job moved back to
    # Interested must be drafted again, not skipped as done.
    from jobflow import batch
    from jobflow.candidate import paths

    titles = {"1": "Analyst", "2": "Engineer", "3": "Quant", "4": "Trader"}
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": title,
                                         "descriptionText": f"{title} role"}
                                        for key, title in titles.items()], country="AU")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    runs = {"1": ["SUCCEEDED"],                # done, output deleted
            "2": ["SUCCEEDED"],                # done, output there
            "3": ["NEEDS-FIX"],                # failed the check, fixed by hand
            "4": ["SUCCEEDED", "NEEDS-FIX"]}   # passed, output deleted, then failed
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='interested'")
        for key, statuses in runs.items():
            for status in statuses:
                conn.execute("INSERT INTO workflow_runs(job_id, kind, status) "
                             "VALUES (%s, 'tailor', %s)", (ids[key], status))
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    for key in ("2", "3", "4"):
        folder = paths.job_directory(ids[key], {"company": LINKEDIN["companyName"],
                                                "title": titles[key]})
        folder.mkdir(parents=True)
        (folder / "CV_dev_draft.yaml").write_text("cv: {}")
    ran, extra = [], []
    batch._run_jobs(_tailor(ran.append, lambda folder: any(folder.glob("CV_*_draft.yaml")),
                            fixed_by_hand=lambda folder: folder.name.startswith("quant"),
                            done_one=lambda folder: extra.append(folder.name)))
    assert sorted(ran) == sorted([ids["1"], ids["4"]])
    assert _one("SELECT count(*) FROM workflow_runs WHERE status='FIXED-BY-HAND'")[0] == 1
    # --format's extra file, for the jobs already done.
    assert sorted(extra) == [f"engineer-{ids['2']}", f"quant-{ids['3']}"]
    # --redo: the named job again, done or not; no other.
    ran.clear()
    batch._run_jobs(_tailor(ran.append, lambda folder: True), only=[ids["2"]], redo=True)
    assert ran == [ids["2"]]


def test_job_option_does_only_those_jobs_and_only_with_the_right_status(database, tmp_path,
                                                                       monkeypatch):
    # --job 42 must not draft (and pay for) every other Interested job too.
    from jobflow import batch
    from jobflow.candidate import paths

    save_run("linkedin:a", "r1", "d1", [LINKEDIN, {**LINKEDIN, "id": "2", "title": "Quant",
                                                   "descriptionText": "Price options."},
                                        {**LINKEDIN, "id": "3", "title": "Trader",
                                         "descriptionText": "Trade futures."}], country="AU")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='interested' WHERE source_job_id <> '3'")
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    ran = []
    tailor = _tailor(ran.append, lambda folder: False)
    batch._run_jobs(tailor, only=[ids["2"]])
    assert ran == [ids["2"]]
    with pytest.raises(RuntimeError, match=f"#{ids['3']} is new"):
        batch._run_jobs(tailor, only=[ids["3"]])
    assert ran == [ids["2"]]


def test_update_searches_filters_the_inbox_but_not_a_job_moved_back_from_filtered(database):
    from jobflow.ingest.refilter import exclude_inbox_jobs
    from jobflow.ingest.searches import parse_config

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


def test_relaxing_a_filter_brings_back_only_the_jobs_it_kept_out(database):
    # Dropping an excluded term used to leave the jobs it had filtered hidden until archived.
    from functools import partial

    from jobflow.ingest.filters import filter_reason
    from jobflow.ingest.refilter import exclude_inbox_jobs, restore_filtered_jobs
    from jobflow.ingest.searches import parse_config

    base = ('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
            'salary_min = 100000\n')
    strict = parse_config(base + 'exclude_title_terms = ["Intern"]\n')[0]

    def ad(job_id, title, salary=None):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}", "title": title,
                "companyName": f"Employer {job_id}", "descriptionText": f"ad {job_id}",
                **({"salaryInfo": salary} if salary else {})}
    save_run("linkedin:a", "r1", "d1", [ad("1", "Intern Analyst"), ad("2", "Analyst", ["$60,000/yr"])],
             search_name="s", country="AU", job_filter=partial(filter_reason, search=strict, site="linkedin"))
    assert dict(_rows("SELECT source_job_id, status FROM jobs")) == {"1": "filtered", "2": "filtered"}
    relaxed = parse_config(base)
    restored, checked = restore_filtered_jobs(relaxed, dry_run=True)
    assert [job["reason"] for job in restored] == ["title contains 'Intern'"] and checked == 2
    assert _one("SELECT count(*) FROM jobs WHERE status='new'")[0] == 0   # dry run
    restore_filtered_jobs(relaxed)
    # The salary filter still applies to the other job.
    assert dict(_rows("SELECT source_job_id, status FROM jobs")) == {"1": "new", "2": "filtered"}
    # Brought back by update-searches, not by you: putting the term back filters it again.
    assert [job["title"] for job in exclude_inbox_jobs([strict])[0]] == ["Intern Analyst"]


def test_a_job_whose_search_was_renamed_is_judged_by_the_defaults(database):
    from jobflow.ingest.refilter import exclude_inbox_jobs
    from jobflow.ingest.searches import defaults_search, parse_config

    text = ('[defaults]\nexclude_title_terms = ["Intern"]\n'
            '[[search]]\nname = "renamed"\nterms = ["a"]\nlocations = { AU = [] }\n')
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "title": "Intern Analyst"}],
             search_name="old-name", country="AU")
    matched, _ = exclude_inbox_jobs(parse_config(text), fallback=defaults_search(text))
    assert [job["reason"] for job in matched] == ["title contains 'Intern'"]


def test_a_renamed_search_brings_back_only_jobs_its_defaults_can_judge(database):
    # [defaults] has no salary minimum: judged by it, a job filtered for its salary would
    # come back to the Inbox though the search that kept it out still would.
    from jobflow.ingest.refilter import restore_filtered_jobs
    from jobflow.ingest.searches import defaults_search, parse_config

    save_run("linkedin:a", "r1", "d1", [LINKEDIN, {**LINKEDIN, "id": "2", "title": "Quant",
                                                   "descriptionText": "Price options."}],
             search_name="old-name", country="AU")
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='filtered', filter_reason=%s WHERE source_job_id='2'",
                     ("salary 60,000-60,000/yr outside 100,000+/yr",))
        conn.execute("UPDATE jobs SET status='filtered', filter_reason=%s WHERE source_job_id<>'2'",
                     ("title contains 'Data'",))
    text = '[[search]]\nname = "renamed"\nterms = ["a"]\nlocations = { AU = [] }\n'
    restored, checked = restore_filtered_jobs(parse_config(text), fallback=defaults_search(text))
    assert [job["title"] for job in restored] == ["Data Analyst"] and checked == 2


def test_one_posting_that_cant_be_saved_does_not_lose_the_rest_of_a_paid_run(database):
    # A JSON "\ud800" escape is valid JSON, and PostgreSQL rejects it.
    broken = {**SEEK, "id": "999", "title": "Analyst \ud800", "jobLink": "https://www.seek.com.au/job/999"}
    # A timestamp out of range once failed the whole run, and was recorded as IMPORT_FAILED.
    far_future = {**LINKEDIN, "id": "998", "link": None, "title": "Quant", "postedAt": 1e20}
    assert save_run("seek:a", "r1", "d1", [broken, LINKEDIN, "not a record", far_future],
                    country="AU") == 2
    assert sorted(_rows("SELECT title, posted_at FROM jobs")) == [("Data Analyst", None),
                                                                  ("Quant", None)]
    # Every posting failing isn't one posting's fault: the import fails, to be retried.
    with pytest.raises(RuntimeError, match="none of the run's 2 postings"):
        save_run("seek:a", "r3", "d3", [broken, "not a record"], country="AU")
    assert _one("SELECT count(*) FROM scraper_runs WHERE apify_run_id='r3'")[0] == 0


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


def test_score_all_scores_only_interested_jobs_but_picked_inbox_jobs_can_be_scored(database,
                                                                                    monkeypatch):
    # Each job is an LLM call per CV: Score all jobs must never score the whole Inbox, but
    # Inbox jobs you tick are scored to help you triage them.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": title}
                                        for key, title in (("1", "Analyst"), ("2", "Quant"),
                                                           ("3", "Trader"))], country="AU")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    with connect() as conn:
        set_job_state(conn, ids["1"], status="interested", via="test")
        set_job_state(conn, ids["3"], status="not_interested", via="test")
    monkeypatch.setattr(matching, "complete", lambda system, user, **kw: (json.dumps(
        {"requirements": [{"requirement": "SQL", "status": "matched", "evidence": ["SQL"]}]}), "m"))
    monkeypatch.setattr(matching, "matching_weights", lambda: (0.75, 0.25))
    monkeypatch.setattr(matching, "chosen_sources", lambda: {"dev": "I write SQL."})
    assert matching.jobs_needing_match(["dev"]) == [ids["1"]]
    assert matching.count_needing_match(["dev"]) == 1
    picked = [ids["2"], ids["3"]]   # an Inbox job, and a Not interested one
    assert matching.jobs_needing_match(["dev"], picked) == [ids["2"]]
    assert [r["variant"] for r in matching.match_job(ids["2"])] == ["dev"]
    with pytest.raises(RuntimeError, match="only Inbox and interested jobs are scored"):
        matching.match_job(ids["3"])
    # Marked Interested later, it keeps its score: Score all jobs has nothing to redo.
    with connect() as conn:
        set_job_state(conn, ids["2"], status="interested", via="test")
    assert matching.jobs_needing_match(["dev"]) == [ids["1"]]


def test_skill_gaps_count_the_jobs_you_are_pursuing(database, monkeypatch):
    # Jobs dropped out of the report the moment you applied for them, though they say most
    # about what you're aiming at.
    from pathlib import Path

    from jobflow.candidate import skill_gaps

    statuses = {"1": "interested", "2": "applied", "3": "offered", "4": "new", "5": "rejected"}
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": f"Role {key}"}
                                        for key in statuses], country="AU")
    ids = dict(_rows("SELECT source_job_id, id FROM jobs"))
    with connect() as conn:
        for key, status in statuses.items():
            conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, ids[key]))
            conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score, gaps,
                                evidence) VALUES (%s, 'dev', 'd', 0, %s, %s)""",
                         (ids[key], json.dumps([{"requirement": f"Skill {key}",
                                                 "importance": "required",
                                                 "status": "not_evidenced"}]),
                          json.dumps({"format": 2, "breakdown": {}})))
    monkeypatch.setattr(skill_gaps, "matching_weights", lambda: (0.75, 0.25))
    monkeypatch.setattr(skill_gaps, "chosen_files", lambda: {"dev": Path("CV_dev.yaml")})
    monkeypatch.setattr(skill_gaps, "current_digests", lambda: {"dev": "d"})
    gaps, scored, outdated = skill_gaps.collect_gaps()
    assert sorted(g["requirement"] for g in gaps) == ["Skill 1", "Skill 2", "Skill 3"]
    assert (scored, outdated) == (3, 0)


def test_a_save_that_fails_part_way_saves_nothing(database):
    # Saves are all or nothing: half a save would leave the page's drafts and the database
    # disagreeing about which jobs changed.
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with pytest.raises(RuntimeError, match="No job"):
        save_changes([{"id": job_id, "status": "interested", "note": "Keen"},
                      {"id": job_id + 1000, "status": "applied"}])
    assert _one("SELECT status FROM jobs")[0] == "new"
    assert _one("SELECT count(*) FROM applications")[0] == 0
    assert _one("SELECT count(*) FROM status_history")[0] == 0


def test_a_second_run_of_a_command_stops_while_the_first_is_going(database, tmp_path,
                                                                 monkeypatch):
    # Two tailor-cv runs at once (in two terminals, say) would draft, and pay for, the
    # same jobs twice and write over each other's files.
    from jobflow import batch
    from jobflow.candidate import paths

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    ran = []

    def run_one(job_id):
        with pytest.raises(SystemExit, match="tailor-cv is already running"):
            batch.for_jobs(command)   # started while this run is going
        ran.append(job_id)
    command = _tailor(run_one, lambda folder: False)
    batch.for_jobs(command)
    assert ran == [job_id]
    batch.for_jobs(command)   # the first run's lock went with it
    assert ran == [job_id, job_id]


def test_a_site_failing_while_others_succeed_is_flagged(database):
    # The header only warned when no import had succeeded at all, so a site failing every
    # day (all of them, once the Apify account hit its monthly limit) went unseen.
    from jobflow.ingest.store import record_failed_run
    from jobflow.web.queries import summary

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    record_failed_run("seek:a", "r2", None, "FAILED", "Monthly usage hard limit exceeded")
    save_run("seek:a", "r3", "d3", [SEEK], country="AU")
    assert summary()["failed_runs"] == [{"site": "seek", "failed": 1, "total": 2,
                                         "error": "Monthly usage hard limit exceeded"}]


def test_a_dry_run_never_changes_the_database(database, tmp_path, monkeypatch):
    # update-searches --dry-run applied pending migrations, one of which dropped columns.
    from jobflow import github, settings

    searches = tmp_path / "searches.toml"
    searches.write_text('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
                        "max_cost_usd = 1\n")
    monkeypatch.setattr(settings, "searches_file", lambda: searches)
    name = migration_files()[-1][0]
    with connect() as conn:   # as if this migration came with a jobflow update
        conn.execute("DELETE FROM schema_migrations WHERE name=%s", (name,))
    with pytest.raises(RuntimeError, match=f"1 database migration not applied yet \\({name}\\)"):
        github.update_searches(dry_run=True)
    assert _one("SELECT count(*) FROM schema_migrations WHERE name=%s", name)[0] == 0
