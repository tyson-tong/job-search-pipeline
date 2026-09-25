# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and Seek (through Apify scrapers) into your own PostgreSQL database, merging the same job found on several sites. On your machine, it then scores each job against your CVs, drafts tailored CVs, and builds interview briefings.

It is a template: each person makes their own private copy with their own database, credentials, and CVs. This repository contains no CVs or credentials.

## How it works

```
Daily, in GitHub Actions                     On your machine, when you choose
─────────────────────────                    ────────────────────────────────
searches.toml ─► Apify ─► filter + dedupe ─► PostgreSQL ─► jobflow match --new   score jobs against each CV
                                             jobflow list --by-score  pick jobs worth applying to
                                             jobflow tailor-cv ID    draft a CV for one job
                                             jobflow interview-prep ID   briefing before an interview
```

Your CVs never leave your machine, except that the commands send relevant CV, job, and email text to your LLM and web-search providers.

## Routine

Open a terminal in this folder. Run commands with `uv run` (or activate `.venv` once with `. .venv/bin/activate` and drop the `uv run` prefix). `jobflow` reads `.env` and `profile.toml` from this folder by itself. The examples below omit the prefix.

1. **Score the new jobs.** This morning's scheduled run has already imported them.

   ```
   jobflow match --new
   ```

   This checks every job that hasn't been scored against your current CVs (up to 50 per run; use `--limit`). It makes one LLM call per job per CV. Unchanged CVs are not re-scored.

2. **Find the best matches.**

   ```
   jobflow list --by-score --limit 30
   ```

   Columns: job ID, best score (and which CV), posted date, company, title, location, status. The job ID in the first column is what the other commands take. Rejected, withdrawn, and ignored jobs are hidden; add `--all` to see them, or `--status applied` (repeatable) to see one status.

3. **Look at why a job scored as it did.**

   ```
   jobflow match 123
   ```

   This shows each requirement as matched, partial, or not evidenced, with the CV quotes that support it. No LLM call if the job has already been scored.

4. **Draft a CV for a job you want to apply for.**

   ```
   jobflow tailor-cv 123              # uses the best-matching CV
   jobflow tailor-cv 123 --cv dev     # or choose one: CV_dev.yaml is "dev"
   ```

   Output goes to `output/tailored-cvs/123-company-title/`:
   - `CV_<variant>_draft.yaml`: the draft.
   - `REVIEW_REQUIRED.md`: the automated fact check. Errors mean something factual changed, and the PDF is not rendered.
   - The rendered PDF, if RenderCV is installed and the check found no errors.

   Read the draft line by line. After fixing it, render it from its folder so the PDF lands next to it:

   ```
   cd output/tailored-cvs/<folder> && rendercv render CV_<variant>_draft.yaml
   ```

5. **Prepare for an interview.**

   ```
   jobflow interview-prep 123 --email-file ./invite.txt   # with the invitation email
   jobflow interview-prep 123                             # without an email
   pbpaste | jobflow interview-prep 123 --email-file -    # email from the clipboard (macOS)
   ```

   Output is one HTML file in `output/interviews/`; open it in a browser. It covers the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps to prepare for, questions to ask, and a 30-minute checklist. With an email it also covers interview logistics. The email is used only for that run and is never saved.

6. **Track where each application is.**

   ```
   jobflow status 123 applied --note "Applied via referral from Sam"
   jobflow status 456 ignored          # not interested: hidden from list, skipped by match --new
   jobflow status 123                  # show status, date applied, and notes
   ```

   Statuses: `new` (default), `interested`, `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`, `ignored`, and `filtered` (set on import when a job fails your search filters; see why with `jobflow list --status filtered` and the `filter_reason` column). Notes are dated and added to earlier ones; the date you first marked a job `applied` is kept.

## Commands

| Command | What it does | Needs |
|---|---|---|
| `jobflow migrate` | Creates or updates the database tables. Safe to rerun. | Database |
| `jobflow searches [--search NAME] [--site SITE] [--json]` | Checks `searches.toml` and lists the Apify runs it would make. Free. | Nothing |
| `jobflow options [countries\|classifications]` | Lists allowed search values and which site applies which filter. | Nothing |
| `jobflow ingest [--search NAME] [--site SITE]` | Runs your searches on Apify now and imports the results. GitHub Actions runs this daily. It uses Apify credits. | Database, Apify |
| `jobflow import-json FILE` | Imports a JSON file of jobs exported from any of the three scrapers (`-` reads stdin). | Database |
| `jobflow list [--by-score] [--limit N] [--status S] [--all]` | Lists jobs with their best match score and status. | Database |
| `jobflow status ID [STATUS] [--note TEXT]` | Shows or sets a job's application status and notes. | Database |
| `jobflow match ID` / `match --new` | Scores one job, or every open job not yet scored. `--force` re-scores. | Database, LLM, CVs |
| `jobflow tailor-cv ID [--cv VARIANT]` | Writes a tailored CV draft and fact check. | Database, LLM, CVs |
| `jobflow interview-prep ID [--email-file FILE]` | Writes an interview briefing HTML. | Database, LLM, CVs, Tavily |

## One-time setup

You need [uv](https://docs.astral.sh/uv/), a PostgreSQL database reachable from the internet (for example Neon, Supabase, or RDS), an [Apify](https://apify.com) account, an API key for an OpenAI-compatible LLM, and a [Tavily](https://tavily.com) API key. [RenderCV](https://docs.rendercv.com) is optional and is installed below.

1. **Make your own copy.** Create a *private* repository from this template (GitHub: "Use this template"), then clone it.

2. **Install.**

   ```
   uv sync --extra documents
   ```

3. **Point it at your CVs.** Copy `profile.example.toml` to `profile.toml` and set `cv_directory` to the folder containing your RenderCV files. Keep that folder outside this repository. Files must be named `CV_<variant>.yaml`; the variant name is what `--cv` takes. `profile.toml` also sets the output folders, the scoring weights, and your timezone.

4. **Add credentials.** Copy `.env.example` to `.env` and fill in:

   | Variable | Used for |
   |---|---|
   | `DATABASE_URL` | Everything. Use a TLS connection string (`sslmode=require`). |
   | `APIFY_TOKEN` | `jobflow ingest` only (Apify Console → Settings → API & Integrations) |
   | `LLM_API_KEY`, `LLM_MODEL` | match, tailor-cv, interview-prep |
   | `LLM_BASE_URL` | Only for providers other than OpenAI |
   | `WEB_SEARCH_API_KEY` | interview-prep (Tavily) |

   `jobflow` reads `.env` from the current folder automatically. Variables already set in your shell take precedence.

5. **Create the tables.**

   ```
   jobflow migrate
   ```

6. **Define your searches.** Copy `searches.example.toml` to `searches.toml` (ignored by Git) and edit it. Each `[[search]]` has:

   | Setting | Example | Notes |
   |---|---|---|
   | `terms` | `["data analyst", "bi analyst"]` | Each term is searched separately |
   | `locations` | `{ AU = ["Sydney", "Melbourne"], NZ = [] }` | ISO country code → cities; `[]` = whole country |
   | `salary_min`, `salary_max`, `salary_period` | `100000`, `160000`, `"annual"` | Local currency. Jobs without a listed salary are kept |
   | `job_types` | `["full-time", "contract"]` | full-time, part-time, contract, casual |
   | `work_arrangements` | `["hybrid", "remote"]` | on-site, hybrid, remote |
   | `classifications` | `["information-communication-technology"]` | Seek only; `jobflow options classifications` |
   | `sites`, `posted_within_days`, `max_results`, `max_cost_usd`, `radius_km` | | Usually set once under `[defaults]` |

   The three sites don't support the same filters, so each is translated to what the site can do:

   | | LinkedIn | Indeed | Seek |
   |---|---|---|---|
   | Terms, country, city, date posted | on site | on site | on site |
   | Salary, job type, work arrangement | after import | after import | on site |
   | Classification | no | no | on site |

   "On site" filters are applied by the job site, so you don't pay for excluded jobs. "After import" filters are applied when results reach your database. They only exclude a job whose salary, job type, or arrangement is listed and doesn't match; the job is stored with status `filtered` and the reason. LinkedIn has moved to AI search, which dropped its salary, job-type, and workplace filters, and the Indeed actor doesn't offer them. Seek applies its salary filter to the range the advertiser entered, even when that range is hidden on the ad.

   Check the result with `jobflow searches`: it validates every value (listing the allowed ones if something is wrong) and shows each Apify run, without running anything. Add `--json` to see the exact input each actor receives. All option values are constants in `src/jobflow/search_options.py`.

   Cost: LinkedIn makes one run per search, with a result cap per term and location; Indeed and Seek make one run per term and location. The actors charge per result, so keep `posted_within_days = 1` for a daily schedule and set `max_results`. Each run's actual cost is recorded in `scraper_runs.cost_usd`.

7. **Schedule the daily import.** In your GitHub repository, open Settings → Secrets and variables → Actions and add:
   - Secret `DATABASE_URL`
   - Secret `APIFY_TOKEN`
   - Variable `JOBFLOW_SEARCHES`: the contents of your `searches.toml`. Set it, and update it after each edit, with `gh variable set JOBFLOW_SEARCHES < searches.toml`

   Then open Actions → "Daily job ingestion" → **Run workflow** to test it once. After that it runs every day at 06:30 Sydney time (07:30 during daylight saving). To change the time, edit the cron line in `.github/workflows/daily-ingest.yml`; cron times are in UTC. The LLM and Tavily keys are not needed in GitHub.

## How it decides

**Duplicates.** Each scraper's output is first converted to the same fields (`src/jobflow/sources.py`). A job then counts as already seen if any of these match, in order:
1. Same site and job ID, or the same URL without tracking parameters.
2. Identical company, title, location, and description.
3. Same employer and title, ignoring suffixes like "Pty Ltd", first seen in the last 90 days, with descriptions that share at least half their words. This catches the same job on LinkedIn, Indeed, and Seek, and reposts with a new ID.

The job keeps the first site's details (and the longest description), and every sighting is still recorded with the scraper run it came from.

**Match scores.** The LLM lists the job's requirements, marks each required or preferred, and labels it matched, partial, or not evidenced in each CV, with CV quotes. Every quote is checked against the CV file. A "matched" item whose quote isn't found becomes "partial", and a "partial" one becomes "not evidenced". "Not evidenced" means the CV doesn't show it, not that you lack it. The score is calculated, not asked of the LLM: matched counts 1, partial 0.5, not evidenced 0, averaged within required and within preferred, then weighted by `required_weight` and `preferred_weight` in `profile.toml`. Use scores for ranking, not as a prediction.

**Tailored CVs.** Only the CV's `cv` section goes to the LLM; design, locale, and settings are copied from your file. The draft is then compared with your original CV. These count as errors, and the PDF is not rendered:
- changed contact details
- new or altered employers, positions, dates, or education
- a removed employer or education entry
- numbers that aren't in the original

New capitalised or technical terms, such as a tool name, are warnings. The check catches common problems; it does not replace reading the draft.

**Failures.** If one Apify run fails, the error is recorded in the `scraper_runs` table, with the search name and the exact input, and the other runs still go ahead. The GitHub workflow then shows as failed, so you get GitHub's failure email.

## Privacy

- `profile.toml`, `.env`, `output/`, and `private/` are ignored by Git. Never commit them.
- The database holds job postings, scraper history, and match results. Match results include short quotes from your CVs, so keep the database private.
- Interview emails are never stored. CV, job, and email text is sent to your LLM provider, and company names to Tavily, when you run the commands; choose providers whose data handling you accept.
- If you ever move CV processing to the cloud, keep CVs in a private per-person storage bucket, not in PostgreSQL or GitHub variables.

## Development

```
uv sync --extra documents --extra test
uv run pytest
```

The tests need no database or network.
