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
    options = sub.add_parser("options", help="List allowed search option values")
    options.add_argument("topic", nargs="?", choices=("countries", "classifications"),
                         help="Show all countries per site, or all Seek classifications")
    load = sub.add_parser("import-json", help="Import an Apify JSON array from a file or stdin")
    load.add_argument("file", nargs="?", default="-")
    listing = sub.add_parser("list", help="List jobs with their best CV match score")
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument("--by-score", action="store_true", help="Sort by best match score")
    listing.add_argument("--status", action="append", choices=STATUSES,
                         help="Only jobs with this status (repeatable)")
    listing.add_argument("--all", action="store_true",
                         help="Include rejected, withdrawn, and ignored jobs")
    status = sub.add_parser("status", help="Show or set a job's application status")
    status.add_argument("job_id", type=int)
    status.add_argument("status", nargs="?", choices=STATUSES)
    status.add_argument("--note", help="Append a dated note, e.g. who you spoke to")
    match = sub.add_parser("match", help="Score jobs against every CV variant")
    match.add_argument("job_id", type=int, nargs="?")
    match.add_argument("--new", action="store_true",
                       help="Match every job without a current assessment for each CV")
    match.add_argument("--limit", type=int, default=50, help="Maximum jobs for --new")
    match.add_argument("--force", action="store_true",
                       help="Re-assess even when the CV is unchanged since the last assessment")
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
    elif args.command == "list":
        from .db import list_jobs
        for job in list_jobs(args.limit, by_score=args.by_score, statuses=args.status,
                             include_closed=args.all):
            score = f"{job['score']} ({job['variant']})" if job["score"] is not None else "-"
            print(f"{job['id']}\t{score}\t{job['posted_at'] or ''}\t{job['company'] or ''}\t"
                  f"{job['title']}\t{job['location'] or ''}\t{job['status']}")
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
    elif args.command == "match":
        from .candidate.matching import (best_variant, format_result, jobs_needing_match,
                                         match_job)
        if (args.job_id is None) == (not args.new):
            parser.error("match needs either a JOB_ID or --new")
        if args.job_id is not None:
            results = sorted(match_job(args.job_id, force=args.force),
                             key=lambda row: row["score"], reverse=True)
            print("\n\n".join(format_result(result) for result in results))
            return
        failures = 0
        for job_id in jobs_needing_match(args.limit):
            try:
                best = best_variant(match_job(job_id, force=args.force))
                print(f"{job_id}\t{best['score']}/100\t{best['variant']}")
            except Exception as exc:
                failures += 1
                print(f"{job_id}\tFAILED\t{exc}", file=sys.stderr)
        if failures:
            raise SystemExit(f"{failures} job(s) could not be matched")
    elif args.command == "tailor-cv":
        from .candidate.tailoring import tailor_cv
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
