"""The daily GitHub import, seen from your machine: publishing your searches and timezone
to it (update-searches), and reading back what it uses (status), with the GitHub CLI."""
from __future__ import annotations

import shutil
import subprocess
import sys

# The daily import's workflow file, the secrets it needs, and what GitHub's workflow states
# mean for it.
WORKFLOW = "daily-ingest.yml"
ACTIONS_SECRETS = ("DATABASE_URL", "APIFY_TOKEN")
WORKFLOW_STATES = {"active": "on", "disabled_manually": "off",
                   "disabled_inactivity": "off (GitHub turned it off after 60 days without "
                                          "activity in the repository)"}


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
    """Check config/searches.toml, publish it as the JOBFLOW_SEARCHES GitHub variable that
    the daily import reads, then filter Inbox jobs from its excluded employers or matching
    its excluded title terms (imports only apply them to the jobs they bring in), and bring
    back Filtered jobs its filters would now keep. An invalid file stops before anything
    is published, so a typo never reaches the daily import."""
    from .db import migrate
    from .ingest.searches import defaults_search, parse_config, search_warnings
    from .ingest.store import exclude_inbox_jobs, restore_filtered_jobs
    from .settings import searches_file, timezone

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
    # The daily import has no config/profile.toml, so it gets your timezone this way too.
    zone = timezone().key
    if dry_run:
        print(f"Would update the JOBFLOW_SEARCHES and JOBFLOW_TIMEZONE ({zone}) GitHub variables "
              "(dry run: not updated)")
    else:
        _gh(["variable", "set", "JOBFLOW_SEARCHES"], "Updating the JOBFLOW_SEARCHES GitHub "
            "variable (nothing else was changed)", input=text,
            hint=", or set the variable by hand: gh variable set JOBFLOW_SEARCHES < "
            "config/searches.toml")
        _gh(["variable", "set", "JOBFLOW_TIMEZONE", "--body", zone], "Updating the "
            "JOBFLOW_TIMEZONE GitHub variable (JOBFLOW_SEARCHES was updated)",
            hint=f", or set the variable by hand: gh variable set JOBFLOW_TIMEZONE --body {zone}")
        print(f"Updated the JOBFLOW_SEARCHES and JOBFLOW_TIMEZONE ({zone}) GitHub variables: the "
              "daily import uses them now")
    migrate()
    # A job found by a search since renamed or removed is judged by [defaults].
    fallback = defaults_search(text)
    matched, checked = exclude_inbox_jobs(searches, dry_run=dry_run, fallback=fallback)
    for job in matched:
        print(f"  #{job['id']} {job['title']} ({job['company'] or 'unknown employer'}): "
              f"{job['reason']}")
    print(f"{'Would filter' if dry_run else 'Filtered'} {len(matched)} of {checked} Inbox jobs"
          f"{' (dry run: none filtered)' if dry_run else ''}")
    restored, checked = restore_filtered_jobs(searches, dry_run=dry_run, fallback=fallback)
    for job in restored:
        print(f"  #{job['id']} {job['title']} ({job['company'] or 'unknown employer'}): "
              f"was filtered: {job['reason']}")
    print(f"{'Would bring' if dry_run else 'Brought'} {len(restored)} of {checked} Filtered jobs "
          f"back to the Inbox{' (dry run: none moved)' if dry_run else ''}")


def status() -> None:
    """Show what the daily GitHub import uses, reading it from GitHub: whether it's on, its
    last run, its secrets, its searches (and whether they match config/searches.toml), and
    which job sites (Apify scrapers) it calls. Changes nothing."""
    import json
    from datetime import datetime

    from .ingest.options import ACTORS, SITES
    from .ingest.searches import SearchConfigError, parse_config
    from .settings import searches_file, timezone

    state = _gh(["api", f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}", "--jq", ".state"],
                "Reading the daily import's workflow").strip()
    runs = json.loads(_gh(["run", "list", "--workflow", WORKFLOW, "-L", "1", "--json",
                           "status,conclusion,createdAt,event"], "Reading the daily import's runs"))
    secrets = {s["name"]: s["updatedAt"][:10] for s in json.loads(_gh(
        ["secret", "list", "--json", "name,updatedAt"], "Reading the repository's secret names"))}
    variables = {v["name"]: v["value"] for v in json.loads(_gh(
        ["variable", "list", "--json", "name,value"], "Reading the repository's Actions variables"))}
    print(f"Daily import (.github/workflows/{WORKFLOW}): {WORKFLOW_STATES.get(state, state)}")
    if runs:
        run = runs[0]
        started = datetime.fromisoformat(run["createdAt"]).astimezone(timezone())
        print(f"  Last run: {started:%Y-%m-%d %H:%M %Z} ({run['event']}), "
              f"{run['conclusion'] or run['status']}")
    else:
        print("  Last run: none yet")
    print("Secrets:  " + ", ".join(f"{name} set {secrets[name]}" if name in secrets
                                   else f"{name} MISSING (gh secret set {name})"
                                   for name in ACTIONS_SECRETS))
    text = variables.get("JOBFLOW_SEARCHES")
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
            print(f"Searches: JOBFLOW_SEARCHES, {len(searches)} search"
                  f"{'es' if len(searches) != 1 else ''}"
                  + ("" if same is None else " (same as config/searches.toml)" if same
                     else " (differs from config/searches.toml: run `uv run jobflow update-searches`)"))
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
