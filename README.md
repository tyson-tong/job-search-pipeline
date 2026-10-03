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

More: [your searches](docs/searches.md) · [rules](docs/rules.md) (duplicates, similar jobs, scores, CV and cover letter checks) · [structure](docs/structure.html) (open in a browser).

## Daily routine

Run commands from this folder with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the prefix).

1. **Open the tracker:** `uv run jobflow web`. It runs on your machine only (http://127.0.0.1:8765); last night's import has already brought in the new jobs.

2. **Triage the Inbox.** Mark each new job **Interested** or **Not interested**, one at a time or by ticking several rows (shift-click selects a range). Changes are drafts until you click **Save** (or press Ctrl+S). Interested jobs move to **My jobs**; Not interested jobs move to the **Not interested** tab, where **Interested** undoes a misclick for 2 weeks. Unsure about some? Tick them and click **Score selected** to see how well they match your CVs before you decide (one LLM call per job per CV); a job keeps its score when you mark it Interested.

3. **Score your picks.** In **My jobs**, click **Score all jobs**: each Interested job is compared by the LLM with each CV named by `cvs` at the top of `config/profile.toml` (1 or 2 exact file names in `cv/`, such as `cvs = ["CV_dev.yaml", "CV_strat.yaml"]`) that it has no score against yet. That's one LLM call per job per CV, hence the limit of 2, with two jobs at a time. `tailor-cv`, `cover-letter`, `skill-gaps`, and `interview-prep` use only those CVs too; until `cvs` is set right, the tracker says what's wrong and they stop. A job is scored once against each CV. After you edit a CV, its earlier scores are marked `*`, and **Rescore edited CVs** scores the jobs you're pursuing (interested, applied, interviewing, or offered) again against it (the job panel's **Rescore** does one job). What counts as an edit, the CV size limit, and what happens when you add or rename a CV: see [rules](docs/rules.md#match-scores).

4. **Track applications** in **My jobs**: status (applied, interviewing, offered, accepted, declined, rejected, withdrawn), follow-up date, and dated notes, all saved with **Save**. Rows flag overdue follow-ups and applications with no progress after three weeks.

5. **Draft a CV** for each **Interested** job, before you apply:

   ```
   uv run jobflow tailor-cv              # from each job's best-matching CV
   uv run jobflow tailor-cv --cv CV_dev.yaml   # or use one of your cvs for all (or --cv dev)
   uv run jobflow tailor-cv --max-pages 2   # allow 2 pages (default 1)
   uv run jobflow tailor-cv --format docx   # also write the CV as docx or latex
   uv run jobflow tailor-cv --job 42        # only job #42 (its number is in the job panel)
   ```

   It drafts from your best-matching RenderCV YAML CV (CVs in other formats are scored, not tailored; an unscored job is scored first). A reviewer playing the hiring manager (with company research from Tavily, if set up) critiques and revises the draft, and the revision is kept only if it passes the fact check at least as well. It writes `CV_<variant>_draft.yaml`, RenderCV's PDF of it, and `CV_REVIEW.md` (the fact check, the critique, and any bullets cut to fit the page limit, with a `CV_REVIEW.docx` copy) to `output/<company>/<role>-<job-id>/`. Read the PDF line by line before you send it; if you edit the YAML, render it again from that folder with `uv run rendercv render CV_<variant>_draft.yaml`. A draft with fact-check errors gets no PDF (or `--format` file) until you fix it. `--format docx` or `latex` is laid out in code from the checked YAML, and for a job tailored before it just adds that file, with no LLM call. How the fact check, page fitting, and `--format` work: see [rules](docs/rules.md#tailored-cvs).

   **Cover letters are optional:** `uv run jobflow cover-letter` (also with `--cv`) writes `COVER_LETTER_REVIEW.md` (with a `.docx` copy) and, if the check found no errors, `cover-letter.docx` (a standard business letter: your details, the date, the employer, a subject line, then the letter) to the same folder for each Interested job, drafted from your best-matching CV in any format, the job ad, and the company research, then reviewed and checked the same way. The letter's header takes your name and contact details from the CV: from a RenderCV YAML CV, its name, location, email, and phone; from another format, the name the letter signs off with and the first email address and phone number in the CV. A letter with errors is left in the review for you to fix.

6. **Prepare for an interview**, once a job is **Interviewing**: `uv run jobflow interview-prep` writes `interview-prep.html` to the same folder for each: the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps, questions to ask, and a 30-minute checklist. Unlike scoring and drafting, it doesn't refuse a CV over 28,000 characters: it uses the start of it, and warns.

   These commands work through every job at that status and skip jobs they've already done, so run them whenever jobs move along. A job that fails (a network error, say) is retried the next time you run the command, and so is one whose output you deleted (with `clean-up`, say): its `CV_<variant>_draft.yaml` or `CV_REVIEW.md`, `cover-letter.docx`, or `interview-prep.html`. Deleting only a CV's PDF doesn't count; render it again from the YAML. So is a CV or letter that failed the fact check, for a fresh draft, unless you've fixed it yourself: edited the `CV_<variant>_draft.yaml`, or saved the letter as `cover-letter.docx` in its folder. Then your version is kept. To do a job again anyway, name it with `--job ID --redo`; that replaces its files, a fix of yours included. `--job` also takes a job you've moved on from Interested (applied, interviewing, or offered), say for a letter for a second round; `interview-prep` takes only Interviewing jobs. A draft that fails the check never leaves an older PDF, `.docx`, or `cover-letter.docx` beside it.

7. **See your skill gaps** now and then: `uv run jobflow skill-gaps` writes `output/skill-gaps-<date>.html`, ranking the skills that the scored jobs you're pursuing (interested, applied, interviewing, or offered) ask for and your best-matching CVs don't show, with how to close each gap. It uses your stored scores and one LLM call. "Not shown" means the CV lacks evidence, so if you have the skill, the fix may be adding it to your CV; then click **Rescore edited CVs** in the tracker before running `skill-gaps` again, since scores aren't redone by themselves (the report and the command say how many of its jobs have scores from an earlier version of a CV).

### In the tracker

- **Tabs:** **Inbox** (new jobs, with a Match column for any you've scored), **My jobs** (Interested onwards; the status menu picks which statuses, **Default** restores them), **Filtered** (jobs your search filters kept out, with the reason; mark one **Interested** or **Not interested**, or tick several to move them to the Inbox), **Not interested** (jobs you marked so; **Interested** moves one to My jobs, or tick several to move them to the Inbox), and **Searches** (checks your searches file). Untriaged Inbox jobs, Filtered jobs, and Not interested jobs are archived once they're old enough: kept in the database and still recognised if posted again, but in no tab ([rules](docs/rules.md#archiving)).
- **Filters** under the column headings: minimum match score (My jobs and the Inbox), a title/company/location search, country and site (tick several, or **All** or **None**), when the job was posted (past day, week, 2 weeks, or month, or over a month ago), and a minimum salary in US dollars (converted from each job's currency; jobs without a salary are kept). Click **Posted** or **Salary** to sort, again to reverse. **Export CSV (All)** exports every job in the tab (in My jobs, those with the chosen statuses), ignoring the other filters; **Export CSV (this page)** exports just the rows on the page you're viewing. Filters and sorting apply before paging; up to 2,000 matching jobs are loaded per query. If the list reaches that cap, narrow the filters to find jobs beyond it. The results are shown 5 to 100 per page (the menu beside the job count), with similar jobs kept together on one page.
- **Job panel** (click a title): the ad, links to each site it's listed on, why it scored as it did, similar jobs, notes, status history, and the tailor-cv and cover-letter commands (for a job you're pursuing: Interested onwards) and interview-prep (for an Interviewing one) with `--job` and the job's number, ready to copy. The number (`Job #42`) is at the top of the panel, and at the end of the job's output folder name.
- **Similar jobs** sit together with an orange bar ("highly similar" or "similar"; a similar job not in the list shown is counted with where it is, such as "1 highly similar (Filtered)"), and a row warns if it may be a role you've already applied for; the same ad in another country sits alongside with a blue bar. See [rules](docs/rules.md#similar-jobs).
- **A late import is flagged:** the searches look back 24 hours, so a day the daily import missed is jobs never seen. If no import has succeeded for 36 hours, the header says so, and it names each site with runs that failed in that time (hover for the error), even when other sites' runs succeeded; `uv run jobflow status` shows why. Catch up with `uv run jobflow ingest --posted-within-days 7` (or `14` or `30` for a longer gap). This overrides every search's window for that import only and leaves your saved searches unchanged.
- **Saving is safe:** the page refreshes itself (never over unsaved drafts), and if a job was changed elsewhere since you loaded it, Save writes nothing and shows what changed.

## Changing your searches

Edit `config/searches.toml` ([settings reference](docs/searches.md)), then run:

```
uv run jobflow update-searches
```

It checks the file (an invalid one stops everything, so a typo never reaches the daily import), updates the copy the daily GitHub import reads (with your timezone, from `config/profile.toml`), and filters jobs already in your Inbox that your searches now would, say after you add an excluded title term or employer or raise a salary minimum (a job you moved out of Filtered is left alone). It also brings Filtered jobs not yet archived back to the Inbox when your searches would no longer filter them, say after you remove an excluded term or lower a salary minimum. Each job is judged by every filter of the search that found it, as on the day it was imported, so a job isn't filtered just for having aged past the search's posted window. Add `--dry-run` to check and preview without publishing, filtering, or changing anything; if the database tables are out of date (after you update jobflow), it says so and stops, as only a real run brings them up to date. Filtered jobs go to the Filtered tab.

## Commands

| Command | What it does |
|---|---|
| `jobflow web [--port N]` | The job tracker in your browser |
| `jobflow tailor-cv [--job ID [--redo]] [--cv VARIANT] [--max-pages N] [--format pdf\|docx\|latex]` | A reviewed, fact-checked CV (YAML and PDF) for each Interested job without one |
| `jobflow cover-letter [--job ID [--redo]] [--cv VARIANT]` | Optional: a reviewed, fact-checked cover letter for each Interested job without one |
| `jobflow interview-prep [--job ID [--redo]]` | An interview briefing for each Interviewing job without one |
| `jobflow skill-gaps` | The skills the scored jobs you're pursuing ask for that your CVs don't show |
| `jobflow clean-up` | Delete the output folders of Not interested, Withdrawn, Declined, Rejected, and Archived jobs, after you confirm (the jobs stay in the tracker) |
| `jobflow update-searches [--dry-run]` | After editing your searches: check, publish to GitHub, filter the Inbox, bring back Filtered jobs your searches now keep |
| `jobflow ingest [--search NAME] [--site SITE] [--posted-within-days DAYS]` | Run the searches now (the daily import does this; uses Apify credits). The posted-window override applies to this import only: `1`, `7`, `14`, or `30` days |
| `jobflow status` | The daily GitHub import, read from GitHub: whether it's on, its last run, its secrets, whether its searches and timezone match yours, and which sites it calls; and, from your database, why recent Apify runs failed |
| `jobflow init` | Create your settings files (first time only) |
| `jobflow help [COMMAND]` | Every command with its options, like a manual page (or one command's; `COMMAND -h` works too) |

`tailor-cv`, `cover-letter`, and `interview-prep` work through every job at their status (Interested, or for `interview-prep` Interviewing), or only the ones `--job` names (repeat it for several), two jobs at a time, printing each job's output together once it's done. Only one run of each can go at a time: start a second (in another terminal, say) and it stops, saying the first is still running.

Every command except `init`, `help`, `status`, and `update-searches --dry-run` brings the database tables up to date when it starts. `tailor-cv`, `cover-letter`, `interview-prep`, and `skill-gaps` check your LLM (and, for `interview-prep`, Tavily) settings before starting any job.

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

The tests need no database or network. The ones in `tests/test_db.py` (merging, saving, migrations) run only when `TEST_DATABASE_URL` names a disposable PostgreSQL database; each run works in a schema of its own and drops it afterwards. For example, with Docker:

```
docker run -d --rm --name jobflow-test-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
TEST_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres uv run pytest
```

GitHub runs ruff, mypy, and all the tests, the database ones included, on every push (`.github/workflows/tests.yml`). Dependabot (`.github/dependabot.yml`) opens a pull request each month to update the pinned GitHub Actions and the locked dependencies; the tests run on each.
