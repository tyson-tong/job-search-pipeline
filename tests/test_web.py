import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from jobflow.web.export import _csv_cell
from jobflow.web.saving import InvalidChange, validate_changes
from jobflow.web.server import make_handler


def test_a_system_only_status_is_rejected_before_anything_is_written():
    # Only jobflow sets filtered or archived; a job set so by hand would vanish from the tabs.
    with pytest.raises(InvalidChange, match="status must be one of"):
        validate_changes([{"id": 1, "status": "filtered"}])


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
    # Only in a header: a token in a URL stays in the browser's history.
    assert _request(port, f"/api/summary?token={token}", {"Host": local})[0] == 403
    # DNS rebinding: a foreign host name pointing at 127.0.0.1 is refused even with a token.
    assert _request(port, "/api/summary", {"Host": "evil.example", "X-Jobflow-Token": token})[0] == 403
    status, _ = _request(port, "/api/save", {"Host": local, "Content-Type": "application/json"},
                         "POST", json.dumps({"changes": [{"id": 1, "status": "applied"}]}))
    assert status == 403
    # The page runs only its own scripts and can't be framed by another site.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/", headers={"Host": local})
    response = conn.getresponse()
    assert response.status == 200 and response.getheader("X-Frame-Options") == "DENY"
    assert "script-src 'self'" in response.getheader("Content-Security-Policy")
    assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")

