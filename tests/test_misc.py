from jobflow.db import migration_files
from jobflow.candidate.interview import _safe_markup


def test_migrations_ship_inside_the_package():
    # A non-editable install (as in GitHub Actions) must still find them.
    names = [name for name, _ in migration_files()]
    assert names and names[0] == "001_initial.sql" and names == sorted(names)


def test_briefing_markup_cannot_inject_html():
    out = _safe_markup('<script>alert(1)</script>\n'
                       '[click](javascript:alert(1)) [x](https://ok.example/" onmouseover="bad)')
    assert "<script>" not in out
    assert 'href="javascript' not in out and 'onmouseover="bad' not in out


def test_briefing_citations_become_links():
    out = _safe_markup("See [annual report](https://example.com/r?a=1&b=2) and "
                       "https://example.com/news.\n1. **Prepare** a story")
    assert '<a href="https://example.com/r?a=1&amp;b=2" rel="noreferrer">annual report</a>' in out
    assert '<a href="https://example.com/news" rel="noreferrer">https://example.com/news</a>.' in out
    assert "<ol>\n<li><strong>Prepare</strong> a story</li>\n</ol>" in out


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
    for key in ("LLM_MODEL", "LLM_API_KEY", "EXAMPLE_IDS", "LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "from-github-secret")
    load_env(env)
    assert os.environ["LLM_MODEL"] == "gpt-x"
    assert os.environ["LLM_API_KEY"] == "k#1"
    assert os.environ["EXAMPLE_IDS"] == "a,b"
    assert "LLM_BASE_URL" not in os.environ  # empty values don't mask defaults
    assert os.environ["APIFY_TOKEN"] == "from-github-secret"


def test_init_never_overwrites_personal_files_and_keeps_env_private(tmp_path):
    import stat

    from jobflow.settings import init_files

    for template in (".env.example", "profile.example.toml", "searches.example.toml"):
        (tmp_path / template).write_text(f"# template {template}\n")
    (tmp_path / ".env").write_text("DATABASE_URL=my-real-secret\n")  # already set up
    results = dict(init_files(tmp_path))
    assert results == {".env": False, "profile.toml": True, "searches.toml": True}
    assert (tmp_path / ".env").read_text() == "DATABASE_URL=my-real-secret\n"
    assert (tmp_path / "searches.toml").read_text() == "# template searches.example.toml\n"
    (tmp_path / ".env").unlink()
    init_files(tmp_path)
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
