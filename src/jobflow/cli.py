from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def main() -> None:
    from .settings import load_env
    load_env()
    from .ingest.options import SITES

    parser = argparse.ArgumentParser(
        prog="jobflow",
        description="First time: init. Everyday: web (job tracker, including your searches), "
        "tailor-cv, and interview-prep. After editing config/searches.toml: update-searches. "
        "The daily GitHub Action runs ingest.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config/.env, config/profile.toml, and config/searches.toml from the "
                   "templates (never overwrites existing files)")
    ingest = sub.add_parser("ingest", help="Run the searches in config/searches.toml on Apify now and "
                            "import the results (the daily Action runs this; costs Apify credits)")
    ingest.add_argument("--search", help="Only this search name")
    ingest.add_argument("--site", choices=SITES, help="Only this job site")
    update = sub.add_parser("update-searches", help="After editing config/searches.toml: check "
                            "it, update the GitHub copy the daily import reads, and filter Inbox "
                            "jobs matching its exclude_title_terms (and none of its "
                            "include_title_terms)")
    update.add_argument("--dry-run", action="store_true",
                        help="Check the file and list the jobs that would be filtered; change nothing")
    web = sub.add_parser("web", help="Open the job tracker in your browser (local only)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    tailor = sub.add_parser("tailor-cv", help="Draft a tailored CV, with a fact check, for "
                            "every Interviewing job that doesn't have one yet")
    tailor.add_argument("--cv", help="CV variant name (the part after CV_ in the file name) to use "
                        "for every job; defaults to each job's best-matching variant, which "
                        "needs the job to be scored")
    sub.add_parser("interview-prep", help="Generate an interview briefing HTML for every "
                   "Interviewing job that doesn't have one yet")
    args = parser.parse_args()
    try:
        run(args, parser)
    except RuntimeError as exc:
        # Expected problems (missing settings, unknown job, bad CV) without a traceback.
        raise SystemExit(f"error: {exc}") from None


def for_interviewing_jobs(kind: str, what: str, run_one) -> None:
    """Run run_one(job_id) for each Interviewing job without a recorded `kind` run.

    A run is recorded in workflow_runs only once it finishes, so a job is never done
    twice, and a failed job is retried next time. One failure doesn't stop the rest.
    """
    from .db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT j.id, j.company, j.title, EXISTS (SELECT 1 FROM workflow_runs r "
            "WHERE r.job_id=j.id AND r.kind=%s) FROM jobs j WHERE j.status='interviewing' "
            "ORDER BY j.id", (kind,)).fetchall()
    todo = [row for row in rows if not row[3]]
    print(f"{len(rows)} Interviewing job{'s' if len(rows) != 1 else ''}: "
          f"{len(rows) - len(todo)} already have {what}, {len(todo)} to do")
    failed = 0
    for job_id, company, title, _ in todo:
        print(f"\n#{job_id} {title} ({company or 'unknown employer'})")
        try:
            run_one(job_id)
        except Exception as exc:
            failed += 1
            print(f"error: {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(f"{failed} of {len(todo)} failed; run the command again to retry them")


def update_searches(dry_run: bool = False) -> None:
    """Check config/searches.toml, publish it as the JOBFLOW_SEARCHES GitHub variable that
    the daily import reads, then filter Inbox jobs matching its excluded title terms
    (imports only apply them to the jobs they bring in). An invalid file stops before
    anything is published, so a typo never reaches the daily import."""
    from .db import migrate
    from .ingest.searches import parse_config
    from .ingest.store import exclude_inbox_titles
    from .settings import ROOT, searches_file

    path = searches_file()
    if not path.exists():
        raise RuntimeError(f"No searches file at {path}. Run `uv run jobflow init` first")
    text = path.read_text()
    searches = parse_config(text)
    print(f"{path}: valid ({len(searches)} search{'es' if len(searches) != 1 else ''})")
    if dry_run:
        print("Would update the JOBFLOW_SEARCHES GitHub variable (dry run: not updated)")
    else:
        gh = shutil.which("gh")
        if not gh:
            raise RuntimeError("GitHub CLI (gh) not found. Install it and run `gh auth login`, "
                               "or set the variable by hand: "
                               "gh variable set JOBFLOW_SEARCHES < config/searches.toml")
        # gh finds the repository from this checkout's Git remote.
        result = subprocess.run([gh, "variable", "set", "JOBFLOW_SEARCHES"], input=text,
                                text=True, capture_output=True, cwd=ROOT)
        if result.returncode:
            raise RuntimeError(f"Updating the JOBFLOW_SEARCHES GitHub variable failed: "
                               f"{(result.stderr or result.stdout).strip()}. Nothing else was changed")
        print("Updated the JOBFLOW_SEARCHES GitHub variable: the daily import uses this file now")
    migrate()
    matched, checked = exclude_inbox_titles(searches, dry_run=dry_run)
    for job in matched:
        print(f"  #{job['id']} {job['title']} ({job['company'] or 'unknown employer'}): "
              f"{job['reason']}")
    print(f"{'Would filter' if dry_run else 'Filtered'} {len(matched)} of {checked} Inbox jobs"
          f"{' (dry run: nothing changed)' if dry_run else ''}")


def init() -> None:
    from .settings import CONFIG_DIR, init_files

    results = init_files()
    for name, created in results:
        print(f"{'created' if created else 'kept   '}  {CONFIG_DIR / name}"
              + ("" if created else "  (already exists; not changed)"))
    if not any(created for _, created in results):
        print("\nNothing to create: all three files exist.")
        return
    print("""
Next:
  1. config/.env           DATABASE_URL, APIFY_TOKEN, LLM_API_KEY, LLM_MODEL, WEB_SEARCH_API_KEY
  2. config/profile.toml   cv_directory: the folder with your CV_*.yaml files
  3. config/searches.toml  your job searches (it starts with example searches: replace them)
  4. uv run jobflow web, then the Searches tab to check your searches""")


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "init":
        init()
    elif args.command == "ingest":
        from .db import migrate
        from .ingest.apify import ingest_searches
        migrate()
        ingest_searches(args.search, args.site)
    elif args.command == "update-searches":
        update_searches(dry_run=args.dry_run)
    elif args.command == "web":
        from .db import migrate
        from .web.server import serve
        migrate()
        serve(args.port, open_browser=not args.no_browser)
    elif args.command == "tailor-cv":
        from .candidate.tailoring import tailor_cv
        from .db import migrate
        migrate()

        def tailor_one(job_id: int) -> None:
            target, errors, warnings = tailor_cv(job_id, args.cv)
            print(target)
            for message in errors:
                print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            print(f"Review {target.parent / 'REVIEW_REQUIRED.md'} before using this draft.")

        for_interviewing_jobs("tailor", "a tailored CV", tailor_one)
    elif args.command == "interview-prep":
        from .candidate.interview import prepare
        from .db import migrate
        migrate()
        for_interviewing_jobs("interview-prep", "a briefing", lambda job_id: print(prepare(job_id)))
