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
