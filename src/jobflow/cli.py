"""The jobflow command: each command's arguments and a thin run_* that imports what it
needs only when run, so every command starts fast. main turns expected problems into a
one-line error and exit status 1."""
from __future__ import annotations

import argparse
import os
import pydoc
import re
import sys
import textwrap


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def port(text: str) -> int:
    """A fixed TCP port: the tracker checks each request's Host against it, so 0 (any free
    port) would turn every request away."""
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be from 1 to 65535")
    return value


def check_variant(variant: str | None, yaml_only: bool) -> str | None:
    """--cv as a CV's short name, which it may also give as the exact file name. Stops
    before any job is tried if cvs in config/profile.toml has a problem, or --cv names none
    of its CVs (for tailor-cv, none that is a RenderCV YAML CV)."""
    from .candidate.cvs import chosen_files, is_yaml

    if variant is None:
        chosen_files()   # still check the setting before any job is tried
        return None
    choices = {name: path for name, path in chosen_files().items()
               if is_yaml(path) or not yaml_only}
    by_file = {path.name: name for name, path in choices.items()}
    if variant not in choices and variant not in by_file:
        raise RuntimeError(f"No {'RenderCV YAML ' if yaml_only else ''}CV {variant!r} among "
                           f"those cvs in config/profile.toml names; choose from "
                           f"{', '.join(by_file) or 'none'}")
    return by_file.get(variant, variant)


def check_keys(reviewer: bool = False, web_search: bool = False) -> None:
    """Stop before any job is tried if a key it needs is missing: otherwise every job
    fails in turn with the same message (after spending its web searches)."""
    from .llm import credentials

    credentials()
    if reviewer:
        credentials(reviewer=True)
    if web_search and not os.environ.get("WEB_SEARCH_API_KEY"):
        raise RuntimeError("Set WEB_SEARCH_API_KEY in config/.env: interview briefings need "
                           "Tavily company research")


DESCRIPTION = ("First time: init. Every day: web, tailor-cv, cover-letter (optional), and "
               "interview-prep. Now and then: skill-gaps. After editing config/searches.toml: "
               "update-searches. The rules each command follows are in docs/rules.md.")


def build_parser() -> argparse.ArgumentParser:
    """The jobflow command line: each command's parser runs its function (args.func)."""
    from .ingest.options import POSTED_WITHIN_DAYS, SITES

    parser = argparse.ArgumentParser(prog="jobflow", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, func, help: str) -> argparse.ArgumentParser:
        # The description too, so each command's -h (and help) opens with what it does.
        parser = sub.add_parser(name, help=help, description=help)
        parser.set_defaults(func=func)
        return parser

    command("init", run_init, "Create your settings files from the templates (first time only)")
    ingest = command("ingest", run_ingest, "Run your searches on Apify now and import the "
                     "results (the daily import does this; costs Apify credits)")
    ingest.add_argument("--search", help="Only this search name")
    ingest.add_argument("--site", choices=SITES, help="Only this job site")
    ingest.add_argument("--posted-within-days", type=int, choices=POSTED_WITHIN_DAYS,
                        metavar="DAYS", help="Every search's posted window (1, 7, 14, or 30 "
                        "days), for this import only")
    update = command("update-searches", run_update_searches, "After editing "
                     "config/searches.toml: check it, update your jobs, and publish it to the "
                     "daily import")
    update.add_argument("--dry-run", action="store_true",
                        help="Check and list what would change; change nothing")
    web = command("web", run_web, "Run the job tracker on this machine")
    web.add_argument("--port", type=port, default=8765, help="Default: 8765")
    tailor = command("tailor-cv", run_tailor_cv, "A tailored, fact-checked CV for each "
                     "Interested job without one")
    tailor.add_argument("--cv", help="The YAML CV to use for every job, by file or short name "
                        "(CV_dev.yaml or dev); default: each job's best-matching one")
    tailor.add_argument("--max-pages", type=positive_int, default=1, metavar="N",
                        help="Pages the CV may take; longer ones are cut to fit (default: 1)")
    tailor.add_argument("--format", choices=("pdf", "docx", "latex"), default="pdf",
                        help="Also write the CV as a .docx or .tex (default: pdf, the YAML and "
                        "its PDF only)")
    letter = command("cover-letter", run_cover_letter, "Optional: a fact-checked cover letter "
                     "for each Interested job without one")
    letter.add_argument("--cv", help="The CV to write from for every job, by file or short "
                        "name; default: each job's best-matching one")
    prep = command("interview-prep", run_interview_prep, "An interview briefing for each "
                   "Interviewing job without one")
    for each_job in (tailor, letter, prep):
        each_job.add_argument("--job", type=positive_int, action="append", metavar="ID",
                              help="Only this job (its number in the job panel); repeat for "
                              "several")
        each_job.add_argument("--redo", action="store_true",
                              help="With --job: do those jobs again, replacing their files")
    command("skill-gaps", run_skill_gaps, "The skills the jobs you're pursuing ask for that "
            "your CVs don't show")
    command("clean-up", run_clean_up, "Delete the output folders of jobs you're done with, "
            "after you confirm")
    command("status", run_status, "What the daily GitHub import uses, and why recent Apify "
            "runs failed")
    commands = dict(sub.choices)   # every command but help itself

    def show_manual(args: argparse.Namespace) -> None:
        pydoc.pager(manual(commands, args.topic))   # paged like man in a terminal

    manual_parser = command("help", show_manual, "Show every command, what it does, and its "
                            "options (or just one command's)")
    manual_parser.add_argument("topic", nargs="?", metavar="COMMAND", choices=list(commands),
                               help="Only this command")
    return parser


def main() -> None:
    import psycopg
    import requests

    from .settings import load_env
    load_env()
    args = build_parser().parse_args()
    try:
        args.func(args)
    except RuntimeError as exc:
        # Expected problems (missing settings, unknown job, bad CV) without a traceback.
        raise SystemExit(f"error: {exc}") from None
    except psycopg.OperationalError as exc:
        raise SystemExit(f"error: can't use the database (check DATABASE_URL): {exc}") from None
    except requests.RequestException as exc:
        raise SystemExit(f"error: a web request failed (check your connection): {exc}") from None
    except KeyboardInterrupt:
        # Ctrl+C: what was saved stays saved, and an open database transaction is rolled back.
        print("\nStopped.", file=sys.stderr)
        raise SystemExit(130) from None


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
  2. cv/                   your CVs (.yaml, .tex, .docx, or .pdf); name 1 or 2 with cvs
                           in profile.toml
  3. config/searches.toml  your job searches (it starts with an example search: replace it)
  4. uv run jobflow web, then the Searches tab to check your searches
  5. GitHub secrets        DATABASE_URL and APIFY_TOKEN (gh secret set NAME), then
                           uv run jobflow update-searches to publish your searches to
                           the daily import (it is already scheduled)
See docs/setup.md for each step.""")


def manual(commands: dict[str, argparse.ArgumentParser], command: str | None = None) -> str:
    """Every command's usage, description, and options, laid out like a manual page; or
    just command's. Made from their parsers, so it always matches what they accept."""
    if command:
        return commands[command].format_help()

    def section(title: str, text: str) -> str:
        return f"{title}\n{textwrap.indent(text.strip(), '    ')}\n"

    def para(text: str) -> str:
        return textwrap.fill(text, 76)

    def entry(name: str) -> str:
        command = commands[name]
        command.formatter_class = lambda prog: argparse.HelpFormatter(prog, width=72)
        usage, _, rest = command.format_help().strip().partition("\n\n")
        # Without "usage: " and -h, which every command has.
        usage = usage.removeprefix("usage: ").replace(" [-h]", "").replace("\n       ", "\n")
        rest = re.sub(r"\n  -h, --help .*", "", rest)
        rest = re.sub(r"\n\noptions:$", "", rest)   # a command with no options of its own
        return f"{name}\n{textwrap.indent(usage + chr(10) * 2 + rest, '    ')}"

    entries = "\n\n".join(entry(name) for name in commands)
    return "\n".join([
        section("NAME", para("jobflow - collect job ads, then triage, score, tailor CVs and "
                             "cover letters, and prepare for interviews")),
        section("SYNOPSIS", "uv run jobflow COMMAND [OPTIONS]\nuv run jobflow help [COMMAND]"),
        section("DESCRIPTION", para(DESCRIPTION)),
        section("COMMANDS", entries),
        section("NOTES", para(
            "Which jobs tailor-cv, cover-letter, and interview-prep do (and do again), which "
            "statuses --job accepts, and every other rule: docs/rules.md.")),
        section("SEE ALSO", "README.md, docs/setup.md, docs/searches.md, docs/rules.md, "
                "docs/structure.html"),
    ])


def run_init(args: argparse.Namespace) -> None:
    init()


def run_ingest(args: argparse.Namespace) -> None:
    from .db import migrate
    from .ingest.apify import ingest_searches
    migrate()
    ingest_searches(args.search, args.site, args.posted_within_days)


def run_update_searches(args: argparse.Namespace) -> None:
    from .github import update_searches
    update_searches(dry_run=args.dry_run)


def run_status(args: argparse.Namespace) -> None:
    from .github import status
    status()


def run_web(args: argparse.Namespace) -> None:
    from .db import migrate, use_pool
    from .ingest.upkeep import archive_old_jobs, prune_archived_jobs, prune_sightings
    from .web.server import serve
    migrate()
    use_pool()   # the page makes several queries a refresh
    # The daily import does these too; this covers days it didn't run.
    archive_old_jobs()
    prune_sightings()
    prune_archived_jobs()
    serve(args.port)


def _print_draft_result(target, errors: list[str], warnings: list[str], if_errors: str,
                        if_checked: str) -> None:
    """What tailor-cv or cover-letter wrote for one job, and what to do next."""
    print(target)
    for message in errors:
        print(f"FACT-CHECK ERROR: {message}", file=sys.stderr)
    for message in warnings:
        print(f"warning: {message}", file=sys.stderr)
    print(if_errors if errors else if_checked)


def check_redo(args: argparse.Namespace) -> None:
    """--redo replaces finished work, so only for the jobs --job names."""
    if args.redo and not args.job:
        raise RuntimeError("--redo needs --job ID, naming the jobs to do again")


def run_tailor_cv(args: argparse.Namespace) -> None:
    from .batch import JobCommand, for_jobs
    from .candidate.matching import PURSUING
    from .candidate.tailoring import add_format, cv_fixed_by_hand, cv_written, tailor_cv
    from .db import migrate
    check_redo(args)
    variant = check_variant(args.cv, yaml_only=True)
    check_keys(reviewer=True)
    migrate()

    def tailor_one(job_id: int) -> None:
        target, errors, warnings = tailor_cv(job_id, variant, max_pages=args.max_pages,
                                             fmt=args.format)
        review = target.parent / "CV_REVIEW.md"
        rendered = target.with_suffix(".pdf").exists()
        _print_draft_result(target, errors, warnings,
                            f"Read {review}. Fix {target.name} yourself and it's kept; otherwise "
                            "the next tailor-cv drafts this job again.",
                            f"Read {review}, then the CV itself, before you send it." if rendered
                            else f"RenderCV couldn't render it (see {review}). Fix {target.name} "
                            "yourself and it's kept; otherwise the next tailor-cv drafts this "
                            "job again.")

    def add_requested_format(folder) -> str | None:
        # A job tailored before gets --format's file from its checked draft, no LLM call.
        written = add_format(folder, args.format)
        return f"wrote {written}" if written else None

    for_jobs(JobCommand(command="tailor-cv", status="interested", job_statuses=PURSUING,
                        kind="tailor",
                        what="a tailored CV", run_one=tailor_one,
                        output_exists=cv_written,
                        fixed_by_hand=cv_fixed_by_hand,
                        done_one=add_requested_format if args.format != "pdf" else None),
             args.job, args.redo)


def run_cover_letter(args: argparse.Namespace) -> None:
    from .batch import JobCommand, for_jobs
    from .candidate.cover_letter import letter_fixed_by_hand, write_letter
    from .candidate.matching import PURSUING
    from .db import migrate
    check_redo(args)
    variant = check_variant(args.cv, yaml_only=False)
    check_keys(reviewer=True)
    migrate()

    def letter_one(job_id: int) -> None:
        target, errors, warnings = write_letter(job_id, variant)
        _print_draft_result(target, errors, warnings,
                            f"No cover-letter.docx: the letter is in {target}. Fix it and save it "
                            "as cover-letter.docx there and it's kept; otherwise the next "
                            "cover-letter drafts this job again.",
                            f"Read {target}, and the notes in "
                            f"{target.parent / 'COVER_LETTER_REVIEW.md'}, before you send it "
                            "with your application.")

    for_jobs(JobCommand(command="cover-letter", status="interested", job_statuses=PURSUING,
                        kind="cover-letter",
                        what="a cover letter", run_one=letter_one,
                        output_exists=lambda folder: (folder / "cover-letter.docx").exists(),
                        fixed_by_hand=letter_fixed_by_hand),
             args.job, args.redo)


def run_interview_prep(args: argparse.Namespace) -> None:
    from .batch import JobCommand, for_jobs
    from .candidate.cvs import chosen_sources
    from .candidate.interview import prepare
    from .db import migrate
    check_redo(args)
    chosen_sources()  # a problem with cvs in config/profile.toml stops it now
    check_keys(web_search=True)
    migrate()
    for_jobs(JobCommand(command="interview-prep", status="interviewing", kind="interview-prep",
                        what="a briefing", run_one=lambda job_id: print(prepare(job_id)),
                        output_exists=lambda folder: (folder / "interview-prep.html").exists()),
             args.job, args.redo)


def run_clean_up(args: argparse.Namespace) -> None:
    from .cleanup import clean_up
    from .db import migrate
    migrate()
    clean_up()


def run_skill_gaps(args: argparse.Namespace) -> None:
    from .candidate.skill_gaps import outdated_note, report
    from .db import migrate
    from .text import plural
    check_keys()
    migrate()
    target, skills, outdated = report()
    if outdated:
        print(f"warning: {outdated_note(outdated)}", file=sys.stderr)
    for rank, skill in enumerate(skills[:10], 1):
        print(f"{rank:>2}. {skill['skill']}: {plural(skill['jobs'], 'job')}, "
              f"weight {skill['weight']}")
    print(target)
