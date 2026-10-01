import pytest

# Settings from your shell that would let a test reach your real database, LLM provider,
# Tavily, or Apify, or change what it expects. Tests that need one set it themselves.
REAL_SETTINGS = ("DATABASE_URL", "APIFY_TOKEN", "APIFY_MAX_WAIT_MINUTES", "JOBFLOW_SEARCHES",
                 "LLM_API_KEY", "OPENAI_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
                 "LLM_REVIEW_API_KEY", "LLM_REVIEW_MODEL", "LLM_REVIEW_BASE_URL",
                 "WEB_SEARCH_API_KEY",
                 # Paths that would point a test at your own CVs, output, or searches.
                 "CV_DIRECTORY", "OUTPUT_DIRECTORY", "SEARCHES_FILE",
                 # libpq's own settings: with no password in TEST_DATABASE_URL, PGPASSWORD
                 # would be sent to the test server, and PGSSLMODE changes what
                 # encrypted_url does.
                 "PGHOST", "PGHOSTADDR", "PGPORT", "PGUSER", "PGPASSWORD", "PGPASSFILE",
                 "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE", "PGOPTIONS")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path_factory):
    from jobflow import settings

    for name in REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    # Not the timezone in your config/profile.toml.
    monkeypatch.setenv("JOBFLOW_TIMEZONE", "Australia/Sydney")
    # A profile of the test's own, with empty cv/ and output/ folders beside it, so a test
    # that forgets to patch a path reads no CV of yours and writes nothing into your output/.
    home = tmp_path_factory.mktemp("jobflow")
    (home / "cv").mkdir()
    (home / "output").mkdir()
    profile = home / "config" / "profile.toml"
    profile.parent.mkdir()
    profile.write_text('cvs = []\n[paths]\ncv_directory = "../cv"\noutput_directory = "../output"\n')
    monkeypatch.setenv("JOBFLOW_PROFILE", str(profile))
    # Nor your config/.env, with its real keys.
    monkeypatch.setattr(settings, "env_path", lambda: home / "config" / ".env")
