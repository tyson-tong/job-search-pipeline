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

2. **Triage the Inbox.** Mark each new job **Interested** or **Not interested**, one at a time or by ticking several rows (shift-click selects a range). Changes are drafts until you click **Save** (or press Ctrl+S). Interested jobs move to **My jobs**; Not interested jobs leave the tracker (they stay in the database), so undo a misclick before saving.

3. **Score your picks.** In **My jobs**, click **Score all jobs**: each Interested job not yet scored is compared with each of your CVs (every file in `cv` whose name contains "cv": .yaml, .tex, .docx, or .pdf) by the LLM. A job is scored once and keeps its scores even if a CV changes. Only Interested jobs are ever scored.

4. **Track applications** in **My jobs**: status (applied, interviewing, offered, accepted, declined, rejected, withdrawn), follow-up date, and dated notes, all saved with **Save**. Rows flag overdue follow-ups and applications with no progress after three weeks.

5. **Draft a CV** for each **Interested** job, before you apply:

   ```
   uv run jobflow tailor-cv              # from each job's best-matching CV
   uv run jobflow tailor-cv --cv dev     # or use one CV for all: cv/CV_dev.yaml is "dev"
   uv run jobflow tailor-cv --max-pages 2   # allow 2 pages (default 1)
   uv run jobflow tailor-cv --format pdf    # also write the CV: pdf, docx, or latex
   ```

   It works from your best-matching RenderCV YAML CV (CVs in other formats are scored, not tailored). For each job it drafts the CV; a reviewer, playing the hiring manager (with company research from Tavily, if set up), then critiques and revises it. The revision is kept only if it passes the fact check at least as well as the first draft. It writes `CV_<variant>_draft.yaml` and `CV_REVIEW.md` (the fact check and the reviewer's critique, with a `CV_REVIEW.docx` copy) to `output/<company>/<role>-<job-id>/`. If the draft runs past 1 page (or `--max-pages N`) as RenderCV lays it out, it cuts the bullets least relevant to the job until it fits (listed in `CV_REVIEW.md`, so you can restore any), and it warns about a page left mostly empty because a long entry moved to the next one. By default that's all: read the draft line by line, fix the YAML, and render it from that folder with `uv run rendercv render CV_<variant>_draft.yaml`. `--format` also writes the CV itself if the check found no errors: `pdf` (RenderCV's), `docx`, or `latex` (a `.tex` to compile with lualatex). The `.docx` and `.tex` are laid out in code from the checked YAML, not by the LLM, so they say exactly what passed the check; they're fitted to RenderCV's page count, so check theirs in the program you open them with. An unscored job is scored first.

   **Cover letters are optional:** `uv run jobflow cover-letter` (also with `--cv`) writes `COVER_LETTER_REVIEW.md` (with a `.docx` copy) and, if the check found no errors, `cover-letter.docx` (a standard business letter: your details, the date, the employer, a subject line, then the letter) to the same folder for each Interested job, drafted from your best-matching CV in any format, the job ad, and the company research, then reviewed and checked the same way. The letter's header takes your name and contact details from the CV: from a RenderCV YAML CV, its name, location, email, and phone; from another format, the name the letter signs off with and the first email address and phone number in the CV. A letter with errors is left in the review for you to fix.

6. **Prepare for an interview**, once a job is **Interviewing**: `uv run jobflow interview-prep` writes `interview-prep.html` to the same folder for each: the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps, questions to ask, and a 30-minute checklist.

   These commands work through every job at that status and skip jobs they've already done, so run them whenever jobs move along. A job that fails (a network error, say) is retried the next time you run the command.

7. **See your skill gaps** now and then: `uv run jobflow skill-gaps` writes `output/skill-gaps-<date>.html`, ranking the skills your scored jobs ask for that your best-matching CVs don't show, with how to close each gap. It uses your stored scores and one LLM call. "Not shown" means the CV lacks evidence, so if you have the skill, the fix may be adding it to your CV.

### In the tracker

- **Tabs:** **Inbox** (new jobs), **My jobs** (Interested onwards; the status menu picks which statuses, **Default** restores them), **Filtered** (jobs your search filters kept out, with the reason; **Move to Inbox** brings one back), and **Searches** (checks your searches file).
- **Filters** under the column headings: minimum match score, a title/company/location search, country and site (tick several, or **All** or **None**), when the job was posted (past day, week, 2 weeks, or month, or over a month ago), and a minimum salary in US dollars (converted from each job's currency; jobs without a salary are kept). Click **Posted** or **Salary** to sort, again to reverse. **Export CSV (All)** exports every job in the tab (in My jobs, those with the chosen statuses), ignoring the other filters; **Export CSV (Filtered)** exports just the rows on the page you're viewing. Filters and sorting cover every job; the results are then shown 5 to 100 per page (the menu beside the job count), with similar jobs kept together on one page.
- **Job panel** (click a title): the ad, links to each site it's listed on, why it scored as it did, similar jobs, notes, status history, and the tailor-cv and interview-prep commands, ready to copy.
- **Similar jobs** sit together with an orange bar ("highly similar" or "similar"; a similar job not in the list shown is counted with where it is, such as "1 highly similar (Filtered)"), and a row warns if it may be a role you've already applied for; the same ad in another country sits alongside with a blue bar. See [rules](docs/rules.md#similar-jobs).
- **Saving is safe:** the page refreshes itself (never over unsaved drafts), and if a job was changed elsewhere since you loaded it, Save writes nothing and shows what changed.

## Changing your searches

Edit `config/searches.toml` ([settings reference](docs/searches.md)), then run:

```
uv run jobflow update-searches
```

It checks the file (an invalid one stops everything, so a typo never reaches the daily import), updates the copy the daily GitHub import reads, and filters jobs already in your Inbox from your `exclude_companies` or whose titles match your `exclude_title_terms` (unless they match `include_title_terms`). Add `--dry-run` to check and preview without changing anything. Filtered jobs go to the Filtered tab.

## Commands

| Command | What it does |
|---|---|
| `jobflow web [--port N]` | The job tracker in your browser |
| `jobflow tailor-cv [--cv VARIANT] [--max-pages N] [--format yaml\|latex\|docx\|pdf]` | A reviewed, fact-checked CV for each Interested job without one |
| `jobflow cover-letter [--cv VARIANT]` | Optional: a reviewed, fact-checked cover letter for each Interested job without one |
| `jobflow interview-prep` | An interview briefing for each Interviewing job without one |
| `jobflow skill-gaps` | The skills your scored jobs ask for that your CVs don't show |
| `jobflow update-searches [--dry-run]` | After editing your searches: check, publish to GitHub, filter the Inbox |
| `jobflow ingest [--search NAME] [--site SITE]` | Run the searches now (the daily import does this; uses Apify credits) |
| `jobflow init` | Create your settings files (first time only) |

Every command except `init` brings the database tables up to date when it starts.

## Privacy

- `config/.env`, `config/profile.toml`, `config/searches.toml`, `output/`, and `cv/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results, which include short quotes from your CVs: keep it private.
- Scoring, tailoring, cover letters, skill gaps, and interview prep send CV and job text to your LLM provider; tailoring, cover letters, and interview prep send the company name and job title to Tavily. Choose providers whose data handling you accept.

## Development

```
uv sync --extra test
uv run pytest
```

The tests need no database or network.
