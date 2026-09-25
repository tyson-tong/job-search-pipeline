import json

import pytest

from jobflow.db import migration_files
from jobflow.interview import _safe_markup
from jobflow.llm import DEFAULT_BASE_URL, base_url


def test_migrations_are_packaged_and_ordered():
    names = [name for name, _ in migration_files()]
    assert names[:2] == ["001_initial.sql", "002_job_fingerprint.sql"]
    assert names == sorted(names)


def test_empty_llm_base_url_uses_default(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    assert base_url() == DEFAULT_BASE_URL
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1/")
    assert base_url() == "https://llm.example/v1"
    monkeypatch.delenv("LLM_BASE_URL")
    assert base_url() == DEFAULT_BASE_URL


def test_markup_escapes_html():
    out = _safe_markup("<script>alert(1)</script>\n- <b>x</b>")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<li>&lt;b&gt;x&lt;/b&gt;</li>" in out


def test_markup_renders_links_emphasis_and_lists():
    out = _safe_markup(
        "## Company\n"
        "See [annual report](https://example.com/report?a=1&b=2) and https://example.com/news.\n"
        "1. **Prepare** the `STAR` story\n"
        "2. Second\n"
        "- bullet")
    assert "<h3>Company</h3>" in out
    assert '<a href="https://example.com/report?a=1&amp;b=2" rel="noreferrer">annual report</a>' in out
    assert '<a href="https://example.com/news" rel="noreferrer">https://example.com/news</a>.' in out
    assert "<ol>\n<li><strong>Prepare</strong> the <code>STAR</code> story</li>\n<li>Second</li>\n</ol>" in out
    assert "<ul>\n<li>bullet</li>\n</ul>" in out


def test_markup_does_not_link_unsafe_schemes():
    out = _safe_markup('[click](javascript:alert(1)) [x](https://ok.example/" onmouseover="bad)')
    assert 'href="javascript' not in out
    assert 'onmouseover="bad' not in out


def _stub_prepare(monkeypatch, tmp_path):
    import contextlib
    from jobflow import interview

    payloads = []

    def fake_complete(system, user, json_mode=False):
        payloads.append(json.loads(user))
        return "## Brief\n- point", "fake-model"

    class Conn:
        def execute(self, *args):
            pass

    monkeypatch.setattr(interview, "fetch_job", lambda job_id: {
        "id": job_id, "url": "https://example.com/job", "title": "Analyst",
        "company": "Acme", "location": "Sydney", "description": "SQL"})
    monkeypatch.setattr(interview, "web_search", lambda company, title: [])
    monkeypatch.setattr(interview, "cv_sources", lambda: {"demo": "cv: {}"})
    monkeypatch.setattr(interview, "complete", fake_complete)
    monkeypatch.setattr(interview, "connect", lambda: contextlib.nullcontext(Conn()))
    monkeypatch.setattr(interview, "timezone", lambda: None)
    return interview, payloads


@pytest.mark.parametrize("email", [None, "", "  \n"])
def test_interview_prep_without_email(monkeypatch, tmp_path, email):
    interview, payloads = _stub_prepare(monkeypatch, tmp_path)
    target = interview.prepare(1, email, output=tmp_path / "brief.html")
    assert target.exists()
    assert len(payloads) == len(interview.AGENTS) + 1
    assert all(p["interview_email"] == interview.NO_EMAIL for p in payloads)


def test_interview_prep_passes_email_to_every_agent(monkeypatch, tmp_path):
    interview, payloads = _stub_prepare(monkeypatch, tmp_path)
    interview.prepare(1, "Panel interview on Tuesday", output=tmp_path / "brief.html")
    assert all(p["interview_email"] == "Panel interview on Tuesday" for p in payloads)


def test_load_env_parses_and_never_overrides(monkeypatch, tmp_path):
    from jobflow.settings import load_env

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "DATABASE_URL=postgresql://u:p@h/db?sslmode=require\n"
        "export LLM_MODEL='gpt-x'\n"
        'LLM_API_KEY="k#1"\n'
        "EXAMPLE_IDS=a,b # my ids\n"
        "LLM_BASE_URL=\n"
        "APIFY_TOKEN=from-file\n"
        "not a setting\n")
    for key in ("DATABASE_URL", "LLM_MODEL", "LLM_API_KEY", "EXAMPLE_IDS", "LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "from-shell")
    load_env(env)
    import os
    assert os.environ["DATABASE_URL"] == "postgresql://u:p@h/db?sslmode=require"
    assert os.environ["LLM_MODEL"] == "gpt-x"
    assert os.environ["LLM_API_KEY"] == "k#1"
    assert os.environ["EXAMPLE_IDS"] == "a,b"
    assert "LLM_BASE_URL" not in os.environ
    assert os.environ["APIFY_TOKEN"] == "from-shell"


def test_load_env_missing_file_is_ignored(tmp_path):
    from jobflow.settings import load_env

    load_env(tmp_path / "absent.env")
