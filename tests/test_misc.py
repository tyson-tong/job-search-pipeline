import pytest
from jobflow.db import migration_files
from jobflow.candidate.interview import _safe_markup


def test_migrations_ship_inside_the_package():
    # A non-editable install (as in GitHub Actions) must still find them.
    names = [name for name, _ in migration_files()]
    assert names and names[0] == "001_scraper_runs.sql:01" and names == sorted(names)


def test_briefing_markup_cannot_inject_html():
    out = _safe_markup('<script>alert(1)</script>\n'
                       '[click](javascript:alert(1)) [x](https://ok.example/" onmouseover="bad)')
    assert "<script>" not in out
    assert 'href="javascript' not in out and 'onmouseover="bad' not in out


def test_env_file_never_overrides_real_environment(monkeypatch, tmp_path):
    import os

    from jobflow.settings import load_env

    env = tmp_path / ".env"
    env.write_text("# comment\n"
                   "export LLM_MODEL='gpt-x'\n"
                   'LLM_API_KEY="k#1"\n'
                   "EXAMPLE_IDS=a,b # trailing comment\n"
                   "LLM_BASE_URL=\n"
                   "APIFY_TOKEN=from-file\n")
    # A throwaway copy, so nothing load_env sets leaks into later tests.
    environ = {k: v for k, v in os.environ.items()
               if k not in ("LLM_MODEL", "LLM_API_KEY", "EXAMPLE_IDS", "LLM_BASE_URL")}
    environ["APIFY_TOKEN"] = "from-github-secret"
    monkeypatch.setattr(os, "environ", environ)
    load_env(env)
    assert os.environ["LLM_MODEL"] == "gpt-x"
    assert os.environ["LLM_API_KEY"] == "k#1"
    assert os.environ["EXAMPLE_IDS"] == "a,b"
    assert "LLM_BASE_URL" not in os.environ  # empty values don't mask defaults
    assert os.environ["APIFY_TOKEN"] == "from-github-secret"


def test_init_never_overwrites_personal_files_and_keeps_env_private(tmp_path):
    import stat

    from jobflow.settings import init_files

    config = tmp_path / "config"
    config.mkdir()
    for template in (".env.example", "profile.example.toml", "searches.example.toml"):
        (config / template).write_text(f"# template {template}\n")
    (config / ".env").write_text("DATABASE_URL=my-real-secret\n")  # already set up
    results = dict(init_files(tmp_path))
    assert results == {".env": False, "profile.toml": True, "searches.toml": True}
    assert (config / ".env").read_text() == "DATABASE_URL=my-real-secret\n"
    assert (config / "searches.toml").read_text() == "# template searches.example.toml\n"
    (config / ".env").unlink()
    init_files(tmp_path)
    assert stat.S_IMODE((config / ".env").stat().st_mode) == 0o600


def test_update_searches_never_publishes_an_invalid_file(tmp_path, monkeypatch):
    # The daily import reads the published copy, so a typo must stop before gh runs.
    import pytest

    from jobflow import cli, settings
    from jobflow.ingest.searches import SearchConfigError

    broken = tmp_path / "searches.toml"
    broken.write_text('[[search]]\nname = "x"\nterms = ["quant"]\nlocations = { AU = ["Syndey"] }\n')
    monkeypatch.setattr(settings, "searches_file", lambda: broken)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("published anyway"))
    with pytest.raises(SearchConfigError, match="unknown AU city"):
        cli.update_searches()


def test_reviewer_llm_settings_fall_back_but_never_leak_the_main_key(monkeypatch):
    from jobflow.llm import base_url, credentials

    for name in ("LLM_REVIEW_MODEL", "LLM_REVIEW_BASE_URL", "LLM_REVIEW_API_KEY",
                 "LLM_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    assert credentials(reviewer=True) == ("main-key", "main-model")
    monkeypatch.setenv("LLM_REVIEW_MODEL", "review-model")
    assert credentials(reviewer=True) == ("main-key", "review-model")
    assert credentials() == ("main-key", "main-model")
    # Another provider without its own key must not receive the main key.
    monkeypatch.setenv("LLM_REVIEW_BASE_URL", "https://other.example/v1")
    with pytest.raises(RuntimeError, match="LLM_REVIEW_API_KEY"):
        credentials(reviewer=True)
    monkeypatch.setenv("LLM_REVIEW_API_KEY", "other-key")
    assert credentials(reviewer=True) == ("other-key", "review-model")
    assert base_url(reviewer=True) == "https://other.example/v1"
    # Plain http would send the key unencrypted, except to a server on this machine.
    monkeypatch.setenv("LLM_REVIEW_BASE_URL", "http://other.example/v1")
    with pytest.raises(RuntimeError, match="LLM_REVIEW_BASE_URL must be an https"):
        credentials(reviewer=True)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert base_url() == "http://localhost:11434/v1"


def test_llm_retries_rate_limits_but_not_an_empty_balance(monkeypatch):
    # Both come back as HTTP 429 from OpenAI: a rate limit passes if you wait, an empty
    # balance never does and must stop a batch rather than fail every job in turn.
    import requests
    from jobflow import llm

    class Reply:
        def __init__(self, status, body, headers=None):
            self.status_code, self.body, self.headers = status, body, headers or {}
            self.ok, self.reason = status < 400, "Too Many Requests"

        def json(self):
            return self.body

    replies, waits = [], []
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    limited = {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}
    replies[:] = [Reply(429, limited, {"Retry-After": "3"}),
                  Reply(200, {"choices": [{"message": {"content": "ok"}}]})]
    assert llm.complete("s", "u") == ("ok", "model") and waits == [3.0]

    empty = {"error": {"message": "You have no credits remaining.", "type": "insufficient_quota",
                       "code": "credit_balance_exhausted"}}
    replies[:] = [Reply(429, empty)]
    with pytest.raises(llm.LLMError, match="no credits remaining") as caught:
        llm.complete("s", "u")
    assert caught.value.fatal and not replies and waits == [3.0]


def test_llm_drops_settings_the_provider_rejects(monkeypatch):
    # Anthropic's OpenAI-compatible API rejects JSON mode, and newer Claude models reject
    # temperature, each with a 400 that failed every job in a scoring run.
    import requests
    from jobflow import llm

    class Reply:
        def __init__(self, status, body):
            self.status_code, self.body, self.headers = status, body, {}
            self.ok, self.reason = status < 400, "Bad Request"

        def json(self):
            return self.body

    sent = []

    def post(url, json, **kwargs):
        sent.append(sorted(k for k in json if k in llm.OPTIONAL))
        if "response_format" in json:
            return Reply(400, {"error": {"message": "response_format.type: Input should be 'json_schema'"}})
        if "temperature" in json:
            return Reply(400, {"error": {"message": "`temperature` is deprecated for this model."}})
        return Reply(200, {"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    content, _ = llm.complete("s", "u", json_mode=True)
    assert llm.parse_json(content) == {"a": 1}
    assert sent == [["response_format", "temperature"], ["temperature"], []]


def test_jobs_run_two_at_a_time_print_whole_and_stop_on_a_bad_key(monkeypatch, capsys):
    import re
    import sys
    import time
    from jobflow import cli, db
    from jobflow.llm import LLMError

    class Conn:
        def execute(self, *args):
            return self

        def fetchall(self):
            return [(i, "Acme", f"Role {i}", False, False) for i in range(1, 7)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(db, "connect", Conn)
    running, most = set(), []

    def run_one(job_id):
        running.add(job_id)
        most.append(len(running))
        print(f"start {job_id}")
        # Job 3 fails at once, so the stop is set while job 4 (started with it) still runs.
        time.sleep(0 if job_id == 3 else 0.05)
        print(f"warning {job_id}", file=sys.stderr)
        print(f"end {job_id}")
        running.discard(job_id)
        if job_id == 3:
            raise LLMError("Invalid key (LLM provider, HTTP 401)", fatal=True)

    with pytest.raises(SystemExit, match="Stopped"):
        cli.for_jobs("interested", "tailor", "a tailored CV", run_one)
    out = capsys.readouterr().out
    assert max(most) == 2
    # Each job's lines come out together, never interleaved with the other job's.
    blocks = re.findall(r"#(\d) Role \1 \(Acme\)\n(.*?)(?=\n\n#|\Z)", out, re.S)
    assert blocks and all(body.strip() == f"start {job}\nend {job}" for job, body in blocks)
    # Job 4 may run alongside job 3; nothing after it starts.
    assert {job for job, _ in blocks} <= {"1", "2", "3", "4"}
