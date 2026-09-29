import http.client
import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from jobflow.web.queries import InvalidChange, _csv_cell, validate_changes
from jobflow.web.server import make_handler


def test_valid_changes_are_normalised():
    changes = validate_changes([
        {"id": 1, "status": "applied", "note": "Called recruiter", "follow_up_on": "2026-10-02",
         "expected_version": "1790000000000000"},
        {"id": 2, "follow_up_on": ""},          # clearing a date is a change
        {"id": 3, "status": "not_interested", "note": "   "},  # blank note is dropped
    ])
    assert changes == [
        {"id": 1, "status": "applied", "note": "Called recruiter", "follow_up_on": date(2026, 10, 2),
         "expected_version": "1790000000000000"},
        {"id": 2, "follow_up_on": None},
        {"id": 3, "status": "not_interested"},
    ]


@pytest.mark.parametrize("changes, message", [
    ([{"id": 1, "status": "filtered"}], "status must be one of"),  # system-only status
    ([{"id": 1, "status": "applied"}, {"id": 1, "note": "x"}], "repeated"),
    ([{"id": 1, "follow_up_on": "next week"}], "YYYY-MM-DD"),
])
def test_invalid_changes_are_rejected_before_anything_is_written(changes, message):
    with pytest.raises(InvalidChange, match=message):
        validate_changes(changes)


def test_csv_cells_cannot_run_as_spreadsheet_formulas():
    assert _csv_cell('=HYPERLINK("http://evil","x")') == "'=HYPERLINK(\"http://evil\",\"x\")"
    assert _csv_cell("+61 400 000 000") == "'+61 400 000 000"
    assert _csv_cell("Data Analyst") == "Data Analyst" and _csv_cell(None) == ""


@pytest.fixture
def server():
    token = "test-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(token, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(token, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port, token
    httpd.shutdown()
    httpd.server_close()


def _request(port, path, headers, method="GET", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    return response.status, response.read()


def test_api_requires_the_token_and_a_local_host(server):
    port, token = server
    local = f"127.0.0.1:{port}"
    assert _request(port, "/api/summary", {"Host": local})[0] == 403
    assert _request(port, "/api/summary", {"Host": local, "X-Jobflow-Token": "guess"})[0] == 403
    # DNS rebinding: a foreign host name pointing at 127.0.0.1 is refused even with a token.
    assert _request(port, "/api/summary", {"Host": "evil.example", "X-Jobflow-Token": token})[0] == 403
    status, _ = _request(port, "/api/save", {"Host": local, "Content-Type": "application/json"},
                         "POST", json.dumps({"changes": [{"id": 1, "status": "applied"}]}))
    assert status == 403


# --- Scoring in the background ----------------------------------------------------------

@pytest.fixture
def scoring(monkeypatch):
    from jobflow.web import scoring
    monkeypatch.setattr(scoring, "credentials", lambda: ("key", "model"))
    monkeypatch.setattr(scoring, "chosen_sources", lambda: {"dev": "cv: {}"})
    monkeypatch.setattr(scoring, "_state", {**scoring._state, "running": False})
    return scoring


def _wait_until_finished(scoring):
    import time
    for _ in range(200):
        if not scoring.status()["running"]:
            return scoring.status()
        time.sleep(0.01)
    raise AssertionError("scoring did not finish")


def test_a_bad_key_stops_the_batch_instead_of_failing_every_job(scoring, monkeypatch):
    import time

    from jobflow.llm import LLMError
    tried = []

    def match_job(job_id):
        tried.append(job_id)
        if job_id == 1:
            raise LLMError("Invalid authentication credentials (LLM provider, HTTP 401)", fatal=True)
        time.sleep(0.05)
    monkeypatch.setattr(scoring, "match_job", match_job)
    monkeypatch.setattr(scoring, "jobs_needing_match", lambda variants, only=None: [1, 2, 3, 4, 5])
    scoring.start()
    result = _wait_until_finished(scoring)
    # Only job 2 may have started alongside job 1; nothing after it does.
    assert set(tried) <= {1, 2} and result["done"] == len(tried) < result["total"]


def test_similar_jobs_sit_together_but_only_same_country_ones_are_marked_duplicates():
    from jobflow.web.queries import group_duplicates

    def job(pk, duplicates=(), elsewhere=()):
        return {"id": pk, "duplicates": [{"id": d} for d in duplicates],
                "elsewhere": [{"id": e} for e in elsewhere]}
    # Sorted 1, 4, 2, 3: job 3 duplicates job 1 (same country); job 2 is job 1's ad in
    # another country (CPP Investments in Sydney and Hong Kong); job 4 is unrelated.
    jobs = [job(1, duplicates=[3], elsewhere=[2]), job(4), job(2, elsewhere=[1]),
            job(3, duplicates=[1])]
    ordered = group_duplicates(jobs)
    assert [j["id"] for j in ordered] == [1, 3, 2, 4]
    assert {j["id"]: j["group"] for j in ordered} == {1: 1, 3: 1, 2: None, 4: None}
    # Jobs 1, 3, and 2 are also marked as one set spanning countries, in a second colour.
    assert {j["id"]: j["area"] for j in ordered} == {1: 1, 3: 1, 2: 1, 4: None}
