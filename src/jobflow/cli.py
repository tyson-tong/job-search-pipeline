from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Create or update database tables")
    sub.add_parser("ingest", help="Run configured Apify actors and import their datasets")
    load = sub.add_parser("import-json", help="Import an Apify JSON array from a file or stdin")
    load.add_argument("file", nargs="?", default="-")
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
