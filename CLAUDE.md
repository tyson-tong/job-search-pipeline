# jobflow: working on this repository

A personal job-search pipeline, shipped as a template that each person copies privately.
A daily GitHub Action scrapes LinkedIn, Indeed, and Seek through Apify into the user's own
PostgreSQL; on their machine they triage and track jobs in a local web tracker, score jobs
against their CVs with an LLM, and draft tailored CVs, cover letters, interview briefings,
and a skill-gap report. Read README.md for the user's view, docs/rules.md for every rule
the code follows, and docs/structure.html for the parts and tables.

## Commands

```
uv sync --group dev              # dev tools; the documents group (RenderCV, docx, pdf) is a default
uv run pytest                    # the database tests skip without TEST_DATABASE_URL
uv run ruff check                # lint only; the code isn't auto-formatted
uv run mypy                      # src/jobflow
```

To run the database tests, start a throwaway PostgreSQL (each run uses its own schema and
drops it):

```
docker run -d --rm --name jobflow-test-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres uv run pytest
```

CI (`.github/workflows/tests.yml`) runs ruff, mypy, and every test, database ones included,
on each push. Run all three before calling a change done.

Never run `jobflow ingest`, `update-searches` (without `--dry-run`), `tailor-cv`,
`cover-letter`, `interview-prep`, or `skill-gaps` against the user's real setup to test a
change: they spend Apify or LLM credits, publish GitHub variables, or change their jobs.
Ask first.

## Layout

`src/jobflow/`, by area (structure.html has a line per module):

- `cli.py`: argparse commands, each a thin `run_*` that imports what it needs lazily.
  `main` turns `RuntimeError` (expected problems), `psycopg.OperationalError`, and
  `requests.RequestException` into a one-line `error: ...` and exit status 1.
- `settings.py` (config/.env, config/profile.toml, timezone), `db.py` (connections,
  migrations, status changes), `llm.py` (OpenAI-compatible chat completions), `batch.py`
  (per-job commands, two jobs at a time), `github.py` (`gh` for update-searches and
  status), `cleanup.py`, `text.py`.
- `ingest/`: searches config to Apify runs (`searches`, `options`, `runs`, `apify`),
  normalising scraped records (`sources`), filtering, merging, and storing (`filters`,
  `dedupe`, `store`), and later passes (`refilter`, `upkeep`, `similar`, `backfill`,
  `report`).
- `candidate/`: CV files (`cvs`), scoring (`matching`), tailoring (`tailoring`,
  `render`, `documents`, `factcheck`, `review`, `research`), `cover_letter`, `interview`,
  `skill_gaps`, and what several share: `paths` (a job's output folder) and `pages` (the
  HTML page skeleton of the briefing and the skill-gaps report).
- `web/`: the tracker. `server.py` (stdlib HTTP server on 127.0.0.1), `queries.py`,
  `saving.py`, `scoring.py`, `grouping.py`, `export.py`, and `static/` (index.html and ES
  modules; `app.js` is the entry point).

## Rules for changes

### Keep the docs in step, each fact in one place

- **docs/rules.md** holds every rule: thresholds, windows, statuses, which jobs each
  command does, what the tracker flags. README.md, structure.html, and the CLI help
  summarise and link to it; never restate a rule there. A behaviour change means updating
  rules.md (and the README only if the user's routine changes).
- **docs/searches.md** is the reference for `config/searches.toml` settings;
  **docs/setup.md** is first-time setup. `config/*.example.*` are the templates
  `jobflow init` copies.
- CLI help strings stay one short sentence; `jobflow help` builds its manual from them.
- When a constant changes (a threshold, a window, a limit), search the docs for its value.

### Errors

- Expected problems (bad settings, a missing key, a job with the wrong status, a batch with
  failures) raise `RuntimeError` with a message saying what to do. Library code never
  raises `SystemExit`; `cli.main` decides how to exit.
- `llm.LLMError(fatal=True)` means every later request would fail (bad key, no credit,
  unknown model): batches stop instead of failing, and paying for, each job in turn.

### Database and migrations

- The schema is in `src/jobflow/migrations/NNN_table.sql`, each file holding blocks headed
  `-- Migration ID: 01`, `02`, ... Every command but init, help, status, and
  `update-searches --dry-run` applies new blocks once, under an advisory lock.
- **Never edit or reorder a block that has shipped**: real databases have applied it by
  name. Add the change as the next ID in the file of the table it changes, with a comment
  saying why.
- A block that needs existing rows filled in names a function with `-- Then: NAME`; it lives
  in `ingest/backfill.py` (`FOLLOW_UPS`) and runs once, after all new blocks.
- A new job status needs `db.STATUSES`, a migration replacing `jobs_status_check`, and the
  labels in `web/static/format.js`.
- `jobs.status` is the single source of truth. Status changes you make go through
  `db.set_job_state`; changes jobflow makes (update-searches) through `db.move_job`; both
  record `status_history`, which update-searches reads (via `'web'` vs `'update-searches'`).
- Dates are compared in the user's timezone: `settings.today()`/`now()` in Python, and
  every connection sets the session TimeZone (`db.connect`), so `::date` agrees.
- Data loss is the worst failure here. Merging (`ingest/store.py`, `dedupe.py`) is strict
  on purpose: a missed merge leaves a flagged similar job, a wrong merge loses a job.

### The daily import

- It runs `uv sync --no-group documents`: nothing on ingest's import path may import
  `yaml`, `docx`, `pypdf`, or `rendercv` (a test enforces this).
- It has no config/profile.toml: it reads `JOBFLOW_SEARCHES` and `JOBFLOW_TIMEZONE`, which
  `update-searches` publishes.
- GitHub stops it at 180 minutes; `apify.INGEST_MINUTES` (150) keeps runs inside that.
- Actions are pinned to commit SHAs, with the version in a comment.

### LLM work

- The LLM only drafts, groups, or ranks; code checks and computes. Scores are computed
  from verified quotes (`matching.py`); tailored CVs and letters are fact-checked in code
  (`factcheck.py`); a reviewer's revision is kept only if it checks at least as well
  (`review.py`); bullets are cut in code (`render.py`).
- Every prompt treats job ads, CVs, and web results as data, not instructions.
- Temperature is `llm.TEMPERATURE` (0.1) everywhere except cover-letter writing
  (`cover_letter.LETTER_TEMPERATURE`).

### The tracker (web/)

- Local only: 127.0.0.1, a per-start token sent in the `X-Jobflow-Token` header (never a
  URL), a Host check, and a CSP allowing only the page's own script files. Never add inline
  script or event-handler attributes; build DOM with `el()`/`fill()` in `common.js`, which
  never parse text as HTML.
- `format.js` touches no page, so tests run it under node.
- The job list refreshes every 3 minutes: anything it does per refresh must stay cheap.
  `ingest/similar.py` keeps titles (`_TITLES`) and ad words (`_AD_WORDS`) between
  refreshes; a change to when titles or ads change must keep those caches right.

## Tests

- Write a test only for behaviour whose silent breakage would cost something: lost or
  merged jobs, wrong scores or salaries, money (Apify runs, LLM calls), security, or a bug
  that actually happened. No tests of constants, one-line helpers, or mocks.
- Each test opens with a comment on what broke, or would break, without it.
- `tests/conftest.py` clears real credentials from the environment, blocks off-machine
  network connections and the real `gh`, and points every test at a temporary profile, cv/,
  and output/. Keep it that way: a test must never reach the user's database, LLM, Tavily,
  Apify, or GitHub. Fake them (see the `provider`, `scoring`, `letter_llm`, and
  `tailor_llm` fixtures).
- Database tests (`tests/test_db.py`) use the `database` fixture; reset any module-level
  cache there, since each test's schema reuses job ids.

## Style

- Python 3.12, `from __future__ import annotations`, type hints, lines up to 100.
- Plain-English comments and docstrings that explain why, in the user's terms ("a job you're
  pursuing", "the Inbox"); messages say what to do next.
- Imports inside functions are deliberate where they keep a command's start fast or keep
  documents libraries off the daily import's path.
- User-facing text uses Australian/British spelling (normalise, recognised).

## Git

- Commit only when asked. Stage files by name, never `git add -A`: the user edits
  alongside. No `Co-Authored-By` lines.
- Never commit config/.env, config/profile.toml, config/searches.toml, cv/, or output/
  (all Git-ignored).
