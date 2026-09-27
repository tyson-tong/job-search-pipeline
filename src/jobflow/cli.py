from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def check_variant(variant: str | None, yaml_only: bool) -> None:
    """Stop before any job is tried if --cv names no CV (for tailor-cv, no RenderCV YAML
    CV)."""
    from .candidate.cvs import cv_files, is_yaml

    choices = [name for name, path in cv_files().items() if is_yaml(path) or not yaml_only]
    if variant is not None and variant not in choices:
        raise RuntimeError(f"No {'RenderCV YAML ' if yaml_only else ''}CV named {variant!r}; "
                           f"choose from {', '.join(choices) or 'none found'}")


def main() -> None:
    import psycopg

    from .settings import load_env
    load_env()
    from .ingest.options import SITES

    parser = argparse.ArgumentParser(
        prog="jobflow",
        description="First time: init. Everyday: web (job tracker, including your searches), "
        "tailor-cv, cover-letter (optional), interview-prep, and skill-gaps. After editing "
        "config/searches.toml: update-searches. The daily GitHub Action runs ingest.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config/.env, config/profile.toml, and config/searches.toml from the "
                   "templates (never overwrites existing files)")
    ingest = sub.add_parser("ingest", help="Run the searches in config/searches.toml on Apify now and "
                            "import the results (the daily Action runs this; costs Apify credits)")
    ingest.add_argument("--search", help="Only this search name")
    ingest.add_argument("--site", choices=SITES, help="Only this job site")
    update = sub.add_parser("update-searches", help="After editing config/searches.toml: check "
                            "it, update the GitHub copy the daily import reads, and filter Inbox "
                            "jobs from its exclude_companies or matching its "
                            "exclude_title_terms (and none of its include_title_terms)")
    update.add_argument("--dry-run", action="store_true",
                        help="Check the file and list the jobs that would be filtered; change nothing")
    web = sub.add_parser("web", help="Open the job tracker in your browser (local only)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    tailor = sub.add_parser("tailor-cv", help="Draft a tailored CV, reviewed and fact-checked, "
                            "for every Interested job that doesn't have one yet")
    tailor.add_argument("--cv", help="RenderCV YAML CV to use for every job, by name (the file "
                        "name without its extension and a leading CV_); defaults to each job's "
                        "best-matching YAML CV (an unscored job is scored first)")
    tailor.add_argument("--max-pages", type=positive_int, default=1, metavar="N",
                        help="Longest the CV may be as RenderCV lays it out; longer ones have "
                        "their least relevant bullets cut until they fit (default: 1)")
    tailor.add_argument("--format", choices=("yaml", "latex", "docx", "pdf"), default="yaml",
                        help="What to write besides the review: yaml (only the RenderCV YAML "
                        "draft, to render yourself), latex (also a .tex to compile), docx, or "
                        "pdf (rendered by RenderCV). Default: yaml")
    letter = sub.add_parser("cover-letter", help="Optional: draft a cover letter, reviewed and "
                            "fact-checked, for every Interested job that doesn't have one yet")
    letter.add_argument("--cv", help="CV to write from, in any format, for every job; defaults "
                        "to each job's best-matching CV (an unscored job is scored first)")
    sub.add_parser("interview-prep", help="Generate an interview briefing HTML for every "
                   "Interviewing job that doesn't have one yet")
    sub.add_parser("skill-gaps", help="Report the skills your CVs don't show that scored jobs "
                   "ask for most, with how to close each gap")
    args = parser.parse_args()
    try:
        run(args, parser)
    except RuntimeError as exc:
        # Expected problems (missing settings, unknown job, bad CV) without a traceback.
        raise SystemExit(f"error: {exc}") from None
    except psycopg.OperationalError as exc:
        raise SystemExit(f"error: can't use the database (check DATABASE_URL): {exc}") from None


def for_jobs(status: str, kind: str, what: str, run_one) -> None:
    """Run run_one(job_id) for each job with `status` and no recorded `kind` run.

    A run is recorded in workflow_runs only once it finishes, so a job is never done
    twice, and a failed job is retried next time. One failure doesn't stop the rest.
    """
    from .db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT j.id, j.company, j.title, EXISTS (SELECT 1 FROM workflow_runs r "
            "WHERE r.job_id=j.id AND r.kind=%s) FROM jobs j WHERE j.status=%s "
            "ORDER BY j.id", (kind, status)).fetchall()
    todo = [row for row in rows if not row[3]]
    print(f"{len(rows)} {status.capitalize()} job{'s' if len(rows) != 1 else ''}: "
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
    the daily import reads, then filter Inbox jobs from its excluded employers or matching
    its excluded title terms (imports only apply them to the jobs they bring in). An invalid file stops before
    anything is published, so a typo never reaches the daily import."""
    from .db import migrate
    from .ingest.searches import parse_config
    from .ingest.store import exclude_inbox_jobs
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
    matched, checked = exclude_inbox_jobs(searches, dry_run=dry_run)
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
        print("\nNothing to create: all three files exist. Your CVs go in cv/.")
        return
    print("""
Next:
  1. config/.env           DATABASE_URL, APIFY_TOKEN, LLM_API_KEY, LLM_MODEL, WEB_SEARCH_API_KEY
  2. cv/                   your CVs (names containing "cv": .yaml, .tex, .docx, or .pdf)
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
        check_variant(args.cv, yaml_only=True)
        migrate()

        def tailor_one(job_id: int) -> None:
            target, errors, warnings = tailor_cv(job_id, args.cv, max_pages=args.max_pages, fmt=args.format)
            print(target)
            for message in errors:
                print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            print(f"Review {target.parent / 'CV_REVIEW.md'} before using this draft.")

        for_jobs("interested", "tailor", "a tailored CV", tailor_one)
    elif args.command == "cover-letter":
        from .candidate.cover_letter import write_letter
        from .db import migrate
        check_variant(args.cv, yaml_only=False)
        migrate()

        def letter_one(job_id: int) -> None:
            target, errors, warnings = write_letter(job_id, args.cv)
            print(target)
            for message in errors:
                print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            if errors:
                print(f"No cover-letter.docx: fix the letter in {target} first.")
            else:
                print(f"Review {target.parent / 'COVER_LETTER_REVIEW.md'} before sending it.")

        for_jobs("interested", "cover-letter", "a cover letter", letter_one)
    elif args.command == "interview-prep":
        from .candidate.interview import prepare
        from .db import migrate
        migrate()
        for_jobs("interviewing", "interview-prep", "a briefing", lambda job_id: print(prepare(job_id)))
    elif args.command == "skill-gaps":
        from .candidate.skill_gaps import report
        from .db import migrate
        migrate()
        target, skills = report()
        for rank, skill in enumerate(skills[:10], 1):
            print(f"{rank:>2}. {skill['skill']}: {skill['jobs']} job{'s' if skill['jobs'] != 1 else ''}"
                  f", weight {skill['weight']}")
        print(target)
