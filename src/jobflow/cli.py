from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

# Jobs for_jobs works on at once. More would be faster, but each job makes several LLM
# calls sending whole CVs and ads, and providers cap tokens per minute (rate-limited
# calls are retried; see llm.complete).
JOBS_AT_ONCE = 2


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def check_variant(variant: str | None, yaml_only: bool) -> str | None:
    """--cv as a CV's short name, which it may also give as the exact file name. Stops
    before any job is tried if cvs in config/profile.toml has a problem, or --cv names none
    of its CVs (for tailor-cv, none that is a RenderCV YAML CV)."""
    from .candidate.cvs import chosen_files, is_yaml

    choices = {name: path for name, path in chosen_files().items() if is_yaml(path) or not yaml_only}
    by_file = {path.name: name for name, path in choices.items()}
    if variant is not None and variant not in choices and variant not in by_file:
        raise RuntimeError(f"No {'RenderCV YAML ' if yaml_only else ''}CV {variant!r} among "
                           f"those cvs in config/profile.toml names; choose from "
                           f"{', '.join(by_file) or 'none'}")
    return by_file.get(variant, variant)


def check_keys(reviewer: bool = False, web_search: bool = False) -> None:
    """Stop before any job is tried if a key it needs is missing: otherwise every job
    fails in turn with the same message (after spending its web searches)."""
    import os

    from .llm import credentials

    credentials()
    if reviewer:
        credentials(reviewer=True)
    if web_search and not os.environ.get("WEB_SEARCH_API_KEY"):
        raise RuntimeError("Set WEB_SEARCH_API_KEY in config/.env: interview briefings need "
                           "Tavily company research")


def main() -> None:
    import psycopg
    import requests

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
    web = sub.add_parser("web", help="Run the local job tracker web server")
    web.add_argument("--port", type=int, default=8765,
                     help="Port to serve the tracker on (default: 8765)")
    tailor = sub.add_parser("tailor-cv", help="Draft a tailored CV, reviewed and fact-checked, "
                            "for every Interested job that doesn't have one yet")
    tailor.add_argument("--cv", help="RenderCV YAML CV to use for every job, one of those cvs "
                        "in config/profile.toml names, by file name or short name (CV_dev.yaml "
                        "or dev); defaults to each job's best-matching YAML CV (an unscored "
                        "job is scored first)")
    tailor.add_argument("--max-pages", type=positive_int, default=1, metavar="N",
                        help="Longest the CV may be as RenderCV lays it out; longer ones have "
                        "their least relevant bullets cut until they fit (default: 1)")
    tailor.add_argument("--format", choices=("pdf", "docx", "latex"), default="pdf",
                        help="The YAML draft and its PDF, rendered by RenderCV, are always "
                        "written; docx also writes a .docx, and latex a .tex to compile. "
                        "Default: pdf")
    letter = sub.add_parser("cover-letter", help="Optional: draft a cover letter, reviewed and "
                            "fact-checked, for every Interested job that doesn't have one yet")
    letter.add_argument("--cv", help="CV to write from, in any format, for every job, one of "
                        "those cvs in config/profile.toml names, by file name or short name; "
                        "defaults to each job's best-matching CV (an unscored job is scored "
                        "first)")
    sub.add_parser("interview-prep", help="Generate an interview briefing HTML for every "
                   "Interviewing job that doesn't have one yet")
    sub.add_parser("skill-gaps", help="Report the skills your CVs don't show that scored "
                   "Interested jobs ask for most, with how to close each gap")
    sub.add_parser("clean-up", help="Delete the output folders of Not interested, Withdrawn, "
                   "Declined, and Rejected jobs, after asking you to confirm (the jobs stay in "
                   "the database)")
    manual = sub.add_parser("help", help="Show every command, what it does, and its options "
                            "(or just one command's)")
    manual.add_argument("topic", nargs="?", metavar="COMMAND",
                        choices=[name for name in sub.choices if name != "help"],
                        help="Only this command")
    # Each command's -h (and help) opens with what it does, not just its options.
    for action in sub._choices_actions:
        sub.choices[action.dest].description = action.help
    args = parser.parse_args()
    try:
        run(args, parser)
    except RuntimeError as exc:
        # Expected problems (missing settings, unknown job, bad CV) without a traceback.
        raise SystemExit(f"error: {exc}") from None
    except psycopg.OperationalError as exc:
        raise SystemExit(f"error: can't use the database (check DATABASE_URL): {exc}") from None
    except requests.RequestException as exc:
        raise SystemExit(f"error: a web request failed (check your connection): {exc}") from None


class _JobOutput:
    """Stands in for sys.stdout and sys.stderr so that what a job prints, from anywhere,
    is held back and printed in one piece when it finishes, not interleaved with the
    other job running at the same time."""

    def __init__(self, stream):
        self.stream, self.held = stream, threading.local()

    def write(self, text: str) -> int:
        held = getattr(self.held, "lines", None)
        if held is None:
            return self.stream.write(text)
        held.append((self.stream, text))
        return len(text)

    def __getattr__(self, name):
        return getattr(self.stream, name)


# The command each workflow_runs kind is run by, for messages.
COMMANDS = {"tailor": "tailor-cv"}


def for_jobs(status: str, kind: str, what: str, run_one, output_exists,
             fixed_by_hand=None, done_one=None) -> None:
    """_run_jobs, unless the same command is already running (in another terminal, say),
    which would draft the same jobs twice and write over its files.

    A Postgres advisory lock, held on its own connection, marks the command as running;
    Postgres releases it when that connection closes, so even a killed run leaves none.
    (It needs a direct database connection: a pooler in transaction mode would lose it.)"""
    from .db import connect

    with connect() as lock:
        lock.autocommit = True
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                            (f"jobflow {kind}",)).fetchone()[0]:
            raise SystemExit(f"{COMMANDS.get(kind, kind)} is already running (in another "
                             "terminal or window). Let it finish, then run it again for any "
                             "jobs it didn't do.")
        _run_jobs(status, kind, what, run_one, output_exists, fixed_by_hand, done_one)


def _run_jobs(status: str, kind: str, what: str, run_one, output_exists,
              fixed_by_hand=None, done_one=None) -> None:
    """Run run_one(job_id) for each job with `status` not yet done, JOBS_AT_ONCE at a time.

    A run is recorded in workflow_runs only once it finishes, so a job is never done
    twice, and a failed job is retried next time. A job counts as done only while
    output_exists(its folder) too: one whose files were deleted (by clean-up, say) is
    done again. A run whose draft failed the fact check (NEEDS-FIX) is done again, for a
    fresh draft, unless fixed_by_hand(its folder) says you fixed that draft yourself:
    then it's recorded as FIXED-BY-HAND and kept. done_one(folder), if given, runs for
    each job already done and returns a line to print, or None. One failure doesn't stop
    the rest, unless the LLM provider refuses every request (no credit left, a bad key).
    """
    from .candidate.paths import job_directory
    from .db import connect
    from .llm import LLMError

    with connect() as conn:
        rows = conn.execute(
            "SELECT j.id, j.company, j.title, "
            "EXISTS (SELECT 1 FROM workflow_runs r WHERE r.job_id=j.id AND r.kind=%s "
            "AND r.status<>'NEEDS-FIX'), "
            "EXISTS (SELECT 1 FROM workflow_runs r WHERE r.job_id=j.id AND r.kind=%s) "
            "FROM jobs j WHERE j.status=%s ORDER BY j.id", (kind, kind, status)).fetchall()
        todo, done_jobs, redo, lost = [], [], 0, 0
        for job_id, company, title, done, tried in rows:
            name = f"#{job_id} {title} ({company or 'unknown employer'})"
            folder = job_directory(job_id, {"company": company, "title": title})
            if done and output_exists(folder):
                done_jobs.append((name, folder))
                continue
            if done:
                lost += 1
            elif tried and fixed_by_hand and fixed_by_hand(folder):
                conn.execute("INSERT INTO workflow_runs(job_id,kind,status,finished_at) "
                             "VALUES (%s,%s,'FIXED-BY-HAND',now())", (job_id, kind))
                print(f"{name}: you fixed its draft yourself, so it's kept and not drafted again")
                done_jobs.append((name, folder))
                continue
            elif tried:
                redo += 1
            todo.append((job_id, company, title))
    again = [f"{redo} drafted again: the last draft failed the fact check"] if redo else []
    again += [f"{lost} done again: their files were deleted"] if lost else []
    print(f"{len(rows)} {status.capitalize()} job{'s' if len(rows) != 1 else ''}: "
          f"{len(rows) - len(todo)} already have {what}, {len(todo)} to do"
          + (f" ({'; '.join(again)})" if again else ""))
    for name, folder in done_jobs if done_one else []:
        try:
            line = done_one(folder)
        except RuntimeError as exc:
            print(f"{name}: warning: {exc}", file=sys.stderr)
        else:
            if line:
                print(f"{name}: {line}")
    out, err = _JobOutput(sys.stdout), _JobOutput(sys.stderr)
    stop, lock = threading.Event(), threading.Lock()
    tried, failed = [], []

    def one(row) -> None:
        job_id, company, title = row
        if stop.is_set():
            return
        out.held.lines = err.held.lines = lines = []
        print(f"\n#{job_id} {title} ({company or 'unknown employer'})")
        try:
            run_one(job_id)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            with lock:
                failed.append(job_id)
            if isinstance(exc, LLMError) and exc.fatal:
                stop.set()
        finally:
            out.held.lines = err.held.lines = None
            with lock:
                tried.append(job_id)
                for stream, text in lines:  # flushed, so stdout and stderr stay in order
                    stream.write(text)
                    stream.flush()

    sys.stdout.flush()
    sys.stdout, sys.stderr = out, err
    try:
        with ThreadPoolExecutor(max_workers=JOBS_AT_ONCE) as pool:
            list(pool.map(one, todo))
    finally:
        sys.stdout, sys.stderr = out.stream, err.stream
    if stop.is_set():
        raise SystemExit(f"Stopped: the LLM provider will refuse the other "
                         f"{len(todo) - len(tried)} job(s) too. Fix that, then run the "
                         "command again")
    if failed:
        raise SystemExit(f"{len(failed)} of {len(todo)} failed; run the command again to retry them")


def update_searches(dry_run: bool = False) -> None:
    """Check config/searches.toml, publish it as the JOBFLOW_SEARCHES GitHub variable that
    the daily import reads, then filter Inbox jobs from its excluded employers or matching
    its excluded title terms (imports only apply them to the jobs they bring in). An invalid file stops before
    anything is published, so a typo never reaches the daily import."""
    from .db import migrate
    from .ingest.searches import parse_config, search_warnings
    from .ingest.store import exclude_inbox_jobs
    from .settings import ROOT, searches_file

    path = searches_file()
    if not path.exists():
        raise RuntimeError(f"No searches file at {path}. Run `uv run jobflow init` first")
    text = path.read_text()
    searches = parse_config(text)
    print(f"{path}: valid ({len(searches)} search{'es' if len(searches) != 1 else ''})")
    # [defaults] reach every search, so the same warning can come from each: show it once.
    sys.stdout.flush()
    for warning in dict.fromkeys(w for s in searches for w in search_warnings(s)):
        print(f"warning: {warning}", file=sys.stderr)
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


# The statuses whose jobs' output folders clean-up deletes, with how it names them. Only a
# job moved to Not interested from My jobs has a folder: Inbox jobs are never worked on.
CLEAN_UP_STATUSES = {"not_interested": "Not interested", "withdrawn": "Withdrawn",
                     "declined": "Declined", "rejected": "Rejected"}


def clean_up() -> None:
    """Delete the output folders (CVs, letters, reviews, briefings) of jobs that are
    Not interested, Withdrawn, Declined, or Rejected, once you confirm. The jobs, their notes, and their
    history stay in the database."""
    from .db import connect
    from .settings import output_directory

    root = output_directory()

    def closed_jobs(ids: list[int] | None = None) -> list[tuple[int, str]]:
        with connect() as conn:
            return conn.execute(
                "SELECT id, status FROM jobs WHERE status = ANY(%s) "
                "AND (%s::bigint[] IS NULL OR id = ANY(%s)) ORDER BY id",
                (list(CLEAN_UP_STATUSES), ids, ids)).fetchall()

    # A job's folder is output/<company>/<role>-<job id>, found by its id, so a folder is
    # still found if the company or title has changed since it was written.
    folders = [(folder, job_id, status) for job_id, status in closed_jobs()
               for folder in sorted(root.glob(f"*/*-{job_id}")) if folder.is_dir()]
    if not folders:
        print("Nothing to clean up: no Not interested, Withdrawn, Declined, or Rejected job has a "
              f"folder in {root}")
        return
    for folder, _, status in folders:
        print(f"  {CLEAN_UP_STATUSES[status]:<14}  {folder.relative_to(root)}")
    try:
        answer = input(f"Delete these {len(folders)} folder{'s' if len(folders) != 1 else ''} "
                       "and everything in them? This can't be undone. (Y/N) ")
    except EOFError:
        answer = ""
    if answer.strip().casefold() not in ("y", "yes"):
        print("Nothing deleted")
        return
    # A job may have moved on (in the tracker, say) while you were deciding: keep its folder.
    still_closed = {job_id for job_id, _ in closed_jobs([job_id for _, job_id, _ in folders])}
    deleted = 0
    for folder, job_id, _ in folders:
        if job_id not in still_closed:
            print(f"  kept {folder.relative_to(root)}: its job's status has changed since")
            continue
        shutil.rmtree(folder)
        deleted += 1
        if not any(folder.parent.iterdir()):
            folder.parent.rmdir()   # the company's folder, now empty
    print(f"Deleted {deleted} folder{'s' if deleted != 1 else ''}")


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
  2. cv/                   your CVs (.yaml, .tex, .docx, or .pdf); name 1 or 2 with cvs in profile.toml
  3. config/searches.toml  your job searches (it starts with example searches: replace them)
  4. uv run jobflow web, then the Searches tab to check your searches""")


def manual(parser: argparse.ArgumentParser, command: str | None = None) -> str:
    """Every command's usage, description, and options, laid out like a manual page; or
    just command's. Made from the parser, so it always matches what the commands accept."""
    import re
    import textwrap

    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if command:
        return sub.choices[command].format_help()

    def section(title: str, text: str) -> str:
        return f"{title}\n{textwrap.indent(text.strip(), '    ')}\n"

    def para(text: str) -> str:
        return textwrap.fill(text, 76)

    def entry(name: str) -> str:
        command = sub.choices[name]
        command.formatter_class = lambda prog: argparse.HelpFormatter(prog, width=72)
        usage, _, rest = command.format_help().strip().partition("\n\n")
        # Without "usage: " and -h, which every command has.
        usage = usage.removeprefix("usage: ").replace(" [-h]", "").replace("\n       ", "\n")
        rest = re.sub(r"\n  -h, --help .*", "", rest)
        rest = re.sub(r"\n\noptions:$", "", rest)   # a command with no options of its own
        return f"{name}\n{textwrap.indent(usage + chr(10) * 2 + rest, '    ')}"

    commands = "\n\n".join(entry(name) for name in sub.choices if name != "help")
    return "\n".join([
        section("NAME", para("jobflow - collect job ads, then triage, score, tailor CVs and "
                             "cover letters, and prepare for interviews")),
        section("SYNOPSIS", "uv run jobflow COMMAND [OPTIONS]\nuv run jobflow help [COMMAND]"),
        section("DESCRIPTION", para(parser.description)),
        section("COMMANDS", commands),
        section("NOTES", para(
            "tailor-cv, cover-letter, and interview-prep work on two jobs at a time and skip "
            "jobs they've already done (a job that failed, whose draft failed the fact "
            "check, or whose folder was deleted is tried again). Only one run of each can go at a time. Every command "
            "except init and help brings the database tables up to date when it starts.")),
        section("SEE ALSO", "README.md, docs/setup.md, docs/searches.md, docs/rules.md, "
                "docs/structure.html"),
    ])


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "help":
        import pydoc
        pydoc.pager(manual(parser, args.topic))   # paged like man in a terminal
    elif args.command == "init":
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
        serve(args.port)
    elif args.command == "tailor-cv":
        from .candidate.tailoring import add_format, cv_fixed_by_hand, tailor_cv
        from .db import migrate
        args.cv = check_variant(args.cv, yaml_only=True)
        check_keys(reviewer=True)
        migrate()

        def tailor_one(job_id: int) -> None:
            target, errors, warnings = tailor_cv(job_id, args.cv, max_pages=args.max_pages, fmt=args.format)
            print(target)
            for message in errors:
                print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            if errors:
                print(f"Read {target.parent / 'CV_REVIEW.md'}. Fix {target.name} yourself and it's "
                      "kept; otherwise the next tailor-cv drafts this job again.")
            else:
                print(f"Read {target.parent / 'CV_REVIEW.md'}, then the CV itself, before you "
                      "send it.")

        def add_requested_format(folder) -> str | None:
            # A job tailored before gets --format's file from its checked draft, no LLM call.
            written = add_format(folder, args.format)
            return f"wrote {written}" if written else None

        for_jobs("interested", "tailor", "a tailored CV", tailor_one,
                 lambda folder: any(folder.glob("CV_*_draft.yaml")), cv_fixed_by_hand,
                 add_requested_format if args.format != "pdf" else None)
    elif args.command == "cover-letter":
        from .candidate.cover_letter import letter_fixed_by_hand, write_letter
        from .db import migrate
        args.cv = check_variant(args.cv, yaml_only=False)
        check_keys(reviewer=True)
        migrate()

        def letter_one(job_id: int) -> None:
            target, errors, warnings = write_letter(job_id, args.cv)
            print(target)
            for message in errors:
                print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
            for message in warnings:
                print(f"warning: {message}", file=sys.stderr)
            if errors:
                print(f"No cover-letter.docx: the letter is in {target}. Fix it and save it as "
                      "cover-letter.docx there and it's kept; otherwise the next cover-letter "
                      "drafts this job again.")
            else:
                print(f"Read {target}, and the notes in {target.parent / 'COVER_LETTER_REVIEW.md'}, "
                      "before you send it with your application.")

        for_jobs("interested", "cover-letter", "a cover letter", letter_one,
                 lambda folder: (folder / "cover-letter.docx").exists(), letter_fixed_by_hand)
    elif args.command == "interview-prep":
        from .candidate.cvs import chosen_sources
        from .candidate.interview import prepare
        from .db import migrate
        chosen_sources()  # a problem with cvs in config/profile.toml stops it now
        check_keys(web_search=True)
        migrate()
        for_jobs("interviewing", "interview-prep", "a briefing", lambda job_id: print(prepare(job_id)),
                 lambda folder: (folder / "interview-prep.html").exists())
    elif args.command == "clean-up":
        from .db import migrate
        migrate()
        clean_up()
    elif args.command == "skill-gaps":
        from .candidate.skill_gaps import report
        from .db import migrate
        check_keys()
        migrate()
        target, skills = report()
        for rank, skill in enumerate(skills[:10], 1):
            print(f"{rank:>2}. {skill['skill']}: {skill['jobs']} job{'s' if skill['jobs'] != 1 else ''}"
                  f", weight {skill['weight']}")
        print(target)
