"""The daily GitHub import, seen from your machine: publishing your searches and timezone
to it (update-searches), and reading back what it uses (status), with the GitHub CLI."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime

from .text import job_label, plural

# The daily import's workflow file, the secrets it needs, and what GitHub's workflow states
# mean for it.
WORKFLOW = "daily-ingest.yml"
ACTIONS_SECRETS = ("DATABASE_URL", "APIFY_TOKEN")
WORKFLOW_STATES = {"active": "on", "disabled_manually": "off",
                   "disabled_inactivity": "off (GitHub turned it off after 60 days without "
                                          "activity in the repository)"}
# How the import's runs start: on time by your outside cron service (or the Run workflow
# button), and GitHub's schedule, the fallback, which imports nothing after a recent on-time run.
RUN_EVENTS = {"workflow_dispatch": "Last run started on time (or by hand)",
              "schedule": "Last scheduled run (the fallback)"}


def _gh(args: list[str], doing: str, input: str | None = None, hint: str = "") -> str:
    """Run the GitHub CLI in this checkout, where it finds the repository from the Git
    remote, and return what it prints. A failure raises RuntimeError saying what was
    being done."""
    from .settings import ROOT

    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError(f"GitHub CLI (gh) not found. Install it and run `gh auth login`{hint}")
    result = subprocess.run([gh, *args], input=input, text=True, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"{doing} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def update_searches(dry_run: bool = False) -> None:
    """Check config/searches.toml, then do two separate things with it: bring the jobs you
    already have in line (filter Inbox jobs its filters would now filter, as imports apply
    them only to the jobs they bring in, and bring back Filtered jobs they would now keep),
    and publish it, with your timezone, as the GitHub variables the daily import reads.

    An invalid file stops before either, so a typo never reaches the daily import or your
    jobs. Otherwise each goes ahead even if the other fails (no GitHub CLI, say, or a
    database that can't be reached), and the command then stops with an error naming what
    failed: run it again once that's fixed."""
    import psycopg

    from .ingest.filters import search_warnings
    from .ingest.searches import parse_config
    from .settings import searches_file

    path = searches_file()
    if not path.exists():
        raise RuntimeError(f"No searches file at {path}. Run `uv run jobflow init` first")
    text = path.read_text()
    searches = parse_config(text)
    print(f"{path}: valid ({plural(len(searches), 'search', 'es')})")
    # [defaults] reach every search, so the same warning can come from each: show it once.
    sys.stdout.flush()
    for warning in dict.fromkeys(w for s in searches for w in search_warnings(s)):
        print(f"warning: {warning}", file=sys.stderr)
    failed = []
    try:
        _refilter_jobs(text, searches, dry_run)
    except (RuntimeError, psycopg.Error) as exc:
        failed.append(f"your jobs weren't updated ({str(exc).strip()})")
    try:
        _publish_searches(text, dry_run)
    except RuntimeError as exc:
        failed.append(f"the GitHub variables weren't updated ({exc})")
    if failed:
        raise RuntimeError("; ".join(failed) + ". Fix that, then run update-searches again")


def _refilter_jobs(text: str, searches: list, dry_run: bool) -> None:
    """Filter the Inbox jobs the searches would now filter, and bring back the Filtered jobs
    they would now keep; with dry_run, only list them."""
    from .db import migrate, pending_migrations
    from .ingest.refilter import filter_inbox_jobs, restore_filtered_jobs
    from .ingest.searches import defaults_search

    if not dry_run:
        migrate()
    elif pending := pending_migrations():
        # A migration can drop columns: a dry run must change nothing, so it stops here.
        raise RuntimeError(f"{plural(len(pending), 'database migration')} not applied yet "
                           f"({', '.join(pending)}), and a dry run changes nothing. Any other "
                           "jobflow command (such as jobflow web) applies them; then run the "
                           "dry run again")
    # A job found by a search since renamed or removed is judged by [defaults].
    fallback = defaults_search(text)
    matched, checked = filter_inbox_jobs(searches, dry_run=dry_run, fallback=fallback)
    for job in matched:
        print(f"  {job_label(job['id'], job['title'], job['company'])}: {job['reason']}")
    print(f"{'Would filter' if dry_run else 'Filtered'} {len(matched)} of {checked} Inbox jobs"
          f"{' (dry run: none filtered)' if dry_run else ''}")
    restored, checked = restore_filtered_jobs(searches, dry_run=dry_run, fallback=fallback)
    for job in restored:
        print(f"  {job_label(job['id'], job['title'], job['company'])}: "
              f"was filtered: {job['reason']}")
    print(f"{'Would bring' if dry_run else 'Brought'} {len(restored)} of {checked} Filtered jobs "
          f"back to the Inbox{' (dry run: none moved)' if dry_run else ''}")


def _publish_searches(text: str, dry_run: bool) -> None:
    """Set the JOBFLOW_SEARCHES and JOBFLOW_TIMEZONE GitHub variables the daily import reads:
    it has no config/profile.toml, so it gets your timezone this way too."""
    from .settings import timezone

    zone = timezone().key
    if dry_run:
        print(f"Would update the JOBFLOW_SEARCHES and JOBFLOW_TIMEZONE ({zone}) GitHub variables "
              "(dry run: not updated)")
        return
    _gh(["variable", "set", "JOBFLOW_SEARCHES"], "Updating the JOBFLOW_SEARCHES GitHub "
        "variable", input=text,
        hint=", or set the variable by hand: gh variable set JOBFLOW_SEARCHES < "
        "config/searches.toml")
    _gh(["variable", "set", "JOBFLOW_TIMEZONE", "--body", zone], "Updating the "
        "JOBFLOW_TIMEZONE GitHub variable (JOBFLOW_SEARCHES was updated)",
        hint=f", or set the variable by hand: gh variable set JOBFLOW_TIMEZONE --body {zone}")
    print(f"Updated the JOBFLOW_SEARCHES and JOBFLOW_TIMEZONE ({zone}) GitHub variables: the "
          "daily import uses them now")


def _print_published_searches(text: str | None) -> dict[str, list[str]]:
    """Print whether the published JOBFLOW_SEARCHES can be used and matches
    config/searches.toml; return {site: the searches that call it}."""
    from .ingest.searches import SearchConfigError, parse_config
    from .settings import searches_file

    used: dict[str, list[str]] = {}
    if not text:
        print("Searches: JOBFLOW_SEARCHES not set: run `uv run jobflow update-searches`")
    else:
        try:
            searches = parse_config(text)
        except SearchConfigError as exc:
            print(f"Searches: JOBFLOW_SEARCHES can't be used ({exc}): fix config/searches.toml and "
                  "run `uv run jobflow update-searches`")
            searches = []
        for search in searches:
            for site in search.sites:
                used.setdefault(site, []).append(search.name)
        if searches:
            try:
                local = searches_file()
                # GitHub drops the file's final newline when it stores the variable.
                same = local.exists() and local.read_text().strip() == text.strip()
            except RuntimeError:  # no config/profile.toml
                same = None
            print(f"Searches: JOBFLOW_SEARCHES, {plural(len(searches), 'search', 'es')}"
                  + ("" if same is None else " (same as config/searches.toml)" if same
                     else " (differs from config/searches.toml: run "
                          "`uv run jobflow update-searches`)"))
    return used


def _print_recent_runs() -> None:
    """What your database says about the latest imports: when one last succeeded, each site
    whose Apify runs failed lately, with the error (GitHub only says the job failed), runs
    whose results are waiting to be imported again, and each site whose runs hit
    max_results. Reads only; a database that can't be read is a note, not a failed status."""
    import psycopg

    from .db import connect
    from .ingest.store import RECENT_RUNS, recent_capped_runs, recent_failed_runs
    from .settings import timezone

    try:
        with connect() as conn:
            last = conn.execute("SELECT max(finished_at) FROM scraper_runs "
                                "WHERE status = 'SUCCEEDED'").fetchone()[0]
            failed = recent_failed_runs(conn)
            unsaved = conn.execute("""SELECT count(*) FROM scraper_runs
                                      WHERE status = 'IMPORT_FAILED'
                                        AND dataset_id IS NOT NULL""").fetchone()[0]
            try:
                with conn.transaction():
                    capped = recent_capped_runs(conn)
            except psycopg.errors.UndefinedColumn:   # status doesn't migrate: none recorded yet
                capped = []
    except (RuntimeError, psycopg.Error) as exc:
        print(f"Runs:     can't read them from your database ({str(exc).strip()})")
        return
    hours = round(RECENT_RUNS.total_seconds() / 3600)
    print("Runs:     " + (f"last succeeded {last.astimezone(timezone()):%Y-%m-%d %H:%M %Z}"
                          if last else "none has succeeded yet")
          + ("" if failed else f"; none failed in the last {hours} hours"))
    for f in failed:
        print(f"  {f['site']:<9} {f['failed']} of {plural(f['total'], 'run')} failed in the last "
              f"{hours} hours: {f['error'] or 'no error recorded'}")
    if unsaved:
        print(f"  {plural(unsaved, 'run')} succeeded on Apify but couldn't be saved: run "
              "`uv run jobflow ingest --retry-imports` to import their results")
    for c in capped:
        print(f"  {c['site']:<9} {c['capped']} of {plural(c['total'], 'run')} hit max_results in "
              f"the last {hours} hours, so probably left jobs behind: raise max_results, or "
              f"narrow {', '.join(c['searches']) or 'the search'}")


def status() -> None:
    """Show what the daily GitHub import uses, reading it from GitHub: whether it's on, its
    last run started on time and its last scheduled one, its secrets, its searches (and
    whether they match config/searches.toml), and which job sites (Apify scrapers) it calls;
    and from your database, why recent Apify runs failed. Changes nothing."""
    from .ingest.options import ACTORS, SITES
    from .settings import timezone

    state = _gh(["api", f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}", "--jq", ".state"],
                "Reading the daily import's workflow").strip()
    runs = json.loads(_gh(["run", "list", "--workflow", WORKFLOW, "-L", "20", "--json",
                           "status,conclusion,createdAt,event"], "Reading the daily import's runs"))
    secrets = {s["name"]: s["updatedAt"][:10] for s in json.loads(_gh(
        ["secret", "list", "--json", "name,updatedAt"], "Reading the repository's secret names"))}
    variables = {v["name"]: v["value"] for v in json.loads(_gh(
        ["variable", "list", "--json", "name,value"],
        "Reading the repository's Actions variables"))}
    print(f"Daily import (.github/workflows/{WORKFLOW}): {WORKFLOW_STATES.get(state, state)}")
    latest: dict[str, dict] = {}   # the newest run of each way of starting, newest first
    for run in runs:
        latest.setdefault(run["event"], run)
    for event, run in latest.items():
        started = datetime.fromisoformat(run["createdAt"]).astimezone(timezone())
        print(f"  {RUN_EVENTS.get(event, f'Last {event} run')}: {started:%Y-%m-%d %H:%M %Z}, "
              f"{run['conclusion'] or run['status']}")
    if not runs:
        print("  Last run: none yet")
    if "workflow_dispatch" not in latest:
        print("  No run started on time lately: GitHub's schedule starts the import hours "
              "late, in the morning, and Seek's 1-day search returns only that day's jobs. "
              "Start it on time at night: docs/setup.md, section 7")
    _print_recent_runs()
    print("Secrets:  " + ", ".join(f"{name} set {secrets[name]}" if name in secrets
                                   else f"{name} MISSING (gh secret set {name})"
                                   for name in ACTIONS_SECRETS))
    used = _print_published_searches(variables.get("JOBFLOW_SEARCHES"))
    published, zone = variables.get("JOBFLOW_TIMEZONE"), timezone().key
    print(f"Timezone: JOBFLOW_TIMEZONE {published}"
          + (" (same as yours)" if published == zone
             else f" (yours is {zone}: run `uv run jobflow update-searches`)")
          if published else "Timezone: JOBFLOW_TIMEZONE not set, so the daily import uses UTC: "
          "run `uv run jobflow update-searches`")
    print("Sites (the Apify scrapers the import calls):")
    for site in SITES:
        print(f"  {site:<9} {'used' if site in used else 'not used':<9} {ACTORS[site]:<38} "
              f"{', '.join(used.get(site, [])) or 'no search lists it'}")
    print("The LLM and Tavily keys stay on your machine: the daily import doesn't use them.")
