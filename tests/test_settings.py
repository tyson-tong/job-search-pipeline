import os
import stat

import pytest
from psycopg.conninfo import conninfo_to_dict

from jobflow.db import encrypted_url
from jobflow.settings import init_files, load_env


def test_env_file_never_overrides_real_environment(monkeypatch, tmp_path):
    # On GitHub the secrets are real environment variables: a config/.env overriding them would use
    # the wrong keys; a misread value (quotes, comments) breaks a setting.
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
    # init run again must not overwrite your keys or settings, and config/.env holds secrets, so
    # only you may read it.
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


def test_a_database_on_another_machine_is_only_reached_encrypted():
    # With no sslmode, psycopg connects unencrypted when allowed, sending the password and
    # every job in the clear.
    remote = conninfo_to_dict(encrypted_url("postgresql://u:secret@ep-x.neon.tech/db"))
    assert remote["sslmode"] == "require" and remote["password"] == "secret"
    with pytest.raises(RuntimeError, match="sslmode=disable") as caught:
        encrypted_url("postgresql://u:secret@db.example/db?sslmode=disable")
    assert "secret" not in str(caught.value)
    local = "postgresql://postgres:pw@127.0.0.1:5432/postgres"
    assert encrypted_url(local) == local   # a test database on this machine
    # A hostaddr is where libpq connects, even with no host or a local one.
    for url in ("postgresql://u:secret@/db?hostaddr=203.0.113.5",
                "postgresql://u:secret@localhost/db?hostaddr=203.0.113.5"):
        assert conninfo_to_dict(encrypted_url(url))["sslmode"] == "require"
        with pytest.raises(RuntimeError, match="sslmode=prefer") as caught:
            encrypted_url(url + "&sslmode=prefer")
        assert "secret" not in str(caught.value)


def test_a_broken_profile_is_an_error_not_a_quiet_switch_to_utc(monkeypatch, tmp_path):
    # Every date (today, follow-ups, archiving) would silently move to UTC.
    from jobflow.settings import timezone

    monkeypatch.delenv("JOBFLOW_TIMEZONE")
    profile = tmp_path / "profile.toml"
    monkeypatch.setenv("JOBFLOW_PROFILE", str(profile))
    assert timezone().key == "UTC"   # no profile at all, as in GitHub Actions
    profile.write_text('[locale]\ntimezone = "Australia/Sydney\n')   # unclosed quote
    with pytest.raises(RuntimeError, match="not valid TOML"):
        timezone()
