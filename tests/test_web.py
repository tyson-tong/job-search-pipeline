import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from jobflow.web import scoring
from jobflow.web.export import _csv_cell
from jobflow.web.server import make_handler


def test_csv_cells_cannot_run_as_spreadsheet_formulas():
    # A scraped title starting with = runs as a formula when the export is opened in a spreadsheet.
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
    # The tracker changes your jobs: any web page you visit could call it on 127.0.0.1 without the
    # token and Host checks.
    port, token = server
    local = f"127.0.0.1:{port}"
    assert _request(port, "/api/summary", {"Host": local})[0] == 403
    assert _request(port, "/api/summary", {"Host": local, "X-Jobflow-Token": "guess"})[0] == 403
    # Only in a header: a token in a URL stays in the browser's history.
    assert _request(port, f"/api/summary?token={token}", {"Host": local})[0] == 403
    # DNS rebinding: a foreign host name pointing at 127.0.0.1 is refused even with a token.
    foreign = {"Host": "evil.example", "X-Jobflow-Token": token}
    assert _request(port, "/api/summary", foreign)[0] == 403
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



def test_a_failed_scoring_run_never_leaves_scoring_stuck(monkeypatch):
    # The running flag once stayed set after a failure, so every later Score click said
    # "Scoring is already running" until the tracker was restarted.
    import time

    monkeypatch.setattr(scoring, "_state", dict(scoring._state, running=False))
    monkeypatch.setattr(scoring, "credentials", lambda: ("key", "model"))
    monkeypatch.setattr(scoring, "chosen_sources", lambda: {"dev": "cv"})

    def database_down(variants, job_ids):
        raise RuntimeError("database down")
    monkeypatch.setattr(scoring, "jobs_needing_match", database_down)
    with pytest.raises(RuntimeError, match="database down"):
        scoring.start()
    assert not scoring.status()["running"]
    # A job failing in the background thread: the run still ends.
    monkeypatch.setattr(scoring, "jobs_needing_match", lambda variants, job_ids: [1])

    def fail(job_id, rescore=False):
        raise RuntimeError("LLM timed out")
    monkeypatch.setattr(scoring, "match_job", fail)
    scoring.start()
    deadline = time.monotonic() + 5
    while scoring.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    state = scoring.status()
    assert not state["running"] and state["errors"] == [{"id": 1, "error": "LLM timed out"}]


def test_changing_a_status_back_leaves_no_unsaved_change():
    # Statuses were compared cut to a date's 10 characters, so "not_interested" and
    # "interviewing" never matched themselves: undoing a click left an unsaved change, which
    # stopped the page refreshing and asked before you left it.
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node to run the page's JavaScript")
    from jobflow.db import STATUSES

    module = Path(__file__).parents[1] / "src/jobflow/web/static/format.js"
    script = (f"import {{ isSaved }} from {json.dumps(module.as_uri())};\n"
              f"const statuses = {json.dumps(list(STATUSES))};\n"
              "console.log(JSON.stringify({\n"
              "  back: statuses.filter((s) => !isSaved('status', s, s)),\n"
              "  changed: isSaved('status', 'offered', 'interviewing'),\n"
              "  date: isSaved('follow_up_on', '2026-10-09', '2026-10-09'),\n"
              "  cleared: isSaved('follow_up_on', '', null),\n"
              "  note: isSaved('note', 'Called them', null)}));\n")
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"back": [], "changed": False, "date": True,
                                         "cleared": True, "note": False}
