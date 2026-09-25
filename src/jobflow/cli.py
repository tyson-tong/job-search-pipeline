from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    from .settings import load_env
    load_env()
    from .ingest.options import SITES

    parser = argparse.ArgumentParser(
        prog="jobflow",
        description="First time: init. Everyday: web (job tracker, including your searches), "
        "tailor-cv, and interview-prep. The daily GitHub Action runs migrate and ingest.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create config/.env, config/profile.toml, and config/searches.toml from the "
                   "templates (never overwrites existing files)")
    sub.add_parser("migrate", help="Create or update database tables (the daily Action runs "
                   "this; the other commands do it automatically)")
    ingest = sub.add_parser("ingest", help="Run the searches in config/searches.toml on Apify now and "
                            "import the results (the daily Action runs this; costs Apify credits)")
    ingest.add_argument("--search", help="Only this search name")
    ingest.add_argument("--site", choices=SITES, help="Only this job site")
    web = sub.add_parser("web", help="Open the job tracker in your browser (local only)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    tailor = sub.add_parser("tailor-cv", help="Draft a CV tailored to a job, with a fact check")
    tailor.add_argument("job_id", type=int)
    tailor.add_argument("--cv", help="CV variant name (the part after CV_ in the file name); "
                        "defaults to the best-matching variant")
    prep = sub.add_parser("interview-prep", help="Generate an interview briefing HTML")
    prep.add_argument("job_id", type=int)
    prep.add_argument("--email-file",
                      help="Optional interview email text file, or - for stdin; read for this "
                      "run only and never stored")
    args = parser.parse_args()
    try:
        run(args, parser)
    except RuntimeError as exc:
        # Expected problems (missing settings, unknown job, bad CV) without a traceback.
        raise SystemExit(f"error: {exc}") from None


def warn_possible_duplicates(job_id: int, about_to_apply: bool) -> None:
    """List jobs that may be this role under another employer name or place, loudly if
    one is already in progress, so the same role isn't applied for twice."""
    from .ingest.store import IN_PROGRESS, possible_duplicates

    matches = possible_duplicates(job_id)
    if not matches:
        return
    started = [m for m in matches if m["status"] in IN_PROGRESS]
    if started and about_to_apply:
        print(f"WARNING: this may be the same role as job {started[0]['id']}, which is "
              f"already {started[0]['status']}.")
    print("possible duplicates (similar title and ad text; not merged):")
    for m in matches:
        print(f"  {m['id']}\t{m['status']}\t{m['company'] or ''} — {m['title']}"
              f" ({m['location'] or 'no location'}), {m['similarity']:.0%} same text"
              + (f"\n\t{m['url']}" if m["url"] else ""))


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
    elif args.command == "migrate":
        from .db import migrate
        migrate()
    elif args.command == "ingest":
        from .ingest.apify import ingest_searches
        ingest_searches(args.search, args.site)
    elif args.command == "web":
        from .db import migrate
        from .web.server import serve
        migrate()
        serve(args.port, open_browser=not args.no_browser)
    elif args.command == "tailor-cv":
        from .candidate.tailoring import tailor_cv
        from .db import migrate
        migrate()
        warn_possible_duplicates(args.job_id, about_to_apply=True)
        target, errors, warnings = tailor_cv(args.job_id, args.cv)
        print(target)
        for message in errors:
            print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
        for message in warnings:
            print(f"warning: {message}", file=sys.stderr)
        print(f"Review {target.parent / 'REVIEW_REQUIRED.md'} before using this draft.")
        if errors:
            raise SystemExit(1)
    elif args.command == "interview-prep":
        from .candidate.interview import prepare
        from .db import migrate
        migrate()
        email_text = None
        if args.email_file == "-":
            email_text = sys.stdin.read()
        elif args.email_file:
            email_text = Path(args.email_file).read_text()
        print(prepare(args.job_id, email_text))
