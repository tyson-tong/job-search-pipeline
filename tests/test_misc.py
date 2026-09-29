import pytest

from jobflow.candidate.interview import _safe_markup


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
                   "APIFY_MAX_WAIT_MINUTES=\"30\" # quoted, then a comment\n"
                   "LLM_BASE_URL=\n"
                   "APIFY_TOKEN=from-file\n")
    # A throwaway copy, so nothing load_env sets leaks into later tests.
    environ = {k: v for k, v in os.environ.items()
               if k not in ("LLM_MODEL", "LLM_API_KEY", "EXAMPLE_IDS", "LLM_BASE_URL",
                            "APIFY_MAX_WAIT_MINUTES")}
    environ["APIFY_TOKEN"] = "from-github-secret"
    monkeypatch.setattr(os, "environ", environ)
    load_env(env)
    assert os.environ["LLM_MODEL"] == "gpt-x"
    assert os.environ["LLM_API_KEY"] == "k#1"
    assert os.environ["EXAMPLE_IDS"] == "a,b"
    assert os.environ["APIFY_MAX_WAIT_MINUTES"] == "30"   # quotes stripped, even before a comment
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

    from jobflow import github, settings
    from jobflow.ingest.searches import SearchConfigError

    broken = tmp_path / "searches.toml"
    broken.write_text('[[search]]\nname = "x"\nterms = ["quant"]\nlocations = { AU = ["Syndey"] }\n')
    monkeypatch.setattr(settings, "searches_file", lambda: broken)
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: pytest.fail("published anyway"))
    with pytest.raises(SearchConfigError, match="unknown AU city"):
        github.update_searches()


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


def test_a_database_on_another_machine_is_only_reached_encrypted():
    # With no sslmode, psycopg connects unencrypted when allowed, sending the password and
    # every job in the clear.
    from psycopg.conninfo import conninfo_to_dict

    from jobflow.db import encrypted_url

    remote = conninfo_to_dict(encrypted_url("postgresql://u:secret@ep-x.neon.tech/db"))
    assert remote["sslmode"] == "require" and remote["password"] == "secret"
    with pytest.raises(RuntimeError, match="sslmode=disable") as caught:
        encrypted_url("postgresql://u:secret@db.example/db?sslmode=disable")
    assert "secret" not in str(caught.value)
    local = "postgresql://postgres:pw@127.0.0.1:5432/postgres"
    assert encrypted_url(local) == local   # a test database on this machine


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


def test_llm_retries_a_dropped_connection_and_rejects_an_empty_reply(monkeypatch):
    # A network blip failed the job outright; an empty reply crashed later, far from its cause.
    import requests

    from jobflow import llm

    class Reply:
        ok, status_code, headers = True, 200, {}

        def __init__(self, content):
            self.content = content

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    replies, waits = [], []

    def post(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    replies[:] = [requests.ConnectionError("reset"), Reply("ok")]
    assert llm.complete("s", "u") == ("ok", "model") and waits == [2.0]
    replies[:] = [Reply(None)]
    with pytest.raises(llm.LLMError, match="no text"):
        llm.complete("s", "u")


class _Rows:
    """A stand-in connection whose queries all return `rows`, recording what it runs."""

    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def __call__(self):
        return self

    def execute(self, sql, *args):
        self.sql.append(sql)
        return self

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_a_bad_key_stops_the_batch_instead_of_failing_every_job():
    # A bad key or an empty balance fails every request: tailor-cv, cover-letter,
    # interview-prep, and the tracker's scoring must stop, not try (and bill) each job.
    import time

    from jobflow.batch import run_each
    from jobflow.llm import LLMError

    tried = []

    def work(job_id):
        tried.append(job_id)
        if job_id == 1:
            raise LLMError("Invalid key (LLM provider, HTTP 401)", fatal=True)
        if job_id == 3:
            raise RuntimeError("one job's own problem")
        time.sleep(0.05)
    failed, skipped = run_each([1, 2, 3, 4, 5, 6], work)
    assert set(tried) <= {1, 2}   # only job 2 may have started alongside job 1
    assert 1 in failed and set(skipped) == {1, 2, 3, 4, 5, 6} - set(tried)
    # Any other failure doesn't stop the rest.
    tried.clear()
    assert run_each([3, 4, 5], work) == ([3], []) and sorted(tried) == [3, 4, 5]


def test_a_job_counts_as_done_only_while_its_files_exist(monkeypatch, tmp_path):
    # clean-up deletes folders but not the runs that made them: a job moved back to
    # Interested must be drafted again, not skipped as done.
    from jobflow import batch, db
    from jobflow.candidate import paths

    conn = _Rows([(1, "Acme", "Analyst", True, True),     # done, folder deleted
                  (2, "Acme", "Engineer", True, True),    # done, folder there
                  (3, "Beta", "Quant", False, True)])     # draft failed, fixed by hand
    monkeypatch.setattr(db, "connect", conn)
    monkeypatch.setattr(paths, "output_directory", lambda: tmp_path)
    (tmp_path / "acme" / "engineer-2").mkdir(parents=True)
    ran, extra = [], []
    batch._run_jobs("interested", "tailor", "a tailored CV", ran.append,
                  lambda folder: folder.exists(), lambda folder: folder.name == "quant-3",
                  lambda folder: extra.append(folder.name))
    assert ran == [1]
    assert any("FIXED-BY-HAND" in sql for sql in conn.sql)
    assert extra == ["engineer-2", "quant-3"]  # --format's extra file, for the jobs already done


def test_clean_up_deletes_only_closed_jobs_folders_and_only_on_yes(tmp_path, monkeypatch):
    from jobflow import cli, db, settings

    class Conn:
        def execute(self, *args): return self
        def fetchall(self): return [(4, "rejected")]
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(db, "connect", Conn)
    monkeypatch.setattr(settings, "output_directory", lambda: tmp_path)
    # Job 4's folder, then folders of jobs 14 and 12 whose names also contain "4".
    closed, others = tmp_path / "acme" / "analyst-4", [tmp_path / "acme" / "analyst-14",
                                                       tmp_path / "beta" / "dev-4-12"]
    for folder in [closed, *others]:
        folder.mkdir(parents=True)
        (folder / "CV_REVIEW.md").write_text("x")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    cli.clean_up()
    assert closed.exists()
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    cli.clean_up()
    assert not closed.exists() and all(folder.exists() for folder in others)
