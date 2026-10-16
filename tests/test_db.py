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
    # Ads and titles read in another test's schema, whose job ids this one's reuse.
    monkeypatch.setattr(similar, "_AD_WORDS", {})
    monkeypatch.setattr(similar, "_TITLES", similar._TitleIndex())
    try:
        migrate()
        yield
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def scoring(monkeypatch):
    """Scoring with a fake LLM. scoring.cvs: the CVs named, by short name (change it to
    rename or edit one); scoring.requirements(prompt): the requirements the LLM lists for a
    prompt (by default SQL, matched); scoring.weights; scoring.asked: the CV each LLM call
    was for."""
    from types import SimpleNamespace

    from jobflow.candidate import matching

    fake = SimpleNamespace(cvs={"dev": "I write SQL."}, weights=(0.75, 0.25), asked=[],
                           requirements=lambda prompt: [{"requirement": "SQL", "status": "matched",
                                                         "evidence": ["SQL"]}])

    def complete(system, user, **kwargs):
        prompt = json.loads(user)
        fake.asked.append(prompt["cv_variant"])
        return json.dumps({"requirements": fake.requirements(prompt)}), "m"
    monkeypatch.setattr(matching, "complete", complete)
    monkeypatch.setattr(matching, "matching_weights", lambda: fake.weights)
    monkeypatch.setattr(matching, "chosen_sources", lambda: dict(fake.cvs))
    return fake


def _one(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def _rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_migrations_apply_once_on_an_empty_database(database, capsys):
    # A migration applied twice can fail or change existing rows on every command's start.
    migrate()   # already applied by the fixture: nothing runs again
    assert "Applied migration" not in capsys.readouterr().out
    assert _one("SELECT count(*) FROM schema_migrations")[0] == len(migration_files())


def test_one_job_on_three_sites_is_one_row_with_three_copies(database):
    # The same job on LinkedIn, Indeed, and Seek must be one job in the Inbox, with each site's copy
    # kept; a NUL in scraped HTML must not fail the save.
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
    assert save_run("linkedin:a", "r3", "d3", [ad("3", "Melbourne, VIC", payments)],
                    country="AU") == 1
    assert save_run("linkedin:a", "r4", "d4", [ad("4", "Hong Kong", payments)], country="HK") == 1
    # Word for word with no location at all, in two countries: the exact-copy rule too.
    markets = standard + [f"markets{i}" for i in range(20)]
    assert save_run("linkedin:a", "r6", "d6", [ad("6", None, markets)], country="AU") == 1
    assert save_run("linkedin:a", "r7", "d7", [ad("7", None, markets)], country="SG") == 1
    # The check: another site's cut-down copy of the first ad, in the same city, is that job.
    seek = {"id": "5", "jobLink": "https://www.seek.com.au/job/5", "title": "Software Engineer",
            "advertiser": {"name": "Big Bank Limited"},
            "joblocationInfo": {"displayLocation": "Sydney NSW"},
            "content": {"unEditedContent": " ".join(payments[:80] + payments[90:])}}
    assert save_run("seek:a", "r5", "d5", [seek], country="AU") == 0
    assert _one("SELECT count(*) FROM jobs")[0] == 6


def test_a_cpp_and_a_csharp_job_with_one_ad_stay_two_jobs(database):
    # Titles were compared without + and #, so both were "c developer": an employer's one
    # ad for its C++ and C# roles merged, and one of the two jobs never reached the Inbox.
    ad = " ".join(f"duty{i}" for i in range(40))

    def posting(job_id, title, company="Acme"):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": company, "location": "Sydney",
                "descriptionText": ad}
    assert save_run("linkedin:a", "r1", "d1", [posting("1", "C++ Developer")], country="AU") == 1
    assert save_run("linkedin:a", "r2", "d2", [posting("2", "C# Developer")], country="AU") == 1
    # The same ad under the same title is still one job.
    assert save_run("linkedin:a", "r3", "d3", [posting("3", "c++ developer")], country="AU") == 0
    # Under an employer spelt another way (rule 4), each joins the job with its own title.
    assert save_run("linkedin:a", "r4", "d4", [posting("4", "C++ Developer", "Acme Recruitment"),
                                                posting("5", "C# Developer", "Acme Recruitment")],
                    country="AU") == 0
    job_of = dict(_rows("SELECT source_key, job_id FROM job_sources"))
    assert job_of["linkedin:4"] == job_of["linkedin:1"]
    assert job_of["linkedin:1"] != job_of["linkedin:5"] == job_of["linkedin:2"]


def test_a_merged_copy_fills_in_only_what_the_job_lacks(database):
    # Every SET in the merge reads the row as it was: a salary's guessed-period flag must go
    # with the salary kept, and the longest ad must win, or salaries are silently mislabelled
    # (a guessed one would then filter jobs out) and ads lost.
    longer = AD + " Hybrid role."   # still most of the same words: the same short ad
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")   # no salary
    # $8,000 with no period: guessed as a month's pay, 96,000 a year.
    save_run("seek:a", "r2", "d2", [{**SEEK, "salary": "$8,000",
                                     "content": {"unEditedContent": longer}}], country="AU")
    assert _one("SELECT salary_min, salary_estimated, description FROM jobs") == (
        96000, True, longer)
    # A later copy with a stated salary and a shorter ad changes neither.
    save_run("indeed:a", "r3", "d3", [{**INDEED, "salary": "$150,000 per year"}], country="AU")
    assert _one("SELECT count(*), min(salary_min), bool_and(salary_estimated), min(description) "
                "FROM jobs") == (1, 96000, True, longer)


def test_the_job_list_and_the_job_panel_show_the_same_similar_jobs(database):
    # They were worked out by two versions of the rules, which could disagree.
    from jobflow.web.queries import job_detail

    def ad(job_id, title, company, location, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": company, "location": location,
                "descriptionText": " ".join(words)}
    duties = [f"duty{i}" for i in range(60)]
    reposted = duties[:45] + [f"other{i}" for i in range(15)]   # about 60% the same words
    save_run("linkedin:a", "r1", "d1", [ad("1", "Data Analyst", "Acme", "Sydney", duties)],
             country="AU")
    save_run("linkedin:a", "r2", "d2", [ad("2", "Senior Data Analyst", "Hays", "Sydney", reposted)],
             country="AU")
    save_run("linkedin:a", "r3", "d3", [ad("3", "Data Analyst", "Acme", "Hong Kong SAR", duties)],
             country="HK")
    # Only about 60% the same words, abroad: a similar role there, not the same ad.
    save_run("linkedin:a", "r4", "d4", [ad("4", "Data Analyst", "Acme", "Singapore", reposted)],
             country="SG")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    listed = {job["id"]: job for job in list_jobs(parse_filters({"status": ["new"]}))}
    panel = job_detail(ids["1"])
    def found(job, kind):
        return [d["id"] for d in job[kind]]
    assert found(listed[ids["1"]], "duplicates") == found(panel, "duplicates") == [ids["2"]]
    assert found(listed[ids["1"]], "elsewhere") == found(panel, "elsewhere") == [ids["3"]]
    assert (panel["duplicates"][0]["company"], panel["duplicates"][0]["level"]) == (
        "Hays", "similar")


def test_similar_jobs_stay_current_between_refreshes(database):
    # The tracker keeps titles and ads between refreshes (similar._TITLES, _AD_WORDS): a job
    # whose ad grew, or a job added since, must still be compared as it is now.
    first, more = [f"duty{i}" for i in range(30)], [f"extra{i}" for i in range(50)]

    def ad(job_id, title, company, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": company, "location": "Sydney",
                "descriptionText": " ".join(words)}
    save_run("linkedin:a", "r1", "d1", [ad("1", "Data Analyst", "Acme", first)], country="AU")
    save_run("linkedin:a", "r2", "d2", [ad("2", "Data Analyst", "Hays", first + more)],
             country="AU")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))

    def similar_to_first():
        listed = {job["id"]: job for job in list_jobs(parse_filters({"status": ["new"]}))}
        return sorted(d["id"] for d in listed[ids["1"]]["duplicates"])
    assert similar_to_first() == []   # 30 of 80 words alike: under 40%
    # Seek's longer copy of the first ad merges into it, which now has the second's ad.
    seek = {"id": "3", "jobLink": "https://www.seek.com.au/job/3", "title": "Data Analyst",
            "advertiser": {"name": "Acme"}, "joblocationInfo": {"displayLocation": "Sydney NSW"},
            "content": {"unEditedContent": " ".join(first + more)}}
    assert save_run("seek:a", "r3", "d3", [seek], country="AU") == 0
    assert similar_to_first() == [ids["2"]]
    # A job first seen after the last refresh.
    save_run("linkedin:a", "r4", "d4", [ad("4", "Senior Data Analyst", "Robert Walters",
                                         first + more[:40] + [f"own{i}" for i in range(10)])],
             country="AU")
    new = _one("SELECT id FROM jobs WHERE split_part(canonical_key, ':', 2)='4'")[0]
    assert similar_to_first() == sorted([ids["2"], new])


def test_a_cv_named_later_is_scored_without_rescoring_the_others(database, scoring):
    # Scores were never refreshed, so renaming or adding a CV left every scored job with
    # no score against it: tailor-cv then refused them all.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
    cvs = scoring.cvs
    assert [r["variant"] for r in matching.match_job(job_id)] == ["dev"]
    cvs["strat"] = "I write SQL for strategy work."   # a second CV, named later
    assert matching.jobs_needing_match(list(cvs)) == [job_id]
    assert sorted(r["variant"] for r in matching.match_job(job_id)) == ["dev", "strat"]
    assert scoring.asked == ["dev", "strat"]          # dev was not scored again
    assert matching.jobs_needing_match(list(cvs)) == []
    assert matching.count_needing_match(list(cvs)) == 0
    matching.match_job(job_id)
    assert scoring.asked == ["dev", "strat"]


def test_rescoring_redoes_only_the_edited_cv_and_only_when_asked(database, scoring):
    # Scores were never redone, so a skill you added to your CV stayed a gap in skill-gaps.
    # Each rescore is an LLM call per job per CV, so an unedited CV must not be sent again.
    # A job you've applied for is rescored too: skill-gaps reads its score, which was once
    # frozen from the moment you applied.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="applied", via="test")
    scoring.requirements = lambda prompt: [{
        "requirement": "SQL", "evidence": ["write SQL"],
        "status": "matched" if "SQL" in prompt["cv_source"] else "not_evidenced"}]
    scoring.cvs = {"dev": "I build dashboards.", "strat": "I plan strategy."}
    matching.match_job(job_id)
    scoring.cvs["dev"] = "I build dashboards and write SQL."   # the gap, now shown in the CV
    assert matching.outdated_scores(matching.current_digests()) == [(job_id, "dev")]
    matching.match_job(job_id)                        # not asked to rescore: nothing sent
    assert scoring.asked == ["dev", "strat"]
    scores = {r["variant"]: r["score"] for r in matching.match_job(job_id, rescore=True)}
    assert scoring.asked == ["dev", "strat", "dev"] and scores == {"dev": 100, "strat": 0}
    assert matching.outdated_scores(matching.current_digests()) == []


@pytest.mark.parametrize("cvs, failure", [
    # Saving an answer listing no requirements would fix the score at 0 until the CV changes.
    (["dev"], "no requirements"),
    # Saving the one CV that worked would leave the job scored without the other.
    (["dev", "strat"], "timed out"),
])
def test_a_failed_assessment_saves_no_score(database, scoring, cvs, failure):
    # A score saved from a failed or partial assessment ranks the job wrongly and isn't redone.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
    matched = scoring.requirements

    def requirements(prompt):
        if failure == "no requirements":
            return []
        if prompt["cv_variant"] == "strat":
            raise RuntimeError("Read timed out")
        return matched(prompt)
    scoring.requirements = requirements
    scoring.cvs = {cv: "I write SQL." for cv in cvs}
    with pytest.raises(RuntimeError, match=failure):
        matching.match_job(job_id)
    assert _one("SELECT count(*) FROM match_assessments")[0] == 0


def test_the_tracker_and_tailor_cv_rank_cvs_alike_after_the_weights_change(database, monkeypatch,
                                                                           scoring):
    # The tracker showed the score stored at scoring time while tailor-cv recomputed it with
    # the weights now, so after a weights change they could pick different CVs.
    from jobflow.candidate import matching
    from jobflow.web.queries import job_detail

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    with connect() as conn:
        set_job_state(conn, job_id, status="interested", via="test")
        # Scored by an earlier version, in a format that can't be recomputed.
        conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score,
                                                          evidence)
                        VALUES (%s, 'dev', 'old', 90, '{"format": 1}')""", (job_id,))

    def requirements(prompt):
        dev = prompt["cv_variant"] == "dev"
        # dev shows the required skill but not the preferred one; strat the other way round.
        return [{"requirement": "SQL", "importance": "required",
                 "status": "matched" if dev else "not_evidenced", "evidence": ["SQL"]},
                {"requirement": "Tableau", "importance": "preferred",
                 "status": "not_evidenced" if dev else "matched", "evidence": ["Tableau"]}]
    scoring.requirements = requirements
    scoring.cvs = {"dev": "SQL", "strat": "Tableau"}
    # An old-format score once made the job unscorable, so tailor-cv refused it for good.
    assert sorted(job_id for job_id in matching.jobs_needing_match(["dev", "strat"])) == [job_id]
    assert matching.best_variant(matching.match_job(job_id))["variant"] == "dev"
    scoring.weights = (0.2, 0.8)   # now the preferred skill matters most
    # A perfect score against a CV no longer named in cvs isn't the best, either.
    with connect() as conn:
        conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score,
                                                          evidence)
                        VALUES (%s, 'retired', 'x', 100, %s)""", (job_id, json.dumps(
            {"format": 2, "breakdown": {"required": {"count": 1, "weight": 1, "credit": 1.0}}})))
    monkeypatch.setattr(matching, "current_digests", lambda: {"dev": "d", "strat": "s"})
    best = matching.best_variant([r for r in matching.match_job(job_id)
                                  if r["variant"] != "retired"])
    listed = list_jobs(parse_filters({"status": ["interested"]}))[0]
    assert (best["variant"], best["score"]) == (listed["cv_variant"], listed["score"]) == (
        "strat", 80)
    assert [(m["variant"], m["score"]) for m in job_detail(job_id)["matches"]] == [
        ("strat", 80), ("dev", 20)]


def test_a_status_change_keeps_the_first_applied_date_and_every_note(database):
    # The first applied date and your notes are your application history: a later status change must
    # not overwrite them.
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
    # A save that wrote every field would clear a follow-up date or a note you didn't touch.
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
    # Saving a stale page over a change made in another tab would silently undo it.
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
    # Archiving too early hides jobs you haven't decided on; too late, or never, leaves the tabs
    # full of jobs you're done with.
    from datetime import date

    def ad(job_id, title, posted, words):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title, "companyName": f"Employer {job_id}", "postedAt": posted,
                "descriptionText": " ".join(f"{words}{i}" for i in range(40))}
    from jobflow.ingest.filters import TITLE, Filtered
    filtered = lambda posting: Filtered(TITLE, "title contains 'Intern'")
    for key, posted, words in (("1", "2026-09-10", "old"), ("2", "2026-09-18", "recent"),
                               ("9", "2026-08-20", "late")):
        save_run("linkedin:a", f"r{key}", f"d{key}", [ad(key, "Intern", posted, words)],
                 job_filter=filtered)
    save_run("linkedin:a", "r3", "d3", [ad("3", "Analyst", "2026-09-01", "inbox")])
    save_run("linkedin:a", "r5", "d5", [ad("5", "Trader", "2026-09-01", "dropped"),
                                         ad("6", "Quant", "2026-09-01", "fresh"),
                                         ad("7", "Engineer", "2026-08-01", "stale"),
                                         ad("8", "Developer", "2026-08-01", "returned"),
                                         ad("4", "Analyst", "2026-08-01", "pursued")])
    with connect() as conn:
        # Each first seen on its posted date, except job 9: posted on 08-20, first seen on
        # 09-20 (in a month-long search), so it still gets its 2 weeks in Filtered.
        conn.execute("UPDATE jobs SET first_seen_at = posted_at "
                     "WHERE split_part(canonical_key, ':', 2) <> '9'")
        conn.execute("UPDATE jobs SET first_seen_at = '2026-09-20' "
                     "WHERE split_part(canonical_key, ':', 2) = '9'")
        # Marked not interested: job 5 on 09-10, job 6 on 09-20.
        for job_id, marked in (("5", "2026-09-10"), ("6", "2026-09-20")):
            conn.execute("UPDATE jobs SET status='not_interested', status_updated_at=%s "
                         "WHERE split_part(canonical_key, ':', 2)=%s", (marked, job_id))
        # Job 4, which you're pursuing, has been Interested since 08-01.
        conn.execute("UPDATE jobs SET status='interested', status_updated_at='2026-08-01' "
                     "WHERE split_part(canonical_key, ':', 2)='4'")
        # In the Inbox since 08-01 (job 7), and moved back to it on 09-20 (job 8).
        for job_id, since in (("7", "2026-08-01"), ("8", "2026-09-20")):
            conn.execute("UPDATE jobs SET status_updated_at=%s "
                         "WHERE split_part(canonical_key, ':', 2)=%s",
                         (since, job_id))
    # 2026-09-28: job 1 is 18 days past posting; job 2 is 10; job 5 was marked 18 days ago,
    # job 6 8 days ago, though both were posted weeks ago. Inbox jobs wait 30 days: job 3
    # was posted 27 days ago, job 7 58 days ago and never triaged; job 8 is as old, but
    # was moved back to the Inbox 8 days ago. Job 4 is as old, but never archived: a job
    # you're pursuing would vanish from every tab.
    assert archive_old_jobs(date(2026, 9, 28)) == 3
    with connect() as conn:
        statuses = dict(conn.execute(
            "SELECT split_part(canonical_key, ':', 2), status FROM jobs").fetchall())
    assert statuses == {"1": "archived", "2": "filtered", "3": "new", "4": "interested",
                        "5": "archived",
                        "6": "not_interested", "7": "archived", "8": "new", "9": "filtered"}
    assert sorted(_rows("SELECT from_status, via FROM status_history")) == [
        ("filtered", "archive"), ("new", "archive"), ("not_interested", "archive")]
    # Still a known job: seeing it again adds no new one, and it stays archived.
    assert save_run("linkedin:a", "r4", "d4", [ad("1", "Intern", "2026-09-10", "old")]) == 0
    assert _one("SELECT status FROM jobs "
                "WHERE split_part(canonical_key, ':', 2)='1'")[0] == "archived"


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
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
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
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='interested' "
                     "WHERE split_part(canonical_key, ':', 2) <> '3'")
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    ran = []
    tailor = _tailor(ran.append, lambda folder: False)
    batch._run_jobs(tailor, only=[ids["2"]])
    assert ran == [ids["2"]]
    with pytest.raises(RuntimeError, match=f"#{ids['3']} is new"):
        batch._run_jobs(tailor, only=[ids["3"]])
    assert ran == [ids["2"]]
    # A letter for a job you've applied for: named with --job, but never in the batch.
    with connect() as conn:
        conn.execute("UPDATE jobs SET status='applied' WHERE split_part(canonical_key, ':', 2)='3'")
    pursuing = _tailor(ran.append, lambda folder: False, job_statuses=("interested", "applied"))
    batch._run_jobs(pursuing)
    assert sorted(ran[1:]) == sorted([ids[LINKEDIN["id"]], ids["2"]])
    batch._run_jobs(pursuing, only=[ids["3"]])
    assert ran[-1] == ids["3"]


def test_update_searches_filters_the_inbox_but_not_a_job_you_moved_back(database):
    # A job you moved back from Filtered or Archived is one you chose to keep: filtering it
    # again would hide it from you.
    from jobflow.ingest.refilter import filter_inbox_jobs
    from jobflow.ingest.searches import parse_config

    # Every filter applies, not only the exclusions: raising a salary minimum once left the
    # Inbox full of jobs below it. A posted-date window judges a job as on its import day.
    search = parse_config('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
                          'exclude_title_terms = ["Intern"]\nsalary_min = 100000\n'
                          'posted_within_days = 14\n')[0]

    def ad(job_id, title, **more):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title,
                "companyName": f"Employer {job_id}", "descriptionText": f"ad {job_id}", **more}
    save_run("linkedin:a", "r1", "d1", [ad("1", "Intern Analyst"), ad("2", "Intern Trader"),
                                         ad("3", "Analyst", postedAt="2026-01-01"),
                                         ad("4", "Quant", salaryInfo=["$60,000/yr"]),
                                         ad("5", "Intern Quant")],
             search_name="s", country="AU")
    with connect() as conn:   # job 3 was posted the day before it was imported, long ago
        conn.execute("UPDATE jobs SET first_seen_at='2026-01-02' "
                     "WHERE split_part(canonical_key, ':', 2)='3'")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:   # filtered earlier, then moved back to the Inbox by hand
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=%s", (ids["2"],))
        set_job_state(conn, ids["2"], status="new", via="web")
        # Archived while untriaged, then brought back from the Archived tab.
        conn.execute("UPDATE jobs SET status='archived' WHERE id=%s", (ids["5"],))
        set_job_state(conn, ids["5"], status="new", via="web")
    assert [job["id"] for job in filter_inbox_jobs([search], dry_run=True)[0]] == [
        ids["1"], ids["4"]]
    assert _one("SELECT count(*) FROM jobs WHERE status='filtered'")[0] == 0   # dry run
    filter_inbox_jobs([search])
    assert dict(_rows("SELECT split_part(canonical_key, ':', 2), status FROM jobs")) == {
        "1": "filtered", "2": "new", "3": "new", "4": "filtered", "5": "new"}


def test_relaxing_a_filter_brings_back_only_the_jobs_it_kept_out(database):
    # Dropping an excluded term used to leave the jobs it had filtered hidden until archived.
    from functools import partial

    from jobflow.ingest.filters import filter_reason
    from jobflow.ingest.refilter import filter_inbox_jobs, restore_filtered_jobs
    from jobflow.ingest.searches import parse_config

    base = ('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
            'salary_min = 100000\n')
    strict = parse_config(base + 'exclude_title_terms = ["Intern"]\n')[0]

    def ad(job_id, title, salary=None):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title,
                "companyName": f"Employer {job_id}", "descriptionText": f"ad {job_id}",
                **({"salaryInfo": salary} if salary else {})}
    save_run("linkedin:a", "r1", "d1",
             [ad("1", "Intern Analyst"), ad("2", "Analyst", ["$60,000/yr"])],
             search_name="s", country="AU",
             job_filter=partial(filter_reason, search=strict, site="linkedin"))
    assert dict(_rows("SELECT split_part(canonical_key, ':', 2), status FROM jobs")) == {
        "1": "filtered", "2": "filtered"}
    relaxed = parse_config(base)
    restored, checked = restore_filtered_jobs(relaxed, dry_run=True)
    assert [job["reason"] for job in restored] == ["title contains 'Intern'"] and checked == 2
    assert _one("SELECT count(*) FROM jobs WHERE status='new'")[0] == 0   # dry run
    restore_filtered_jobs(relaxed)
    # The salary filter still applies to the other job.
    assert dict(_rows("SELECT split_part(canonical_key, ':', 2), status FROM jobs")) == {
        "1": "new", "2": "filtered"}
    # Brought back by update-searches, not by you: putting the term back filters it again.
    assert [job["title"] for job in filter_inbox_jobs([strict])[0]] == ["Intern Analyst"]


def test_a_job_whose_search_was_renamed_is_judged_by_the_defaults(database):
    # A job whose search was renamed or removed is judged by [defaults]; skipped, a job a [defaults]
    # exclusion covers would stay in the Inbox.
    from jobflow.ingest.refilter import filter_inbox_jobs
    from jobflow.ingest.searches import defaults_search, parse_config

    text = ('[defaults]\nexclude_title_terms = ["Intern"]\n'
            '[[search]]\nname = "renamed"\nterms = ["a"]\nlocations = { AU = [] }\n')
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "title": "Intern Analyst"}],
             search_name="old-name", country="AU")
    matched, _ = filter_inbox_jobs(parse_config(text), fallback=defaults_search(text))
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
        conn.execute("UPDATE jobs SET status='filtered', filter_reason=%s, "
                     "filter_kind='salary' WHERE split_part(canonical_key, ':', 2)='2'",
                     ("salary 60,000-60,000/yr outside 100,000+/yr",))
        conn.execute("UPDATE jobs SET status='filtered', filter_reason=%s, "
                     "filter_kind='title' WHERE split_part(canonical_key, ':', 2)<>'2'",
                     ("title contains 'Data'",))
    text = '[[search]]\nname = "renamed"\nterms = ["a"]\nlocations = { AU = [] }\n'
    restored, checked = restore_filtered_jobs(parse_config(text), fallback=defaults_search(text))
    assert [job["title"] for job in restored] == ["Data Analyst"] and checked == 2


def test_a_job_filtered_for_its_posted_date_stays_filtered(database):
    # ingest --posted-within-days 14 makes LinkedIn search a month and filter older jobs
    # itself; the next update-searches judged them by the search's own 1-day window, which
    # LinkedIn applies natively (so no posted-date filter), and brought them back to the Inbox.
    from dataclasses import replace
    from datetime import date
    from functools import partial

    from jobflow.ingest.filters import filter_reason
    from jobflow.ingest.refilter import restore_filtered_jobs
    from jobflow.ingest.searches import parse_config

    search = parse_config('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
                          'sites = ["linkedin"]\n')[0]
    one_off = replace(search, posted_within_days=14)
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "postedAt": "2026-09-01"}],
             search_name="s", country="AU",
             job_filter=partial(filter_reason, search=one_off, site="linkedin",
                                today=date(2026, 9, 26)))
    with connect() as conn:
        conn.execute("UPDATE jobs SET first_seen_at = '2026-09-26'")
    assert _one("SELECT status FROM jobs")[0] == "filtered"
    assert restore_filtered_jobs([search]) == ([], 1)
    assert _one("SELECT status FROM jobs")[0] == "filtered"


def test_jobs_filtered_before_filter_kind_get_the_kind_their_reason_says(database):
    # update-searches decides which filtered jobs it may bring back by filter_kind: a job
    # filtered for its posted date, given no kind or the wrong one, could come back to the
    # Inbox, and one filtered by an excluded term, given none, never could.
    reasons = {"1": "employer matches 'Acme'", "2": "title contains 'Intern'",
               "3": "posted 2026-09-01, older than 2 weeks",
               "4": "salary 60,000-60,000/yr outside 100,000+/yr",
               "5": "job type part-time not wanted", "6": "work arrangement on-site not wanted"}
    with connect() as conn:   # as jobflow stored them before filter_kind existed
        for key, reason in reasons.items():
            conn.execute("INSERT INTO jobs(canonical_key, title, status, filter_reason) "
                         "VALUES (%s, 'Job', 'filtered', %s)", (f"linkedin:{key}", reason))
        conn.execute("DELETE FROM schema_migrations WHERE name = '002_jobs.sql:10'")
    migrate()
    assert dict(_rows("SELECT split_part(canonical_key, ':', 2), filter_kind FROM jobs")) == {
        "1": "employer", "2": "title", "3": "posted", "4": "salary", "5": "job_type",
        "6": "work_arrangement"}


def test_every_status_can_be_saved_and_has_a_label(database):
    # A status added to db.STATUSES without a migration replacing jobs_status_check fails
    # every save of it; without a label in format.js the tracker shows its raw name.
    import re
    from pathlib import Path

    from jobflow.db import STATUSES

    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    job_id = _one("SELECT id FROM jobs")[0]
    for status in STATUSES:
        with connect() as conn:
            set_job_state(conn, job_id, status=status, via="test")
        assert _one("SELECT status FROM jobs")[0] == status
    script = (Path(__file__).parents[1] / "src/jobflow/web/static/format.js").read_text()
    labels = re.search(r"LABELS = \{(.*?)\};", script, re.S).group(1)
    assert set(re.findall(r"(\w+):", labels)) == set(STATUSES)


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
    # Pruning old sightings frees space: it must not lose the job's own record, or stop a later
    # sighting merging into the job.
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    save_run("indeed:a", "r2", "d2", [INDEED], country="AU")
    with connect() as conn:   # the LinkedIn sighting is 31 days old
        conn.execute("""UPDATE job_observations SET observed_at = now() - interval '31 days'
                        WHERE scraper_run_id = (SELECT id FROM scraper_runs
                                                WHERE apify_run_id='r1')""")
    assert prune_sightings() == 1
    assert _rows("SELECT observed_json IS NULL FROM job_observations ORDER BY observed_at") == [
        (True,), (False,)]
    # The job keeps its record.
    assert _one("SELECT raw_json->>'id' FROM jobs")[0] == LINKEDIN["id"]
    # A later sighting still merges by the site's ID.
    assert save_run("linkedin:a", "r3", "d3", [LINKEDIN], country="AU") == 0


def test_jobs_archived_over_90_days_ago_lose_their_ad_but_are_still_recognised(database):
    # A cleared job that no longer matched would return to the Inbox as new each time it was
    # reposted.
    save_run("linkedin:a", "r1", "d1", [LINKEDIN, {**SEEK, "title": "Quant"}], country="AU")
    with connect() as conn:
        for key, days in ((LINKEDIN["id"], 91), (SEEK["id"], 89)):
            conn.execute("UPDATE jobs SET status='archived', "
                         "status_updated_at = now() - make_interval(days => %s) "
                         "WHERE split_part(canonical_key, ':', 2)=%s", (days, key))
    assert prune_archived_jobs() == 1
    assert sorted(_rows("SELECT split_part(canonical_key, ':', 2), description IS NULL, "
                        "raw_json IS NULL FROM jobs")) == [
        (LINKEDIN["id"], True, True), (SEEK["id"], False, False)]
    # Seen again, the cleared job is still the same job, and stays archived.
    assert save_run("linkedin:a", "r2", "d2", [LINKEDIN], country="AU") == 0
    assert _one("SELECT status FROM jobs WHERE split_part(canonical_key, ':', 2)=%s",
                LINKEDIN["id"])[0] == "archived"


def test_dates_are_in_your_timezone_in_python_and_the_database(database, monkeypatch):
    # The database's own dates were UTC: a day behind Sydney every morning, so the Posted
    # column, the posted-within filter, and archiving disagreed with today's date.
    from jobflow.settings import today

    monkeypatch.setenv("JOBFLOW_TIMEZONE", "Pacific/Kiritimati")   # UTC+14
    assert _one("SELECT current_setting('TimeZone'), now()::date") == (
        "Pacific/Kiritimati", today())


def test_score_all_scores_the_jobs_you_are_pursuing_and_picked_inbox_jobs(database, scoring):
    # Each job is an LLM call per CV: Score all jobs must never score the whole Inbox, but
    # Inbox jobs you tick are scored to help you triage them. A job you've applied for is
    # scored too: one against a CV added later was never scored, so skill-gaps kept judging
    # it by the old CV alone.
    from jobflow.candidate import matching

    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": title}
                                        for key, title in (("1", "Analyst"), ("2", "Quant"),
                                                           ("3", "Trader"), ("4", "Engineer"))],
             country="AU")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:
        set_job_state(conn, ids["1"], status="interested", via="test")
        set_job_state(conn, ids["3"], status="not_interested", via="test")
        set_job_state(conn, ids["4"], status="applied", via="test")
    pursued = sorted([ids["1"], ids["4"]])
    assert sorted(matching.jobs_needing_match(["dev"])) == pursued
    assert matching.count_needing_match(["dev"]) == 2
    picked = [ids["2"], ids["3"]]   # an Inbox job, and a Not interested one
    assert matching.jobs_needing_match(["dev"], picked) == [ids["2"]]
    assert [r["variant"] for r in matching.match_job(ids["2"])] == ["dev"]
    with pytest.raises(RuntimeError, match="only Inbox jobs and those you're pursuing"):
        matching.match_job(ids["3"])
    # Marked Interested later, it keeps its score: Score all jobs has nothing to redo.
    with connect() as conn:
        set_job_state(conn, ids["2"], status="interested", via="test")
    assert sorted(matching.jobs_needing_match(["dev"])) == pursued


def test_skill_gaps_count_the_jobs_you_are_pursuing(database, monkeypatch):
    # Jobs dropped out of the report the moment you applied for them, though they say most
    # about what you're aiming at.
    from pathlib import Path

    from jobflow.candidate import skill_gaps

    statuses = {"1": "interested", "2": "applied", "3": "offered", "4": "new", "5": "rejected"}
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": f"Role {key}"}
                                        for key in statuses], country="AU")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:
        for key, status in statuses.items():
            conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, ids[key]))
            conn.execute("""INSERT INTO match_assessments(job_id, cv_variant, cv_sha256, score,
                                gaps, evidence) VALUES (%s, 'dev', 'd', 0, %s, %s)""",
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
        with pytest.raises(RuntimeError, match="tailor-cv is already running"):
            batch.for_jobs(command)   # started while this run is going
        ran.append(job_id)
    command = _tailor(run_one, lambda folder: False)
    batch.for_jobs(command)
    assert ran == [job_id]
    batch.for_jobs(command)   # the first run's lock went with it
    assert ran == [job_id, job_id]


def test_a_failed_import_is_imported_again_as_it_would_have_been(database, monkeypatch, capsys):
    # A run whose results couldn't be saved is paid for: its results, still on Apify, were
    # lost, with no way to import them.
    from datetime import UTC, datetime

    from apify_client.errors import ApifyApiError

    from jobflow.ingest import apify
    from jobflow.ingest.store import record_failed_run

    class Gone(ApifyApiError):   # what Apify's client raises for a deleted dataset
        def __new__(cls):
            return Exception.__new__(cls)

        def __init__(self):
            Exception.__init__(self, "Dataset was not found")
            self.status_code = 404

    class Client:
        def dataset(self, dataset_id):
            class Dataset:
                def iterate_items(self, clean):
                    if dataset_id == "d2":
                        raise Gone()
                    return iter([LINKEDIN, {**LINKEDIN, "id": "2", "title": "Intern Analyst"}])
            return Dataset()
    monkeypatch.setattr(apify, "make_client", Client)
    monkeypatch.setenv("JOBFLOW_SEARCHES", '[[search]]\nname = "s"\nterms = ["a"]\n'
                       'locations = { AU = [] }\nexclude_title_terms = ["Intern"]\n')
    ran = datetime(2026, 10, 1, tzinfo=UTC)
    run_input = {"urls": ["https://www.linkedin.com/jobs/search/?location=Australia"],
                 "limitPerSource": 50}
    record_failed_run("linkedin:a", "r1", "d1", "IMPORT_FAILED", "server closed the connection",
                      ran, ran, 0.5, "s", run_input)
    # Its results deleted by Apify since: recorded as such, and not tried again.
    record_failed_run("seek:a", "r2", "d2", "IMPORT_FAILED", "server closed the connection",
                      ran, ran, 0.25, "s", {"country": "australia", "maxResults": 50})
    with pytest.raises(RuntimeError, match="1 of 2 failed imports couldn't be imported"):
        apify.retry_imports()
    # Saved as the import would have: filtered by its search, in its country.
    assert sorted(_rows("SELECT title, status, country FROM jobs")) == [
        ("Data Analyst", "new", "AU"), ("Intern Analyst", "filtered", "AU")]
    assert sorted(_rows("SELECT apify_run_id, status, started_at, cost_usd FROM scraper_runs")) \
        == [("r1", "SUCCEEDED", ran, 0.5), ("r2", "EXPIRED", ran, 0.25)]
    capsys.readouterr()
    apify.retry_imports()
    assert capsys.readouterr().out == "No failed imports to retry\n"


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
    # A run that returned all max_results allows probably left jobs behind; unflagged,
    # they're lost unseen.
    save_run("indeed:a", "r4", "d4", [INDEED], country="AU", search_name="s", hit_cap=True)
    assert summary()["capped_runs"] == [{"site": "indeed", "capped": 1, "total": 1,
                                         "searches": ["s"]}]


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


def test_a_job_from_before_job_sources_is_still_recognised_by_its_site_id(database):
    # job_sources was filled in from the sightings still holding their record, so an older
    # job could have no row there; seen again with an edited ad, its insert broke the
    # unique key and the posting was skipped at every import.
    save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU")
    with connect() as conn:
        conn.execute("DELETE FROM job_sources")
    edited = {**LINKEDIN, "descriptionText": "An entirely rewritten advert for this role."}
    assert save_run("linkedin:a", "r2", "d2", [edited], country="AU") == 0
    assert _one("SELECT count(*) FROM jobs")[0] == 1
    assert _one("SELECT count(*) FROM job_observations")[0] == 2


def test_the_job_list_filters_hide_only_what_they_say(database):
    # Each filter is SQL no other test runs; a mistake would silently hide jobs.
    from datetime import timedelta

    from jobflow.settings import today
    recently = (today() - timedelta(days=2)).isoformat()
    long_ago = (today() - timedelta(days=60)).isoformat()
    def ad(job_id, title, location, **more):
        return {"id": job_id, "link": f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": title,
                "companyName": f"Employer {job_id}", "location": location,
                "descriptionText": " ".join(f"{title}{i}" for i in range(40)), **more}
    save_run("linkedin:a", "r1", "d1", [ad("1", "Analyst", "Sydney", salaryInfo=["$150,000/yr"],
                                           postedAt=recently)], country="AU")
    save_run("linkedin:a", "r2", "d2", [ad("2", "Trader", "Hong Kong", salaryInfo=["HK$600,000/yr"],
                                           postedAt=long_ago)], country="HK")
    save_run("linkedin:a", "r3", "d3", [ad("3", "Quant", "Remote")])   # no country, salary, or date
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:
        conn.execute("UPDATE jobs SET first_seen_at = %s "
                     "WHERE split_part(canonical_key, ':', 2) = '3'", (long_ago,))

    def shown(**params):
        query = {"status": ["new"], **{k: [str(v)] if not isinstance(v, list) else v
                                       for k, v in params.items()}}
        return sorted(key for key, pk in ids.items()
                      if pk in {job["id"] for job in list_jobs(parse_filters(query))})
    assert shown() == ["1", "2", "3"]
    # A minimum in USD just under and just over each salary converted at USD_PER_UNIT (worked
    # out here, so updating the rates doesn't break this); a job without a salary or a known
    # currency is always kept.
    from jobflow.ingest.options import USD_PER_UNIT
    in_usd = {"1": 150000 * USD_PER_UNIT["AUD"], "2": 600000 * USD_PER_UNIT["HKD"]}
    for usd in in_usd.values():
        for minimum in (int(usd - 1), int(usd + 1)):
            assert shown(salary_min_usd=minimum) == sorted(
                ["3"] + [key for key, salary in in_usd.items() if salary >= minimum])
    assert shown(country=["HK"]) == ["2"] and shown(country=["none"]) == ["3"]
    assert shown(site=["indeed"]) == []
    # Posted in the past week, or over a month ago, by the posted date, else the date first seen.
    assert shown(posted=7) == ["1"] and shown(posted="older") == ["2", "3"]


def test_clean_up_keeps_the_folder_of_every_job_you_applied_for(database, tmp_path, monkeypatch):
    # Its folder holds the CV and letter you sent, wanted for a follow-up or a new
    # application; clean-up once deleted it as soon as the job was Rejected, and a job
    # rejected straight from Interested (never marked Applied) never counted as applied.
    from jobflow import cleanup, settings
    from jobflow.candidate import paths

    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "id": key, "title": title}
                                        for key, title in (("1", "Analyst"), ("2", "Quant"),
                                                           ("3", "Trader"), ("4", "Engineer"))],
             country="AU")
    ids = dict(_rows("SELECT split_part(canonical_key, ':', 2), id FROM jobs"))
    with connect() as conn:
        # Applied, then rejected; applied, then withdrawn; withdrawn before applying;
        # rejected without ever being marked Applied, then withdrawn: rejected, so you
        # applied.
        for key, statuses in (("1", ["interested", "applied", "rejected"]),
                              ("2", ["interested", "applied", "withdrawn"]),
                              ("3", ["interested", "withdrawn"]),
                              ("4", ["interested", "rejected", "withdrawn"])):
            for status in statuses:
                set_job_state(conn, ids[key], status=status, via="test")
    monkeypatch.setattr(settings, "output_directory", lambda: tmp_path)
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    folders = {}
    for key, title in (("1", "Analyst"), ("2", "Quant"), ("3", "Trader"), ("4", "Engineer")):
        folders[key] = paths.job_directory(ids[key], {"company": "Acme", "title": title})
        folders[key].mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cleanup.clean_up()
    assert {key: folder.exists() for key, folder in folders.items()} == {
        "1": True, "2": True, "3": False, "4": True}


def test_export_all_has_every_job_with_the_chosen_statuses(database):
    # The docs suggest Export CSV (All) for a copy of your jobs: it must not stop at the
    # 2,000 rows the job list shows.
    import csv
    import io

    from jobflow.web.export import export_csv
    from jobflow.web.queries import MAX_ROWS

    with connect() as conn:
        conn.execute("""INSERT INTO jobs(canonical_key, title, status)
                        SELECT 'k' || n, 'Job ' || n, CASE WHEN n = 1 THEN 'rejected'
                                                          ELSE 'interested' END
                        FROM generate_series(1, %s) AS n""", (MAX_ROWS + 5,))
    rows = list(csv.reader(io.StringIO(export_csv(parse_filters(
        {"status": ["interested", "rejected"]})))))
    assert len(rows) - 1 == MAX_ROWS + 5


def test_update_searches_updates_your_jobs_even_when_github_cant_be(database, tmp_path,
                                                                    monkeypatch):
    # Filtering the Inbox ran only after the GitHub variables were set, so without the
    # GitHub CLI (or with it logged out) your jobs were never brought in line.
    from jobflow import github, settings

    searches = tmp_path / "searches.toml"
    searches.write_text('[[search]]\nname = "s"\nterms = ["a"]\nlocations = { AU = [] }\n'
                        'max_cost_usd = 1\nexclude_title_terms = ["Intern"]\n')
    monkeypatch.setattr(settings, "searches_file", lambda: searches)
    save_run("linkedin:a", "r1", "d1", [{**LINKEDIN, "title": "Intern Analyst"}],
             search_name="s", country="AU")
    # conftest's no_github makes every gh command fail.
    with pytest.raises(RuntimeError, match="GitHub variables weren't updated"):
        github.update_searches()
    assert _one("SELECT status FROM jobs")[0] == "filtered"
