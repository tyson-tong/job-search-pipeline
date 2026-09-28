"""Against a real PostgreSQL: merging, saving, and migrating, where a silent mistake loses
or corrupts data. Skipped unless TEST_DATABASE_URL names a disposable database; each run
works in its own schema and drops it afterwards."""
import os
import secrets

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from jobflow.db import connect, migrate, set_job_state
from jobflow.ingest.store import save_run
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


def test_migrations_apply_once_on_an_empty_database(database, capsys):
    migrate()   # already applied by the fixture: nothing runs again
    assert "Applied migration" not in capsys.readouterr().out
    assert _one("SELECT count(*) FROM schema_migrations")[0] == 9


def test_one_job_on_three_sites_is_one_row_with_three_copies(database):
    assert save_run("linkedin:a", "r1", "d1", [LINKEDIN], country="AU") == 1
    assert save_run("indeed:a", "r2", "d2", [INDEED], country="AU") == 0
    assert save_run("seek:a", "r3", "d3", [SEEK], country="AU") == 0   # the NUL didn't fail it
    assert save_run("linkedin:a", "r4", "d4", [LINKEDIN], country="AU") == 0  # seen again
    assert _one("SELECT count(*) FROM jobs")[0] == 1
    assert _one("SELECT count(*) FROM job_sources")[0] == 3
    assert _one("SELECT count(*) FROM job_observations")[0] == 4


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
