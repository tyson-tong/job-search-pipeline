import pytest

from jobflow import cleanup, db, github, settings
from jobflow.ingest.searches import SearchConfigError


def test_update_searches_never_publishes_an_invalid_file(tmp_path, monkeypatch):
    # The daily import reads the published copy, so a typo must stop before gh runs.
    broken = tmp_path / "searches.toml"
    broken.write_text('[[search]]\nname = "x"\nterms = ["quant"]\n'
                      'locations = { AU = ["Syndey"] }\n')
    monkeypatch.setattr(settings, "searches_file", lambda: broken)
    # conftest's no_github fails the test if gh runs.
    with pytest.raises(SearchConfigError, match="unknown AU city"):
        github.update_searches()


def test_clean_up_deletes_only_closed_jobs_folders_and_only_on_yes(tmp_path, monkeypatch):
    # Clean-up deletes folders for good: matching on a job id within a folder name would delete the
    # folders of other jobs whose ids contain it, and deleting without a yes loses the CVs and
    # letters you sent.
    class Conn:
        def execute(self, *args): return self
        def fetchall(self): return [(4, "withdrawn")]
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
    answers = iter([[(4, "withdrawn"), (7, "withdrawn")],  # listed for the prompt
                    [(4, "withdrawn")]])                   # checked again after "y"

    class Conn:
        def execute(self, *args): return self
        def fetchall(self): return next(answers)
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(db, "connect", Conn)
    monkeypatch.setattr(settings, "output_directory", lambda: tmp_path)
    withdrawn, moved_on = tmp_path / "acme" / "analyst-4", tmp_path / "beta" / "quant-7"
    for folder in (withdrawn, moved_on):
        folder.mkdir(parents=True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cleanup.clean_up()
    assert not withdrawn.exists() and moved_on.exists()


def test_the_daily_import_needs_no_document_libraries():
    # The daily GitHub Action installs without the documents group (uv sync --no-group
    # documents), so an import of one on ingest's path would break only the daily run.
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


def test_a_big_batch_in_a_terminal_asks_before_spending(monkeypatch, capsys):
    # tailor-cv on every Interested job makes several LLM calls and web searches per job.
    from jobflow import batch

    todo = [(job_id, "Acme", "Analyst") for job_id in range(1, batch.CONFIRM_ABOVE + 2)]
    monkeypatch.setattr(batch, "_plan_jobs",
                        lambda *args: batch.Plan(len(todo), todo, [], [], 0, 0))
    monkeypatch.setattr(batch, "_in_terminal", lambda: True)
    ran: list[int] = []
    command = batch.JobCommand(command="tailor-cv", status="interested", kind="tailor",
                               what="a tailored CV", run_one=ran.append,
                               output_exists=lambda folder: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    batch._run_jobs(command)
    assert ran == [] and "Nothing done" in capsys.readouterr().out
    # Jobs you name with --job are your choice already.
    batch._run_jobs(command, only=[job_id for job_id, _, _ in todo])
    assert sorted(ran) == [job_id for job_id, _, _ in todo]
    ran.clear()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    batch._run_jobs(command)
    assert sorted(ran) == [job_id for job_id, _, _ in todo]


def test_a_job_in_chinese_keeps_the_folder_its_drafts_are_in(tmp_path):
    # Named only in Chinese, every such job's folder was output/unknown/unknown-<id>. Kept
    # names would move a job already tailored to a new folder, where its drafts aren't, and
    # tailor-cv would pay to draft it again.
    from jobflow.candidate.paths import job_directory

    job = {"company": "滙豐銀行", "title": "數據分析師"}
    assert job_directory(7, job, tmp_path) == tmp_path / "滙豐銀行" / "數據分析師-7"
    (tmp_path / "unknown" / "unknown-8").mkdir(parents=True)
    assert job_directory(8, job, tmp_path) == tmp_path / "unknown" / "unknown-8"
