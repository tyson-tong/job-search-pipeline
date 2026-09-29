import pytest

# Settings from your shell that would let a test reach your real database, LLM provider,
# Tavily, or Apify, or change what it expects. Tests that need one set it themselves.
REAL_SETTINGS = ("DATABASE_URL", "APIFY_TOKEN", "APIFY_MAX_WAIT_MINUTES", "JOBFLOW_SEARCHES",
                 "LLM_API_KEY", "OPENAI_API_KEY", "LLM_MODEL", "LLM_BASE_URL",
                 "LLM_REVIEW_API_KEY", "LLM_REVIEW_MODEL", "LLM_REVIEW_BASE_URL",
                 "WEB_SEARCH_API_KEY")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    # Not the timezone in your config/profile.toml.
    monkeypatch.setenv("JOBFLOW_TIMEZONE", "Australia/Sydney")
