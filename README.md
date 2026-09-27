# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and Seek (through Apify scrapers) into your own PostgreSQL database, merging the same job found on several sites. On your machine, you triage the new jobs, score the ones you're interested in against your CVs, and draft tailored CVs and interview briefings for the jobs you're interviewing for.

It is a template: each person makes their own private copy with their own database, credentials, and CVs. **First time? Follow [docs/setup.md](docs/setup.md).**

```
Daily, in GitHub Actions                     On your machine, when you choose
─────────────────────────                    ────────────────────────────────
config/searches.toml ─► Apify ─► filter + dedupe ─► PostgreSQL ─► jobflow web                 triage, score, and track jobs
                                                           jobflow tailor-cv           draft CVs for Interviewing jobs
                                                           jobflow interview-prep      briefings for Interviewing jobs
```

More: [your searches](docs/searches.md) · [rules](docs/rules.md) (duplicates, similar jobs, scores, CV checks) · [structure](docs/structure.html) (open in a browser).

## Daily routine

Run commands from this folder with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the prefix).

1. **Open the tracker:** `uv run jobflow web`. It runs on your machine only (http://127.0.0.1:8765); last night's import has already brought in the new jobs.

2. **Triage the Inbox.** Mark each new job **Interested** or **Not interested**, one at a time or by ticking several rows (shift-click selects a range). Changes are drafts until you click **Save** (or press Ctrl+S). Interested jobs move to **My jobs**; Not interested jobs leave the tracker (they stay in the database), so undo a misclick before saving.

3. **Score your picks.** In **My jobs**, click **Score all jobs**: each Interested job not yet scored is compared with each of your CVs by the LLM. A job is scored once and keeps its scores even if a CV changes. Only Interested jobs are ever scored.

4. **Track applications** in **My jobs**: status (applied, interviewing, offered, accepted, declined, rejected, withdrawn), follow-up date, and dated notes, all saved with **Save**. Rows flag overdue follow-ups and applications with no progress after three weeks.

5. **Draft CVs and interview briefings** for the jobs you've set to **Interviewing**:

   ```
   uv run jobflow tailor-cv              # a CV draft for each, from its best-matching CV
   uv run jobflow tailor-cv --cv dev     # or use one CV for all: CV_dev.yaml is "dev"
   uv run jobflow interview-prep         # a briefing for each
   ```

   Each command works through every Interviewing job and skips jobs it has already done, so run them whenever a job moves to Interviewing. A job that fails (a network error, say) is retried the next time you run the command. Without `--cv`, a job needs a match score (scored while it was Interested); an unscored one fails with a message until you give `--cv`.

   `tailor-cv` writes `CV_<variant>_draft.yaml` and a fact check, `REVIEW_REQUIRED.md`, to `output/<company>/<role>-<job-id>/`, and renders a PDF if RenderCV is installed and the check found no errors. Read the draft line by line; after fixing it, render it from that folder: `rendercv render CV_<variant>_draft.yaml`.

   `interview-prep` writes `interview-prep.html` to the same folder: the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps, questions to ask, and a 30-minute checklist.

### In the tracker

- **Tabs:** **Inbox** (new jobs), **My jobs** (Interested onwards; the status menu picks which statuses, **Default** restores them), **Filtered** (jobs your search filters kept out, with the reason; **Move to Inbox** brings one back), and **Searches** (checks your searches file).
- **Filters** under the column headings: minimum match score, a title/company/location search, country (tick several), site, when the job was posted (past day, week, 2 weeks, or month, or over a month ago), and a minimum salary in US dollars (converted from each job's currency; jobs without a salary are kept). Click **Posted** or **Salary** to sort, again to reverse. **Export CSV** exports the filtered view.
- **Job panel** (click a title): the ad, links to each site it's listed on, why it scored as it did, similar jobs, notes, status history, and the tailor-cv and interview-prep commands, ready to copy.
- **Similar jobs** sit together with an orange bar ("highly similar" or "similar"; a similar job not in the list shown is counted with where it is, such as "1 highly similar (Filtered)"), and a row warns if it may be a role you've already applied for; the same ad in another country sits alongside with a blue bar. See [rules](docs/rules.md#similar-jobs).
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
| `jobflow tailor-cv [--cv VARIANT]` | A tailored CV draft and its fact check for each Interviewing job without one |
| `jobflow interview-prep` | An interview briefing for each Interviewing job without one |
| `jobflow update-searches [--dry-run]` | After editing your searches: check, publish to GitHub, filter the Inbox |
| `jobflow ingest [--search NAME] [--site SITE]` | Run the searches now (the daily import does this; uses Apify credits) |
| `jobflow init` | Create your settings files (first time only) |

Every command except `init` brings the database tables up to date when it starts.

## Privacy

- `config/.env`, `config/profile.toml`, `config/searches.toml`, `output/`, and `private/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results, which include short quotes from your CVs: keep it private.
- Scoring, tailoring, and interview prep send CV and job text to your LLM provider, and the company name and job title to Tavily. Choose providers whose data handling you accept.

## Development

```
uv sync --extra documents --extra test
uv run pytest
```

The tests need no database or network.
