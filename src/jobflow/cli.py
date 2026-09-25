from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Create or update database tables")
    sub.add_parser("ingest", help="Run configured Apify actors and import their datasets")
    load = sub.add_parser("import-json", help="Import an Apify JSON array from a file or stdin")
    load.add_argument("file", nargs="?", default="-")
    listing = sub.add_parser("list", help="List recently seen jobs")
    listing.add_argument("--limit", type=int, default=30)
    match = sub.add_parser("match", help="Score one job against every CV variant")
    match.add_argument("job_id", type=int)
    tailor = sub.add_parser("tailor", help="Create a fact-preserving RenderCV draft")
    tailor.add_argument("job_id", type=int)
    tailor.add_argument("--cv", required=True, help="Variant name: dev, strat, or harvard_cse")
    prep = sub.add_parser("interview-prep", help="Generate a manual interview briefing HTML")
    prep.add_argument("job_id", type=int)
    prep.add_argument("--email-file", required=True,
                      help="Path to interview email text; it is read for this run only")
    args = parser.parse_args()
    if args.command == "migrate":
        from .db import migrate
        migrate()
    elif args.command == "ingest":
        from .apify import ingest_configured_tasks
        ingest_configured_tasks()
    elif args.command == "import-json":
        from .ingest import save_run
        payload = json.load(sys.stdin if args.file == "-" else open(args.file))
        if not isinstance(payload, list):
            raise SystemExit("Expected a JSON array of job objects")
        print(f"Imported {save_run('manual-import', 'manual-' + str(id(payload)), '', payload)} new jobs")
    elif args.command == "list":
        from .db import list_jobs
        for job in list_jobs(args.limit):
            print(f"{job['id']}\t{job['posted_at'] or ''}\t{job['company'] or ''}\t"
                  f"{job['title']}\t{job['location'] or ''}\t{job['status']}")
    elif args.command == "match":
        from .cv import match_job
        for result in sorted(match_job(args.job_id), key=lambda row: row["score"], reverse=True):
            print(f"{result['variant']}: {result['score']}/100 — {result['rationale']}")
            print(json.dumps({"matched": result["matched"], "partial": result["partial"],
                              "gaps": result["gaps"]}, ensure_ascii=False, indent=2))
    elif args.command == "tailor":
        from .cv import tailor_cv
        print(tailor_cv(args.job_id, args.cv))
    elif args.command == "interview-prep":
        from .interview import prepare
        email_text = sys.stdin.read() if args.email_file == "-" else Path(args.email_file).read_text()
        print(prepare(args.job_id, email_text))
