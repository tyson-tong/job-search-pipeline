from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


def main() -> None:
    from .settings import load_env
    load_env()
    from .db import STATUSES
    from .ingest.options import SITES

    parser = argparse.ArgumentParser(prog="jobflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Create or update database tables")
    ingest = sub.add_parser("ingest", help="Run the searches in searches.toml on Apify and "
                            "import the results (costs Apify credits)")
    ingest.add_argument("--search", help="Only this search name")
    ingest.add_argument("--site", choices=SITES, help="Only this job site")
    preview = sub.add_parser("searches", help="Check searches.toml and show the Apify runs "
                             "it would make, without running anything")
    preview.add_argument("--search", help="Only this search name")
    preview.add_argument("--site", choices=SITES, help="Only this job site")
    preview.add_argument("--json", action="store_true", help="Print full actor inputs")
    web = sub.add_parser("web", help="Open the job tracker in your browser (local only)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    options = sub.add_parser("options", help="List allowed search option values")
    options.add_argument("topic", nargs="?", choices=("countries", "classifications"),
                         help="Show all countries per site, or all Seek classifications")
    load = sub.add_parser("import-json", help="Import an Apify JSON array from a file or stdin")
    load.add_argument("file", nargs="?", default="-")
    status = sub.add_parser("status", help="Show or set a job's application status")
    status.add_argument("job_id", type=int)
    status.add_argument("status", nargs="?", choices=STATUSES)
    status.add_argument("--note", help="Append a dated note, e.g. who you spoke to")
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


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "migrate":
        from .db import migrate
        migrate()
    elif args.command == "ingest":
        from .ingest.apify import ingest_searches
        ingest_searches(args.search, args.site)
    elif args.command == "searches":
        from .ingest.report import print_plan
        from .ingest.searches import load_searches, plan
        print_plan(plan(load_searches(), args.search, args.site), show_json=args.json)
    elif args.command == "web":
        from .web.server import serve
        serve(args.port, open_browser=not args.no_browser)
    elif args.command == "options":
        from .ingest.report import print_options
        print_options(args.topic)
    elif args.command == "import-json":
        from .ingest.store import save_run
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise SystemExit("Expected a JSON array of job objects")
        source = "stdin" if args.file == "-" else Path(args.file).name
        added = save_run(f"manual-import:{source}", f"manual-{uuid.uuid4()}", None, payload)
        print(f"Imported {len(payload)} items, {added} new jobs")
    elif args.command == "status":
        from .db import job_status
        job = job_status(args.job_id, args.status, args.note)
        print(f"{job['id']}\t{job['company'] or ''} — {job['title']}")
        print(f"status: {job['status']} (since {job['status_updated_at']:%Y-%m-%d})"
              + (f", applied {job['applied_at']:%Y-%m-%d}" if job["applied_at"] else ""))
        if job["status"] == "filtered" and job["filter_reason"]:
            print(f"filtered on import: {job['filter_reason']}")
        if job["notes"]:
            print("notes:\n" + "\n".join(f"  {line}" for line in job["notes"].splitlines()))
        warn_possible_duplicates(args.job_id, about_to_apply=args.status in
                                 ("interested", "applied"))
    elif args.command == "tailor-cv":
        from .candidate.tailoring import tailor_cv
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
        email_text = None
        if args.email_file == "-":
            email_text = sys.stdin.read()
        elif args.email_file:
            email_text = Path(args.email_file).read_text()
        print(prepare(args.job_id, email_text))
