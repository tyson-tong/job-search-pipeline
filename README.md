# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and Seek (through Apify scrapers) into your own PostgreSQL database, merging the same job found on several sites. On your machine, you triage the new jobs, score the ones you're interested in against your CVs, draft tailored CVs, and build interview briefings.

It is a template: each person makes their own private copy with their own database, credentials, and CVs. **First time? Follow [docs/setup.md](docs/setup.md).**

```
Daily, in GitHub Actions                     On your machine, when you choose
─────────────────────────                    ────────────────────────────────
config/searches.toml ─► Apify ─► filter + dedupe ─► PostgreSQL ─► jobflow web                 triage, score, and track jobs
                                                           jobflow tailor-cv ID        draft a CV for one job
                                                           jobflow interview-prep ID   briefing before an interview
```

More: [your searches](docs/searches.md) · [rules](docs/rules.md) (duplicates, similar jobs, scores, CV checks) · [structure](docs/structure.html) (open in a browser).

## Daily routine

Run commands from this folder with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the prefix).

1. **Open the tracker:** `uv run jobflow web`. It runs on your machine only (http://127.0.0.1:8765); this morning's import has already brought in the new jobs.

2. **Triage the Inbox.** Mark each new job **Interested** or **Not interested**, one at a time or by ticking several rows (shift-click selects a range). Changes are drafts until you click **Save** (or press Ctrl+S). Interested jobs move to **My jobs**; Not interested jobs leave the tracker (they stay in the database), so undo a misclick before saving.

3. **Score your picks.** In **My jobs**, click **Score all jobs**: each Interested job not yet scored is compared with each of your CVs by the LLM. A job is scored once and keeps its scores even if a CV changes. Only Interested jobs are ever scored.

4. **Track applications** in **My jobs**: status (applied, interviewing, offered, accepted, declined, rejected, withdrawn), follow-up date, and dated notes, all saved with **Save**. Rows flag overdue follow-ups and applications with no progress after three weeks.

5. **Draft a CV** for a job you're applying to:

   ```
   uv run jobflow tailor-cv 123              # uses the best-matching CV
   uv run jobflow tailor-cv 123 --cv dev     # or choose one: CV_dev.yaml is "dev"
   ```

   Without `--cv`, it scores the job first if it's an unscored Interested job; any other unscored job needs `--cv`. It warns if a similar job is already applied for, then writes `CV_<variant>_draft.yaml` and a fact check, `REVIEW_REQUIRED.md`, to `output/<company>/<role>-<job-id>/`, and renders a PDF if RenderCV is installed and the check found no errors. Read the draft line by line; after fixing it, render it from that folder: `rendercv render CV_<variant>_draft.yaml`.

6. **Prepare for an interview:**

   ```
   uv run jobflow interview-prep 123 --email-file ./invite.txt   # with the invitation email
   uv run jobflow interview-prep 123                             # without one
   pbpaste | uv run jobflow interview-prep 123 --email-file -    # email from the clipboard (macOS)
   ```

   It writes `interview-prep.html` to the same folder: the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps, questions to ask, and a 30-minute checklist. The email is used only for that run and never saved.

### In the tracker

- **Tabs:** **Inbox** (new jobs), **My jobs** (Interested onwards; the status menu picks which statuses, **Default** restores them), **Filtered** (jobs your search filters kept out, with the reason; **Move to Inbox** brings one back), and **Searches** (checks your searches file).
- **Filters** under the column headings: minimum match score, a title/company/location search, country (tick several), site, when the job was posted (past day, week, 2 weeks, or month, or over a month ago), and a minimum salary in US dollars (converted from each job's currency; jobs without a salary are kept). Click **Posted** or **Salary** to sort, again to reverse. **Export CSV** exports the filtered view.
- **Job panel** (click a title): the ad, links to each site it's listed on, why it scored as it did, similar jobs, notes, status history, and the tailor-cv and interview-prep commands, ready to copy.
- **Similar jobs** sit together with an orange bar ("highly similar" or "similar"), and a row warns if it may be a role you've already applied for; the same ad in another country sits alongside with a blue bar. See [rules](docs/rules.md#similar-jobs).
- **Saving is safe:** the page refreshes itself (never over unsaved drafts), and if a job was changed elsewhere since you loaded it, Save writes nothing and shows what changed.

## Changing your searches

Edit `config/searches.toml` ([settings reference](docs/searches.md)), then run:

```
uv run jobflow update-searches
```

It checks the file (an invalid one stops everything, so a typo never reaches the daily import), updates the copy the daily GitHub import reads, and filters jobs already in your Inbox whose titles match your `exclude_title_terms` (unless they match `include_title_terms`). Add `--dry-run` to check and preview without changing anything. Filtered jobs go to the Filtered tab.

## Commands

| Command | What it does |
|---|---|
| `jobflow web [--port N]` | The job tracker in your browser |
| `jobflow tailor-cv ID [--cv VARIANT]` | A tailored CV draft and its fact check |
| `jobflow interview-prep ID [--email-file FILE]` | An interview briefing |
| `jobflow update-searches [--dry-run]` | After editing your searches: check, publish to GitHub, filter the Inbox |
| `jobflow ingest [--search NAME] [--site SITE]` | Run the searches now (the daily import does this; uses Apify credits) |
| `jobflow init` | Create your settings files (first time only) |

Every command except `init` brings the database tables up to date when it starts.

## Privacy

- `config/.env`, `config/profile.toml`, `config/searches.toml`, `output/`, and `private/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results, which include short quotes from your CVs: keep it private.
- Scoring, tailoring, and interview prep send CV, job, and email text to your LLM provider, and the company name and job title to Tavily. Interview emails are never stored. Choose providers whose data handling you accept.

## Development

```
uv sync --extra documents --extra test
uv run pytest
```

The tests need no database or network.
