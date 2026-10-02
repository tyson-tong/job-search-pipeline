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
