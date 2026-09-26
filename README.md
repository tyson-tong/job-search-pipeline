# Job Search Workflow

A personal job-search pipeline. Every day it collects job postings from LinkedIn, Indeed, and/or Seek (through Apify scrapers; use any combination) into your own PostgreSQL database, merging the same job found on several sites. On your machine, you triage the new jobs, score the ones you're interested in against your CVs, draft tailored CVs, and build interview briefings.

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

2. **Pick jobs worth applying to.**
   - **Inbox**: newly scraped jobs, most recently added first. A job here can only be marked Interested or Not interested; the other statuses are for My jobs. Tick several rows (shift-click selects a range) to mark them all at once.
   - **My jobs**: jobs you marked Interested and everything after (Interested and Applied by default; the status menu selects the others). Not interested and filtered jobs never appear here. Search, filter by site or minimum salary, sort by match, posted date, date added, or follow-up date, and export the view as CSV.
   - **Filtered**: jobs your search filters kept out of the Inbox, each with its reason ("title contains 'Trader'", a salary below your minimum). **Move to Inbox** brings one back, or tick several to move them together; saved with Save like any other change.
   - **Pipeline counts** at the top (click one to filter), and overdue follow-ups.
   - **Job panel** (click a title): the ad, a link to each site it's listed on, why it scored as it did (each requirement matched, partial, or not evidenced, with the CV quotes), similar jobs, notes, status history, and the tailor-cv and interview-prep commands for that job, ready to copy.

3. **Score the jobs you're interested in.** After saving your picks, click **Score all interested jobs** (greyed out when you have no Interested jobs, when all of them have already been scored, or while a scoring run is in progress). Each Interested job that has never been scored is compared with each of your CVs by the LLM, newest first. It asks first, stating how many LLM calls that is, and you can keep working while it runs. Only Interested jobs are ever scored (New, Not interested, and every other status are not), and a job is scored once: it keeps its scores even if you later change a CV. An unscored Interested job can also be scored from its panel.

4. **Track each application** in the tracker: status, follow-up date, and dated notes. Changes are drafts, highlighted until you click **Save** (or press Ctrl+S); nothing is written before that, and leaving with unsaved changes asks first. Save writes all drafts in one transaction, then reloads the view.

   The page refreshes itself when you return to its tab and every 3 minutes, but never while you have unsaved drafts; the header shows when it last updated. If a job you edited was changed elsewhere in the meantime (for example in another browser tab), Save writes nothing and shows what changed: save yours anyway, or keep your drafts and review the latest data. Rows tag **similar jobs** (similar title and ad text, such as a recruiter re-posting the employer's ad under its own name): "highly similar" when titles are at least 60% alike and ads at least 80%, otherwise "similar" (titles at least 60%, ads at least 40%). Rows also warn when a job may be the same role as one you've already applied for, and flag overdue follow-ups and applications with no progress after three weeks. Similar jobs in the same list are shown together, marked by an orange bar down the left; the same ad in another country sits alongside, marked by a blue bar.

   Statuses: `new` (the Inbox), `interested`, `not_interested`, `applied`, `interviewing`, `offered`, `accepted`, `declined`, `rejected`, `withdrawn`, and `filtered` (set on import, or by `jobflow update-searches`, when a job fails your search filters; the Filtered tab shows why). Not interested, accepted, declined, rejected, withdrawn, and filtered are closed: hidden from the default view. Not interested jobs leave the tracker altogether (they stay in the database), so undo a misclick before you click Save. Filtered jobs are listed in the Filtered tab, from where they can go back to the Inbox. The date you first applied is kept, and every status change is recorded with its date.

5. **Draft a CV for a job you want to apply for.**

   ```
   jobflow tailor-cv 123              # uses the best-matching CV (scoring the job first if it's Interested)
   jobflow tailor-cv 123 --cv dev     # or choose one: CV_dev.yaml is "dev"
   ```

   Output goes to `output/<company>/<role>-<job-id>/`:
   - `CV_<variant>_draft.yaml`: the draft.
   - `REVIEW_REQUIRED.md`: the automated fact check. Errors mean something factual changed, and the PDF is not rendered.
   - The rendered PDF, if RenderCV is installed and the check found no errors.

   Before drafting, it lists any similar jobs and warns if one is already applied for. Read the draft line by line. After fixing it, render it from its folder, so RenderCV's output (a `rendercv_output/` folder by default) is created there:

   ```
   cd output/<company>/<role>-<job-id> && rendercv render CV_<variant>_draft.yaml
   ```

6. **Prepare for an interview.**

   ```
   jobflow interview-prep 123 --email-file ./invite.txt   # with the invitation email
   jobflow interview-prep 123                             # without an email
   pbpaste | jobflow interview-prep 123 --email-file -    # email from the clipboard (macOS)
   ```

   Output is `interview-prep.html` directly in `output/<company>/<role>-<job-id>/`; open it in a browser. It covers the company (with web sources), the role's priorities, evidence from your CVs, likely questions with answer outlines, gaps to prepare for, questions to ask, and a 30-minute checklist. With an email it also covers interview logistics. The email is used only for that run and is never saved.

## Commands

First time: `jobflow init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the templates (never overwrites).

Everyday:

| Command | What it does | Needs |
|---|---|---|
| `jobflow web [--port N]` | Job tracker in your browser: score jobs against your CVs, browse and filter, track applications (saves on Save), and check your searches. | Database (LLM and CVs to score) |
| `jobflow tailor-cv ID [--cv VARIANT]` | Writes a tailored CV draft and fact check. | Database, LLM, CVs |
| `jobflow interview-prep ID [--email-file FILE]` | Writes an interview briefing HTML. | Database, LLM, CVs, Tavily |
| `jobflow update-searches [--dry-run]` | After editing `config/searches.toml`: checks it, updates the GitHub copy the daily import reads, and filters Inbox jobs matching your `exclude_title_terms`. See [Changing your searches](#changing-your-searches). | Database, `config/searches.toml`, [GitHub CLI](https://cli.github.com) |

Every command except `init` brings the database tables up to date itself when it starts.

Run by the daily GitHub Action (you rarely need it):

| Command | What it does | Needs |
|---|---|---|
| `jobflow ingest [--search NAME] [--site SITE]` | Runs your searches on Apify and imports the results. Run it yourself only to test a changed search now instead of waiting for tomorrow; it uses Apify credits. | Database, Apify, `config/searches.toml` |

## Changing your searches

After editing `config/searches.toml` (for example switching an `exclude_title_terms` entry on or off), run:

```
uv run jobflow update-searches
```

It does three things, in order:
1. Checks the file. If anything is invalid it stops with the reason and changes nothing, so a typo never reaches the daily import.
2. Updates the `JOBFLOW_SEARCHES` GitHub variable, the copy the daily import reads (through the [GitHub CLI](https://cli.github.com), `gh`, logged in with `gh auth login`). Without this, tomorrow's import still runs your old searches.
3. Filters jobs already in your Inbox whose titles match your excluded terms, since imports only apply them to the jobs they bring in.

Add `--dry-run` to check the file and see which jobs it would filter, without updating GitHub or your Inbox. Filtered jobs go to the tracker's Filtered tab, where **Move to Inbox** brings one back; switching a term off doesn't bring back jobs it already filtered. Click **Reload** on the Searches tab to check the file.

## One-time setup

You need [uv](https://docs.astral.sh/uv/), a PostgreSQL database reachable from the internet (for example Neon, Supabase, or RDS), an [Apify](https://apify.com) account, an API key for an OpenAI-compatible LLM, and a [Tavily](https://tavily.com) API key. [RenderCV](https://docs.rendercv.com) is optional and is installed below.

1. **Make your own copy.** Create a *private* repository from this template (GitHub: "Use this template"), then clone it.

2. **Install and create your settings files.**

   ```
   uv sync --extra documents
   uv run jobflow init
   ```

   `init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the `.example` templates, with comments on what to fill in. It never overwrites a file that already exists, so it's safe to rerun. The personal files are ignored by Git, and `config/.env` is readable only by you.

3. **Point it at your CVs.** In `config/profile.toml`, set `cv_directory` to the folder containing your RenderCV files. Keep that folder outside this repository. Files must be named `CV_<variant>.yaml`; the variant name is what `--cv` takes. `config/profile.toml` also sets where `config/searches.toml` lives, the shared output root, the scoring weights, and your timezone.

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

   The database tables are created automatically the first time you run any command other than `init`, such as `jobflow web`.

5. **Define your searches.** Edit `config/searches.toml`: it starts with example searches, so replace them before the daily import runs. Each `[[search]]` has:

   | Setting | Example | Notes |
   |---|---|---|
   | `name` | `"data-analyst"` | Required and unique; used by `--search` and in run history |
   | `terms` | `["data analyst", "bi analyst"]` | Each term is searched separately |
   | `locations` | `{ AU = ["Sydney"], SG = ["Singapore"], HK = ["Hong Kong"] }` | Country code → cities from the list below; `[]` = whole country |
   | `salary_min`, `salary_max`, `salary_period` | `100000`, `160000`, `"annual"` | Local currency. Jobs without a listed salary are kept |
   | `job_types` | `["full-time", "contract"]` | full-time, part-time, contract, casual |
   | `work_arrangements` | `["hybrid", "remote"]` | on-site, hybrid, remote |
   | `classifications` | `["information-communication-technology"]` | Seek only; the tracker's Searches tab lists them all |
   | `sites` | `["seek", "linkedin"]` | Which job sites to search; see below |
   | `posted_within_days` | `1` | 1 (24 hours), 7 (1 week), 14 (2 weeks), or 30 (1 month); see below |
   | `max_results`, `max_cost_usd`, `radius_km` | `30`, `0.50`, `25` | Usually set once under `[defaults]` |
   | `exclude_title_terms` | `["Internship", "Fresh Grad", "Graduate"]` | Jobs whose title contains any of these words are stored as `filtered` and never reach the Inbox. Whole words, any capitalisation and punctuation ("Graduate" excludes "Graduate Analyst", not "Postgraduate"). Usually set under `[defaults]`; imports apply it to new jobs, and `jobflow update-searches` to jobs already in the Inbox |

   **Countries and cities for `locations`.** Cities must come from this list (any capitalisation); a misspelt or unlisted city is rejected rather than searched. Use `[]` to search the whole country.

   | Code | Country | Cities | Sites |
   |---|---|---|---|
   | `AU` | Australia | Sydney, Melbourne, Brisbane, Perth, Adelaide, Canberra | all three |
   | `NZ` | New Zealand | Auckland, Christchurch, Wellington | all three |
   | `SG` | Singapore | Singapore | all three |
   | `HK` | Hong Kong | Hong Kong | all three |
   | `GB` (or `UK`) | United Kingdom | London, Manchester, Birmingham, Edinburgh, Glasgow, Leeds, Bristol | LinkedIn, Indeed |
   | `US` | United States | New York, San Francisco, Los Angeles, Chicago, Boston, Seattle, Houston | LinkedIn, Indeed |

   Seek has no UK or US site, so it skips those countries. The lists are `CITIES` in `src/jobflow/ingest/options.py`; to add a city or country, edit it there. The template's active search covers Sydney, with Singapore and Hong Kong searches commented out.

   For different salary cutoffs in AUD, SGD, and HKD, make one `[[search]]` entry per country and put `salary_min`, `salary_max`, and `salary_period` inside that country’s entry. The amounts are interpreted in the posting’s local currency; the workflow does not convert currencies. Leave salary fields out to disable salary filtering for that country. Separate searches may increase LinkedIn scraper runs because LinkedIn runs once per search.

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
   | Salary | after import | after import | after import |
   | Job type, work arrangement | after import | after import | on site |
   | Classification | no | no | on site |
   | Excluded title terms | after import | after import | after import |

   "On site" filters are applied by the job site, so you don't pay for excluded jobs. "After import" filters are applied when results reach your database. They only exclude a job whose salary, job type, or arrangement is listed and doesn't match, or whose title contains an excluded term; the job is stored with status `filtered` and the reason, and appears in the tracker's Filtered tab. LinkedIn has moved to AI search, which dropped its salary, job-type, and workplace filters, and the Indeed actor doesn't offer them. LinkedIn results rarely list a salary and never say whether a job is remote, so in practice those filters seldom remove LinkedIn jobs. Salary bounds are applied after import for all three sites. Missing salary data is kept; Seek is deliberately scraped without salary bounds so it cannot hide salaryless postings upstream. This can increase the number of Seek results and Apify usage.

   Check the result in the tracker: open `jobflow web` and the **Searches** tab. It validates every value (listing the allowed ones if something is wrong), shows each Apify run with the exact input each actor receives, and lists all allowed values; click **Reload** after editing. Nothing runs from that page. All option values are constants in `src/jobflow/ingest/options.py`.

   Cost: LinkedIn makes one run per search, with a result cap per term and location; Indeed and Seek make one run per term and location. The actors charge per result, including jobs you already have, so keep `posted_within_days = 1` for a daily schedule and set `max_results`. Each run's actual cost is recorded in `scraper_runs.cost_usd`.

6. **Schedule the daily import.** In your GitHub repository, open Settings → Secrets and variables → Actions and add:
   - Secret `DATABASE_URL`
   - Secret `APIFY_TOKEN`
   - Variable `JOBFLOW_SEARCHES`: the contents of your `config/searches.toml`. Set it with `gh variable set JOBFLOW_SEARCHES < config/searches.toml`; after each later edit, `jobflow update-searches` updates it (see [Changing your searches](#changing-your-searches))

   Then open Actions → "Daily job ingestion" → **Run workflow** to test it once. After that it runs every day at 06:30 Sydney time (07:30 during daylight saving). To change the time, edit the cron line in `.github/workflows/daily-ingest.yml`; cron times are in UTC. The LLM and Tavily keys are not needed in GitHub.

## How it decides

**Duplicates.** Each scraper's output is first converted to the same fields (`src/jobflow/ingest/sources.py`). A job then counts as already seen if any of these match, in order:
1. Same site and job ID (or, for a posting without an ID, the same URL without tracking parameters), checked against every site's copy seen so far, not just the first.
2. Identical company, title, location, and description.
3. Same employer and title, ignoring suffixes like "Pty Ltd", in the same city, first seen in the last 90 days, with descriptions that share at least half their words. This catches the same job on LinkedIn, Indeed, and Seek, and reposts with a new ID. "Same city" only rules out a match when both postings name different places, such as Sydney and Melbourne; a missing or country-only location doesn't. Suburbs count as different places, so a Parramatta and a Sydney listing of one job stay separate unless rule 4 applies.
4. The same title (ignoring case and punctuation), in the same country, first seen in the last 90 days, with an ad at least 95% the same words, whatever the employer's spelling or the city. This catches one ad listed under slightly different employer names or districts. Short ads (under 30 distinct words) never match this way, a different title never does (employers reuse one ad for different roles), and neither does one ad for offices in two countries. A job's country is the one its search run covered, or else the country named in its location; an unknown country doesn't block a match.

The job keeps the first site's details (and the longest description). Each site's copy, with its own ID and link, is kept in the `job_sources` table, so later sightings match by ID and you can open the job on any site it was listed on. Every sighting is also recorded with the scraper run it came from.

**Similar jobs.** Merging is kept strict so two different jobs are never combined, which means some copies stay separate: a recruiter re-post with an edited ad, a reworded title, or a suburb instead of the city. As a safety net, the tracker and `jobflow tailor-cv` compare the job with every job first seen in the last 180 days in the same country, ignoring employer and city: titles sharing at least 60% of their words and ads sharing at least 40% are listed as similar jobs, and as highly similar when the ads share at least 80%. They are never merged automatically. The same ad in another country (one role advertised for offices in Sydney and Hong Kong, say) is not listed as similar: it is placed next to the job in the list with a blue bar (similar jobs get an orange one), tagged "Same ad in" that country, and listed separately in the job panel.

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
- Interview emails are never stored. CV, job, and email text is sent to your LLM provider, and the company name and job title to Tavily, when you run the commands; choose providers whose data handling you accept.
- If you ever move CV processing to the cloud, keep CVs in a private per-person storage bucket, not in PostgreSQL or GitHub variables.

## Development

```
uv sync --extra documents --extra test
uv run pytest
```

The tests need no database or network.
