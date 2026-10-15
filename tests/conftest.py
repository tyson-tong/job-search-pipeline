import socket

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
                 "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE", "PGOPTIONS",
                 # Set in GitHub Actions, where it makes load_searches refuse a searches
                 # file: a test reading one would pass locally and fail there.
                 "GITHUB_ACTIONS")
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "localhost")


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
    profile.write_text('cvs = []\n[paths]\ncv_directory = "../cv"\n'
                       'output_directory = "../output"\n')
    monkeypatch.setenv("JOBFLOW_PROFILE", str(profile))
    # Nor your config/.env, with its real keys.
    monkeypatch.setattr(settings, "env_path", lambda: home / "config" / ".env")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse any connection off this machine, so a test that forgets to fake the LLM,
    Tavily, Apify, or GitHub fails instead of sending data or spending credits. The test
    web server is on this machine, and libpq (the database tests) doesn't go through
    Python's sockets."""
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def local(host) -> bool:
        return isinstance(host, str) and (host in LOCAL_ADDRESSES or host.startswith("/"))

    def guarded_connect(sock, address):
        host = address if isinstance(address, str) else address[0]
        if sock.family != getattr(socket, "AF_UNIX", None) and not local(host):
            raise RuntimeError(f"a test tried to reach {host} over the network")
        return connect(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not local(host):
            raise RuntimeError(f"a test tried to look up {host} on the network")
        return getaddrinfo(host, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Refuse to run the GitHub CLI: it runs as a subprocess, which no_network can't stop,
    with your own GitHub login, so a test that forgets to fake it could set your real
    repository's variables. A test that needs gh fakes _gh (or subprocess.run) itself."""
    from jobflow import github

    def refuse(args, *rest, **kwargs):
        raise RuntimeError(f"a test tried to run the real GitHub CLI: gh {' '.join(args)}")
    monkeypatch.setattr(github, "_gh", refuse)


class NullConnection:
    """A database connection that accepts every statement and returns no rows."""

    def execute(self, *args):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def no_database(monkeypatch):
    """For tests of code that only records its result: every connection is a NullConnection."""
    from jobflow import db

    monkeypatch.setattr(db, "connect", NullConnection)
