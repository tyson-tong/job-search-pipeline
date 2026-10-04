# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and Seek (through Apify scrapers) into your own PostgreSQL database, merging the same job found on several sites. On your machine, you triage the new jobs, score the ones you're interested in against your CVs, draft a tailored CV (and, if you want, a cover letter) for each one, build interview briefings for the jobs you're interviewing for, and see which skills the jobs ask for that your CVs don't show.

It is a template: each person makes their own private copy with their own database, credentials, and CVs. **First time? Follow [docs/setup.md](docs/setup.md).**

```
Daily, in GitHub Actions                     On your machine, when you choose
─────────────────────────                    ────────────────────────────────
config/searches.toml ─► Apify ─► filter + dedupe ─► PostgreSQL ─► jobflow web                 triage, score, and track jobs
                                                           jobflow tailor-cv           CVs for Interested jobs
                                                           jobflow cover-letter        optional letters for Interested jobs
                                                           jobflow interview-prep      briefings for Interviewing jobs
                                                           jobflow skill-gaps          skills your CVs don't show
```

More: [your searches](docs/searches.md) · [rules](docs/rules.md) (duplicates, similar jobs, scores, CV and cover letter checks, which jobs each command does) · [structure](docs/structure.html) (open in a browser).

A job you're **pursuing** is one at Interested, Applied, Interviewing, or Offered: the jobs that are scored, tailored, and counted in skill gaps.

## Daily routine

Run commands from this folder with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the prefix).

1. **Open the tracker:** `uv run jobflow web`. It runs on your machine only (http://127.0.0.1:8765); last night's import has already brought in the new jobs.

2. **Triage the Inbox.** Mark each new job **Interested** or **Not interested**, one at a time or by ticking several rows (shift-click selects a range). Changes are drafts until you click **Save** (or press Ctrl+S). Unsure about some? Tick them and click **Score selected** to see how well they match your CVs first.

3. **Score your picks.** In **My jobs**, **Score all jobs** compares each job you're pursuing with each of your CVs: the 1 or 2 files named by `cvs` at the top of `config/profile.toml`, such as `cvs = ["CV_dev.yaml", "CV_strat.yaml"]`. Each is one LLM call per job per CV, made once. After you edit a CV, its earlier scores are marked `*`, and **Rescore edited CVs** redoes them. How scores work: [rules](docs/rules.md#match-scores).

4. **Track applications** in **My jobs**: status, follow-up date, and dated notes, all saved with **Save**. Rows flag overdue follow-ups and applications with no progress after three weeks.

5. **Draft a CV** for each **Interested** job, before you apply:

   ```
   uv run jobflow tailor-cv              # from each job's best-matching CV
   uv run jobflow tailor-cv --cv dev     # or from one of your cvs for all (CV_dev.yaml)
   uv run jobflow tailor-cv --max-pages 2   # allow 2 pages (default 1)
   uv run jobflow tailor-cv --format docx   # also write the CV as docx or latex
   uv run jobflow tailor-cv --job 42        # only job #42 (its number is in the job panel)
   ```

   It writes a reviewed, fact-checked `CV_<variant>_draft.yaml`, RenderCV's PDF of it, and `CV_REVIEW.md` to `output/<company>/<role>-<job-id>/`. Read the PDF line by line before you send it. If you edit the YAML, render it again from that folder with `uv run rendercv render CV_<variant>_draft.yaml --pdf-path CV_<variant>_draft.pdf`. A draft with fact-check errors gets no PDF until you fix it. How drafting, the fact check, and page fitting work: [rules](docs/rules.md#tailored-cvs).

   **Cover letters are optional:** `uv run jobflow cover-letter` (also with `--cv`) writes `cover-letter.docx` to the same folder, with its review in `COVER_LETTER_REVIEW.md`; a letter with fact-check errors is left in the review for you to fix. See [rules](docs/rules.md#cover-letters-and-the-review).

6. **Prepare for an interview**, once a job is **Interviewing**: `uv run jobflow interview-prep` writes `interview-prep.html` to the same folder: the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps, questions to ask, and a 30-minute checklist.

   Run these commands whenever jobs move along: each skips the jobs it has already done, tries again the ones that failed or whose files you deleted, and keeps a draft you fixed yourself. `--job ID --redo` does a job again anyway. The details: [rules](docs/rules.md#which-jobs-the-commands-do).

7. **See your skill gaps** now and then: `uv run jobflow skill-gaps` writes `output/skill-gaps-<date>.html`, ranking the skills the scored jobs you're pursuing ask for and your best-matching CVs don't show, with how to close each gap. "Not shown" means the CV lacks evidence: if you have the skill, add it to your CV, then click **Rescore edited CVs** before running `skill-gaps` again. See [rules](docs/rules.md#skill-gaps).

### In the tracker

- **Tabs:** **Inbox** (new jobs), **My jobs** (jobs you're pursuing and those you've finished with; the status menu picks which, **Default** restores them), **Filtered** (jobs your search filters kept out, with the reason), **Not interested**, and **Searches** (checks your searches file). From Filtered and Not interested you can mark a job Interested, or tick several to move them back to the Inbox. Old untriaged, Filtered, and Not interested jobs are [archived](docs/rules.md#archiving): kept and still recognised if posted again, but in no tab.
- **Filters** under the column headings: minimum match score, a title/company/location search, country and site, when the job was posted, and a minimum salary in US dollars (converted from each job's currency; jobs without a salary, in a country whose currency isn't known, or whose ad gives no pay period, are kept; such a guessed salary is shown with `~`). Click **Posted** or **Salary** to sort, again to reverse. **Export CSV (All)** exports every job in the tab, ignoring the other filters; **Export CSV (this page)** just the rows shown. Up to 2,000 matching jobs are loaded per query; narrow the filters to find jobs beyond that.
- **Job panel** (click a title): the ad, links to each site it's listed on, why it scored as it did, similar jobs, notes, status history, and the commands for this job (`tailor-cv --job 42` and so on) ready to copy. The number (`Job #42`) is at the top of the panel, and at the end of the job's output folder name.
- **Similar jobs** sit together with an orange bar, and a row warns if it may be a role you've already applied for; the same ad in another country sits alongside with a blue bar. See [rules](docs/rules.md#similar-jobs).
- **A late import is flagged:** if no import has succeeded for 36 hours, or a site's runs failed in that time, the header says so; `uv run jobflow status` shows why. So is a site whose runs returned all the results `max_results` allows, which probably left jobs behind: raise `max_results`, or narrow the search. The searches look back 24 hours, so catch up with `uv run jobflow ingest --posted-within-days 7` (or `14` or `30`), which changes the window for that import only.
- **Saving is safe:** the page refreshes itself (never over unsaved drafts), and if a job was changed elsewhere since you loaded it, Save writes nothing and shows what changed.

## Changing your searches

Edit `config/searches.toml` ([settings reference](docs/searches.md)), then run:

```
uv run jobflow update-searches
```

It checks the file (an invalid one stops everything, so a typo never reaches the daily import or your jobs), filters Inbox jobs your searches would now filter, brings back Filtered jobs they would no longer filter, and updates the copy the daily GitHub import reads (with your timezone, from `config/profile.toml`). Updating your jobs and updating GitHub are separate: if one fails (no GitHub CLI, say), the other still happens, and the command ends with an error saying what to fix before running it again. Add `--dry-run` to check and preview without changing anything. How jobs already saved are judged: [searches](docs/searches.md#which-site-applies-which-filter).

## Commands

| Command | What it does |
|---|---|
| `jobflow web [--port N]` | The job tracker in your browser |
| `jobflow tailor-cv [--job ID [--redo]] [--cv VARIANT] [--max-pages N] [--format pdf\|docx\|latex]` | A reviewed, fact-checked CV (YAML and PDF) for each Interested job without one |
| `jobflow cover-letter [--job ID [--redo]] [--cv VARIANT]` | Optional: a reviewed, fact-checked cover letter for each Interested job without one |
| `jobflow interview-prep [--job ID [--redo]]` | An interview briefing for each Interviewing job without one |
| `jobflow skill-gaps` | The skills the scored jobs you're pursuing ask for that your CVs don't show |
| `jobflow clean-up` | Delete the output folders of Not interested, Withdrawn, and Archived jobs you never applied for, after you confirm (the jobs stay in the database; a job you applied for, rejected or not, keeps the CV and letter you sent) |
| `jobflow update-searches [--dry-run]` | After editing your searches: check, filter the Inbox, bring back Filtered jobs your searches now keep, publish to GitHub (each of the last two goes ahead if the other fails) |
| `jobflow ingest [--search NAME] [--site SITE] [--posted-within-days DAYS]` | Run the searches now (the daily import does this; uses Apify credits). The posted-window override applies to this import only: `1`, `7`, `14`, or `30` days |
| `jobflow status` | The daily GitHub import, read from GitHub: whether it's on, its last run, its secrets, whether its searches and timezone match yours, and which sites it calls; and, from your database, why recent Apify runs failed |
| `jobflow init` | Create your settings files (first time only) |
| `jobflow help [COMMAND]` | Every command with its options, like a manual page (or one command's; `COMMAND -h` works too) |

Every command except `init`, `help`, `status`, and `update-searches --dry-run` brings the database tables up to date when it starts.

## Privacy

- `config/.env`, `config/profile.toml`, `config/searches.toml`, `output/`, and `cv/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results, which include short quotes from your CVs: keep it private.
- Scoring, tailoring, cover letters, and interview prep send CV and job text to your LLM provider, and skill gaps sends the job requirements your CVs didn't show; tailoring, cover letters, and interview prep send the company name and job title to Tavily. Choose providers whose data handling you accept.

## Development

```
uv sync --group dev
uv run pytest
uv run ruff check    # unused imports, likely bugs, import order, outdated syntax
uv run mypy          # type checks
```

The tests need no database or network. The ones in `tests/test_db.py` (merging, saving, migrations) run only when `TEST_DATABASE_URL` names a disposable PostgreSQL database; each run works in a schema of its own and drops it afterwards. One test of the tracker's JavaScript runs only where `node` is installed. For example, with Docker:

```
docker run -d --rm --name jobflow-test-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres uv run pytest
```

GitHub runs ruff, mypy, and all the tests, the database ones included, on every push (`.github/workflows/tests.yml`). Dependabot (`.github/dependabot.yml`) opens a pull request each month to update the pinned GitHub Actions and the locked dependencies; the tests run on each.
