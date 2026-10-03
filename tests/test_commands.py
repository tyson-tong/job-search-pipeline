import pytest

from jobflow import cleanup, db, github, settings
from jobflow.ingest.searches import SearchConfigError


def test_update_searches_never_publishes_an_invalid_file(tmp_path, monkeypatch):
    # The daily import reads the published copy, so a typo must stop before gh runs.
    broken = tmp_path / "searches.toml"
    broken.write_text('[[search]]\nname = "x"\nterms = ["quant"]\nlocations = { AU = ["Syndey"] }\n')
    monkeypatch.setattr(settings, "searches_file", lambda: broken)
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: pytest.fail("published anyway"))
    with pytest.raises(SearchConfigError, match="unknown AU city"):
        github.update_searches()


def test_clean_up_deletes_only_closed_jobs_folders_and_only_on_yes(tmp_path, monkeypatch):
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
    cleanup.clean_up()
    assert closed.exists()
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    cleanup.clean_up()
    assert not closed.exists() and all(folder.exists() for folder in others)


def test_clean_up_keeps_the_folder_of_a_job_that_moved_on_while_you_decided(tmp_path, monkeypatch):
    # The tracker may move a job back to Interested while the prompt waits: its folder, with
    # the CV you're about to send, must not be deleted.
    answers = iter([[(4, "rejected"), (7, "rejected")],   # listed for the prompt
                    [(4, "rejected")]])                    # checked again after "y"

    class Conn:
        def execute(self, *args): return self
        def fetchall(self): return next(answers)
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(db, "connect", Conn)
    monkeypatch.setattr(settings, "output_directory", lambda: tmp_path)
    rejected, moved_on = tmp_path / "acme" / "analyst-4", tmp_path / "beta" / "quant-7"
    for folder in (rejected, moved_on):
        folder.mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cleanup.clean_up()
    assert not rejected.exists() and moved_on.exists()


def test_the_daily_import_needs_no_document_libraries():
    # The daily GitHub Action installs without the documents group (uv sync --no-group
    # documents), so an import of one on ingest's path would break only the 00:30 run.
    import subprocess
    import sys

    code = ("import sys\n"
            "for name in ('docx', 'pypdf', 'rendercv', 'yaml'):\n"
            "    sys.modules[name] = None   # importing it now fails, as in the Action\n"
            "from jobflow import cli, db\n"
            "from jobflow.ingest import apify, store, upkeep\n"
            "cli.build_parser().parse_args(['ingest'])\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
