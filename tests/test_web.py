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
    ([], "Nothing to save"),
    ([{"id": 1, "status": "filtered"}], "status must be one of"),  # system-only status
    ([{"id": 1, "status": "hired"}], "status must be one of"),
    ([{"id": 1, "follow_up_on": "next week"}], "YYYY-MM-DD"),
    ([{"id": 1, "status": "applied"}, {"id": 1, "note": "x"}], "repeated"),
    ([{"id": "1", "status": "applied"}], "Invalid"),
    ([{"id": 1, "status": "applied", "salary": 1}], "Unknown field"),
    ([{"id": 1}], "nothing to change"),
    ([{"id": 1, "expected_version": "1790000000000000"}], "nothing to change"),
    ([{"id": 1, "status": "applied", "expected_version": "yesterday"}], "invalid expected_version"),
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


def test_page_carries_this_runs_token(server):
    port, token = server
    status, body = _request(port, "/", {"Host": f"localhost:{port}"})
    assert status == 200 and token.encode() in body and b"__JOBFLOW_TOKEN__" not in body
