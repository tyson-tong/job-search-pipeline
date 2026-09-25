# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and/or Seek (through Apify scrapers; use any combination) into your own PostgreSQL database, merging the same job found on several sites. On your machine, it then scores each job against your CVs, drafts tailored CVs, and builds interview briefings.

It is a template: each person makes their own private copy with their own database, credentials, and CVs. This repository contains no CVs or credentials.

## How it works

```
Daily, in GitHub Actions                     On your machine, when you choose
─────────────────────────                    ────────────────────────────────
config/searches.toml ─► Apify ─► filter + dedupe ─► PostgreSQL ─► jobflow web                 score, browse, and track jobs
                                                           jobflow tailor-cv ID        draft a CV for one job
                                                           jobflow interview-prep ID   briefing before an interview
```

Your CVs never leave your machine, except that scoring, tailoring, and interview prep send relevant CV, job, and email text to your LLM and web-search providers.

For a diagram of every component, table, and decision, open [`docs/structure.html`](docs/structure.html) in a browser.

## Routine

Open a terminal in this folder. Run commands with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the `uv run` prefix). `jobflow` reads `config/.env` and `config/profile.toml` from this folder by itself. The examples below omit the prefix.

1. **Open the job tracker.**

   ```
   jobflow web
   ```

   It opens in your browser, runs on your machine only (http://127.0.0.1:8765), and reads and writes your database directly. This morning's scheduled run has already imported the new jobs.

2. **Score the new jobs.** Click **Score unscored jobs**. Each open job (not closed; see the statuses below) that hasn't been scored against your current CVs is compared with each CV by the LLM, up to 50 per run, newest first. It asks first, stating how many LLM calls that is, and you can keep working while it runs. Unchanged CVs are never re-scored; **Re-score** in a job's panel forces it.

3. **Pick jobs worth applying to.**
   - **Inbox**: newly scraped jobs, best match first, with one-click Interested / Not interested.
   - **My jobs**: Interested and Applied by default; the status menu selects any others. Search, filter by site or minimum salary, sort by match, posted date, date added, or follow-up date, and export the view as CSV.
   - **Pipeline counts** at the top (click one to filter), and overdue follow-ups.
   - **Job panel** (click a title): the ad, a link to each site it's listed on, why it scored as it did (each requirement matched, partial, or not evidenced, with the CV quotes), possible duplicates, notes, status history, and the tailor-cv and interview-prep commands for that job, ready to copy.

4. **Track each application** in the tracker: status, follow-up date, and dated notes. Changes are drafts, highlighted until you click **Save** (or press Ctrl+S); nothing is written before that, and leaving with unsaved changes asks first. Save writes all drafts in one transaction, then reloads the view.

   The page refreshes itself when you return to its tab and every 3 minutes, but never while you have unsaved drafts; the header shows when it last updated. If a job you edited was changed elsewhere in the meantime (for example in another browser tab), Save writes nothing and shows what changed: save yours anyway, or keep your drafts and review the latest data. Rows warn when a job may be the same role as one you've already applied for (**possible duplicates**: similar title and ad text, such as a recruiter re-posting the employer's ad under its own name), and flag overdue follow-ups and applications with no progress after three weeks.

   Statuses: `new` (the Inbox), `interested`, `not_interested`, `applied`, `interviewing`, `offered`, `accepted`, `declined`, `rejected`, `withdrawn`, and `filtered` (set on import when a job fails your search filters; the job panel shows why). Not interested, accepted, declined, rejected, withdrawn, and filtered are closed: hidden from the default view and never scored. The date you first applied is kept, and every status change is recorded with its date.

5. **Draft a CV for a job you want to apply for.**

   ```
   jobflow tailor-cv 123              # uses the best-matching CV
   jobflow tailor-cv 123 --cv dev     # or choose one: CV_dev.yaml is "dev"
   ```

   Output goes to `output/tailored-cvs/123-company-title/`:
   - `CV_<variant>_draft.yaml`: the draft.
   - `REVIEW_REQUIRED.md`: the automated fact check. Errors mean something factual changed, and the PDF is not rendered.
   - The rendered PDF, if RenderCV is installed and the check found no errors.

   Before drafting, it lists any possible duplicates of the job and warns if one is already applied for. Read the draft line by line. After fixing it, render it from its folder, so RenderCV's output (a `rendercv_output/` folder by default) is created there:

   ```
   cd output/tailored-cvs/<folder> && rendercv render CV_<variant>_draft.yaml
   ```

6. **Prepare for an interview.**

   ```
   jobflow interview-prep 123 --email-file ./invite.txt   # with the invitation email
   jobflow interview-prep 123                             # without an email
   pbpaste | jobflow interview-prep 123 --email-file -    # email from the clipboard (macOS)
   ```

   Output is one HTML file in `output/interviews/`; open it in a browser. It covers the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps to prepare for, questions to ask, and a 30-minute checklist. With an email it also covers interview logistics. The email is used only for that run and is never saved.

## Commands

First time: `jobflow init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the templates (never overwrites).

Everyday:

| Command | What it does | Needs |
|---|---|---|
| `jobflow web [--port N]` | Job tracker in your browser: score jobs against your CVs, browse and filter, track applications (saves on Save), and check your searches. | Database (LLM and CVs to score) |
| `jobflow tailor-cv ID [--cv VARIANT]` | Writes a tailored CV draft and fact check. | Database, LLM, CVs |
| `jobflow interview-prep ID [--email-file FILE]` | Writes an interview briefing HTML. | Database, LLM, CVs, Tavily |

These three bring the database tables up to date themselves when they start.

Run by the daily GitHub Action (you rarely need them):

| Command | What it does | Needs |
|---|---|---|
| `jobflow migrate` | Creates or updates the database tables. Safe to rerun. | Database |
| `jobflow ingest [--search NAME] [--site SITE]` | Runs your searches on Apify and imports the results. Run it yourself only to test a changed search now instead of waiting for tomorrow; it uses Apify credits. | Database, Apify, `config/searches.toml` |

## One-time setup

You need [uv](https://docs.astral.sh/uv/), a PostgreSQL database reachable from the internet (for example Neon, Supabase, or RDS), an [Apify](https://apify.com) account, an API key for an OpenAI-compatible LLM, and a [Tavily](https://tavily.com) API key. [RenderCV](https://docs.rendercv.com) is optional and is installed below.

1. **Make your own copy.** Create a *private* repository from this template (GitHub: "Use this template"), then clone it.

2. **Install and create your settings files.**

   ```
   uv sync --extra documents
   uv run jobflow init
   ```

   `init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the `.example` templates, with comments on what to fill in. It never overwrites a file that already exists, so it's safe to rerun. The personal files are ignored by Git, and `config/.env` is readable only by you.

3. **Point it at your CVs.** In `config/profile.toml`, set `cv_directory` to the folder containing your RenderCV files. Keep that folder outside this repository. Files must be named `CV_<variant>.yaml`; the variant name is what `--cv` takes. `config/profile.toml` also sets where `config/searches.toml` lives, the output folders, the scoring weights, and your timezone.

4. **Add credentials.** In `config/.env`, fill in:

   | Variable | Used for |
   |---|---|
   | `DATABASE_URL` | Everything. Use a TLS connection string (`sslmode=require`). |
   | `APIFY_TOKEN` | `jobflow ingest` only (Apify Console → Settings → API & Integrations) |
   | `LLM_API_KEY`, `LLM_MODEL` | scoring (in the tracker), tailor-cv, interview-prep |
   | `LLM_BASE_URL` | Only for providers other than OpenAI |
   | `WEB_SEARCH_API_KEY` | interview-prep (Tavily) |
   | `APIFY_MAX_WAIT_MINUTES` | Optional: how long to wait for each Apify run (default 60) |

   `jobflow` reads `config/.env` from the current folder (or this repository's folder) automatically. Variables already set in your shell take precedence.

   The database tables are created automatically the first time you run `jobflow web`.

5. **Define your searches.** Edit `config/searches.toml`: it starts with example searches, so replace them before the daily import runs. Each `[[search]]` has:

   | Setting | Example | Notes |
   |---|---|---|
   | `name` | `"data-analyst"` | Required and unique; used by `--search` and in run history |
   | `terms` | `["data analyst", "bi analyst"]` | Each term is searched separately |
   | `locations` | `{ AU = ["Sydney", "Melbourne"], NZ = [] }` | ISO country code → cities; `[]` = whole country |
   | `salary_min`, `salary_max`, `salary_period` | `100000`, `160000`, `"annual"` | Local currency. Jobs without a listed salary are kept |
   | `job_types` | `["full-time", "contract"]` | full-time, part-time, contract, casual |
   | `work_arrangements` | `["hybrid", "remote"]` | on-site, hybrid, remote |
   | `classifications` | `["information-communication-technology"]` | Seek only; the tracker's Searches tab lists them all |
   | `sites` | `["seek", "linkedin"]` | Which job sites to search; see below |
   | `posted_within_days` | `1` | 1 (24 hours), 7 (1 week), 14 (2 weeks), or 30 (1 month); see below |
   | `max_results`, `max_cost_usd`, `radius_km` | `30`, `0.50`, `25` | Usually set once under `[defaults]` |

   **Choosing job sites.** Use any combination of `linkedin`, `indeed`, and `seek`. Set `sites` under `[defaults]` for all searches, and override it in a search that needs different sites. If `sites` is left out everywhere, all three are used. For example, to use only Seek and Indeed, except LinkedIn too for one search:

   ```toml
   [defaults]
   sites = ["seek", "indeed"]

   [[search]]
   name = "data-analyst"
   sites = ["seek", "indeed", "linkedin"]
   ...
   ```

   A site you don't list is never called, so it costs nothing. You don't need an account per site; all three scrapers run on your one Apify account. To try a single site once without editing the file, use `jobflow ingest --site seek`.

   **Posted-date window.** Use `1` for the daily schedule; a longer window suits a first run. Not every site offers every window:

   | | LinkedIn | Indeed | Seek |
   |---|---|---|---|
   | 24 hours, 1 week | ✓ | ✓ | ✓ |
   | 2 weeks | uses 1 month | ✓ | ✓ |
   | 1 month | ✓ | uses any time | ✓ |

   A site without your window searches the next wider one, so no jobs are missed, and jobs posted before your window are marked `filtered` after import. The tracker's Searches tab notes when this happens. You still pay Apify for those extra results.

   The three sites don't support the same filters, so each is translated to what the site can do:

   | | LinkedIn | Indeed | Seek |
   |---|---|---|---|
   | Terms, country, city, date posted | on site | on site | on site |
   | Salary, job type, work arrangement | after import | after import | on site |
   | Classification | no | no | on site |

   "On site" filters are applied by the job site, so you don't pay for excluded jobs. "After import" filters are applied when results reach your database. They only exclude a job whose salary, job type, or arrangement is listed and doesn't match; the job is stored with status `filtered` and the reason. LinkedIn has moved to AI search, which dropped its salary, job-type, and workplace filters, and the Indeed actor doesn't offer them. LinkedIn results rarely list a salary and never say whether a job is remote, so in practice those filters seldom remove LinkedIn jobs. Seek applies its salary filter to the range the advertiser entered, even when that range is hidden on the ad.

   Check the result in the tracker: open `jobflow web` and the **Searches** tab. It validates every value (listing the allowed ones if something is wrong), shows each Apify run with the exact input each actor receives, and lists all allowed values; click **Reload** after editing. Nothing runs from that page. All option values are constants in `src/jobflow/ingest/options.py`.

   Cost: LinkedIn makes one run per search, with a result cap per term and location; Indeed and Seek make one run per term and location. The actors charge per result, including jobs you already have, so keep `posted_within_days = 1` for a daily schedule and set `max_results`. Each run's actual cost is recorded in `scraper_runs.cost_usd`.

6. **Schedule the daily import.** In your GitHub repository, open Settings → Secrets and variables → Actions and add:
   - Secret `DATABASE_URL`
   - Secret `APIFY_TOKEN`
   - Variable `JOBFLOW_SEARCHES`: the contents of your `config/searches.toml`. Set it, and update it after each edit, with `gh variable set JOBFLOW_SEARCHES < config/searches.toml`

   Then open Actions → "Daily job ingestion" → **Run workflow** to test it once. After that it runs every day at 06:30 Sydney time (07:30 during daylight saving). To change the time, edit the cron line in `.github/workflows/daily-ingest.yml`; cron times are in UTC. The LLM and Tavily keys are not needed in GitHub.

## How it decides

**Duplicates.** Each scraper's output is first converted to the same fields (`src/jobflow/ingest/sources.py`). A job then counts as already seen if any of these match, in order:
1. Same site and job ID (or, for a posting without an ID, the same URL without tracking parameters), checked against every site's copy seen so far, not just the first.
2. Identical company, title, location, and description.
3. Same employer and title, ignoring suffixes like "Pty Ltd", in the same city, first seen in the last 90 days, with descriptions that share at least half their words. This catches the same job on LinkedIn, Indeed, and Seek, and reposts with a new ID. "Same city" only rules out a match when both postings name different places, such as Sydney and Melbourne; a missing or country-only location doesn't. Suburbs count as different places, so a Parramatta and a Sydney listing of one job may stay separate.

The job keeps the first site's details (and the longest description). Each site's copy, with its own ID and link, is kept in the `job_sources` table, so later sightings match by ID and you can open the job on any site it was listed on. Every sighting is also recorded with the scraper run it came from.

**Possible duplicates.** Merging is kept strict so two different jobs are never combined, which means some copies stay separate: a recruiter re-post under another employer name, a reworded title, or a suburb instead of the city. As a safety net, the tracker and `jobflow tailor-cv` compare the job with every job first seen in the last 180 days, ignoring employer and location: titles sharing at least 60% of their words and ads sharing at least 40% are listed as possible duplicates. They are never merged automatically.

**Match scores.** The LLM lists the job's requirements, marks each required or preferred, and labels it matched, partial, or not evidenced in each CV, with CV quotes. Every quote is checked against the CV file. A "matched" item whose quote isn't found becomes "partial", and a "partial" one becomes "not evidenced". "Not evidenced" means the CV doesn't show it, not that you lack it. The score is calculated, not asked of the LLM: matched counts 1, partial 0.5, not evidenced 0, averaged within required and within preferred, then weighted by `required_weight` and `preferred_weight` in `config/profile.toml`. Use scores for ranking, not as a prediction.

**Tailored CVs.** Only the CV's `cv` section goes to the LLM; design, locale, and settings are copied from your file. The draft is then compared with your original CV. These count as errors, and the PDF is not rendered:
- changed contact details
- new or altered employers, positions, dates, or education
- a removed employer or education entry
- numbers that aren't in the original

New capitalised or technical terms, such as a tool name, are warnings. The check catches common problems; it does not replace reading the draft.

**Failures.** If one Apify run fails, the error is recorded in the `scraper_runs` table, with the search name and the exact input, and the other runs still go ahead. The GitHub workflow then shows as failed, so you get GitHub's failure email.

## Privacy

- `config/.env`, `config/profile.toml`, `config/searches.toml`, `output/`, and `private/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results. Match results include short quotes from your CVs, so keep the database private.
- Interview emails are never stored. CV, job, and email text is sent to your LLM provider, and company names to Tavily, when you run the commands; choose providers whose data handling you accept.
- If you ever move CV processing to the cloud, keep CVs in a private per-person storage bucket, not in PostgreSQL or GitHub variables.

## Development

```
uv sync --extra documents --extra test
uv run pytest
```

The tests need no database or network.
