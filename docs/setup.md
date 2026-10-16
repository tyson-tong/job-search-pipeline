# Setup

Everything needed once, before the daily routine in the [README](../README.md). You need [uv](https://docs.astral.sh/uv/) and a GitHub account. `uv sync` also installs [RenderCV](https://docs.rendercv.com), which renders your CVs.

1. **Make your own copy.** Create a *private* repository from this template (GitHub: "Use this template"), then clone it.
2. **Install and create your settings files**, from the repository folder:

   ```
   uv sync
   uv run jobflow init
   ```

   `init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the `.example` templates. It never overwrites an existing file, so it's safe to rerun. These files are ignored by Git, and `init` creates `config/.env` readable only by you. If you made `config/.env` yourself, make it so with `chmod 600 config/.env`.
3. **Get your accounts and keys** (sections 1 to 5 below) and put them in `config/.env`.
4. **Add your CVs** to `cv/` (`init` creates it; Git ignores it). They can be RenderCV YAML (`.yaml`), `.tex`, `.docx`, or `.pdf`. Then **name 1 or 2 of them exactly** with `cvs` at the top of `config/profile.toml`, such as `cvs = ["CV_dev.yaml", "CV_strat.pdf"]`: jobs are scored against those (one LLM call per job per CV), and `tailor-cv`, `cover-letter`, `skill-gaps`, and `interview-prep` use only those; other files in `cv/` are ignored. `--cv` takes one of them by file name or by short name, the file name without its extension and a leading `CV_` (`CV_dev.yaml` is `dev`), which is also how scores are labelled. `cover-letter` works from any format. `tailor-cv` needs a [RenderCV](https://docs.rendercv.com) YAML CV, since it edits and fact-checks the CV's structure, so it uses your best-matching YAML CV. To keep your CVs elsewhere, set `cv_directory` in `config/profile.toml`; the same file sets the output folder, the scoring weights, and your timezone.
5. **Define your searches** in `config/searches.toml`: see [searches.md](searches.md).
6. **Schedule the daily import**: see [section 6](#6-github-secrets-and-the-daily-import), then have it started on time each night: [section 7](#7-start-the-daily-import-on-time).

## Accounts and keys

Sign-up screens and prices change, so the steps below describe what to look for rather than exact buttons, and link each provider's own documentation. Check current pricing on each provider's site before you start.

| Service | Needed for | Goes in |
|---|---|---|
| [PostgreSQL on Neon](#1-database-neon) | everything | `DATABASE_URL` in `config/.env`, and a GitHub secret |
| [Apify](#2-apify-job-scrapers) | the daily import | `APIFY_TOKEN` in `config/.env`, and a GitHub secret |
| [An LLM provider](#3-llm-provider) | scoring, tailor-cv, cover-letter, interview-prep, skill-gaps | `LLM_API_KEY`, `LLM_MODEL` (and `LLM_BASE_URL`) in `config/.env` |
| [Tavily](#4-tavily-web-search) | interview-prep; optional for tailor-cv and cover-letter | `WEB_SEARCH_API_KEY` in `config/.env` |
| [GitHub CLI](#5-github-cli) | `jobflow update-searches`, setting GitHub secrets | logged in on your machine |
| [A cron service](#7-start-the-daily-import-on-time) | starting the daily import on time | a GitHub token that can only start this repository's workflows, stored there |

Keep every key out of Git: `config/.env` is ignored by Git, and should be readable only by you (as `init` creates it). Never paste a key into `config/searches.toml`, `config/profile.toml`, or an issue.

Optional: `APIFY_MAX_WAIT_MINUTES` sets how long to wait for each Apify run (default and at most 30); a run still going then is stopped, so it can't keep charging, and recorded as failed. In `config/.env` it applies to `jobflow ingest` on your machine; for the daily import, set it as a GitHub variable too: `gh variable set APIFY_MAX_WAIT_MINUTES --body 20`. `JOBFLOW_PROFILE` names another profile file, and `CV_DIRECTORY`, `OUTPUT_DIRECTORY`, and `SEARCHES_FILE` override those paths in it; a relative one is relative to the profile's folder (`config/`), like the paths in the profile, not to the folder you run `jobflow` from. `OPENAI_API_KEY` works in place of `LLM_API_KEY`. `jobflow` reads `config/.env` by itself; variables already set in your shell take precedence. The database tables are created the first time you run a command that uses the database ([which](rules.md#database-tables)).

## 1. Database (Neon)

Any PostgreSQL database reachable from the internet works (Supabase, Amazon RDS, and others). These steps use [Neon](https://neon.tech), which has a free tier.

1. Sign up at [neon.tech](https://neon.tech) and create a project. Choose the region nearest to you; the tracker talks to the database on every page load.
2. Open the project's **Connect** (connection details) panel and copy the connection string. Use the **direct** connection, not the pooled one (the pooled host name contains `-pooler`): the direct one is the one this project is tested with, and the pooled one would lose the lock that stops two runs of `tailor-cv` (or `cover-letter`, or `interview-prep`) from working on the same jobs at once.
3. Make sure the string ends with `sslmode=require` (Neon's usually does), so the connection is encrypted. jobflow insists on it for a database on another machine: with no `sslmode` it uses `require`, and it refuses a weaker one (`disable`, `allow`, or `prefer`, which can all connect unencrypted).
4. In `config/.env`:

   ```
   DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
   ```

5. Check it: `uv run jobflow web` creates the tables on first run and opens the tracker. An error mentioning the host or password means the string is wrong.

The connection string contains your database password: treat it like one. Neon's docs: [Connect from any application](https://neon.tech/docs/connect/connect-from-any-app).

## 2. Apify (job scrapers)

The daily import runs three scrapers ("actors") from the Apify Store on your Apify account: `curious_coder/linkedin-jobs-scraper`, `curious_coder/indeed-scraper`, and `websift/seek-job-scraper`. You don't need an account on LinkedIn, Indeed, or Seek.

1. Sign up at [apify.com](https://apify.com).
2. Open each actor's page in the [Apify Store](https://apify.com/store) and read its pricing. They charge per result, including jobs you already have. Some actors ask you to start a trial or accept terms before first use; if a run fails with a permissions or rental message, open that actor's page and follow its prompt.
3. Find your API token in the Apify Console under **Settings → API & Integrations** (Apify's docs: [API tokens](https://docs.apify.com/platform/integrations/api#api-token)).
4. In `config/.env`:

   ```
   APIFY_TOKEN=apify_api_...
   ```

5. Keep costs down in `config/searches.toml`: `posted_within_days = 1` for a daily schedule, `max_results` per run, `max_cost_usd` as a hard cap per run, and only the `sites` you need. The Searches tab in the tracker shows how many runs and results a day your searches can cost, without running anything.
6. Check it once with a single site: `uv run jobflow ingest --site seek`. Each run's actual cost is recorded in the database (`scraper_runs.cost_usd`) and shown in the Apify Console.

Your Apify plan may limit how much memory runs can use at once. `jobflow ingest` runs one scraper per site at the same time (three at most); if a run fails with a memory-limit message, see your plan's limits in the Apify Console.

## 3. LLM provider

Scoring, tailored CVs, cover letters, interview briefings, and the skill-gaps report use any OpenAI-compatible chat-completions API.

**OpenAI:**

1. Sign up at [platform.openai.com](https://platform.openai.com), add billing, and set a monthly spending limit in the billing settings.
2. Create an API key (OpenAI's docs: [API keys](https://platform.openai.com/api-keys)). It is shown once: copy it straight into `config/.env`.
3. Choose a model from OpenAI's [model list](https://platform.openai.com/docs/models) that supports JSON output.
4. In `config/.env`:

   ```
   LLM_API_KEY=sk-...
   LLM_MODEL=<model name>
   LLM_BASE_URL=
   ```

**Another provider** (for example a hosted open model, or a gateway): set `LLM_BASE_URL` to its OpenAI-compatible endpoint (usually ending in `/v1`), and `LLM_API_KEY` and `LLM_MODEL` to its key and model name. Use an `https://` address, so your key isn't sent unencrypted: jobflow refuses `http://` except for a server on your own machine (`http://localhost`).

**Optional: a different model as reviewer.** `tailor-cv` and `cover-letter` have a second model call review each draft as the hiring manager would. By default that's `LLM_MODEL`; a different model makes a better critic, since it doesn't share the drafting model's blind spots. In `config/.env`:

```
LLM_REVIEW_MODEL=<model name>     # another model (required when the endpoint differs)
LLM_REVIEW_BASE_URL=              # or another provider's endpoint...
LLM_REVIEW_API_KEY=               # ...with its own key (required when the endpoint differs)
```

Leave any of them empty to use the main setting; a different endpoint needs its own key and model. `CV_REVIEW.md` and `COVER_LETTER_REVIEW.md` name the model that reviewed.

What each command sends: scoring, tailoring, cover letters, and interview prep send job text and your CV text; skill gaps sends the job requirements your CVs didn't show. See the README's [Privacy](../README.md#privacy) section, and choose a provider whose data handling you accept.

Check it: mark a job Interested, save, and click **Score all jobs** in the tracker's My jobs tab. A configuration problem shows as an error instead of a score.

## 4. Tavily (web search)

`jobflow interview-prep` uses [Tavily](https://tavily.com) to research the company, and `jobflow tailor-cv` and `jobflow cover-letter` use it, if set, for their reviews and letters. Interview briefings need it; the other two work without it, from the job ad alone.

1. Sign up at [tavily.com](https://tavily.com); there is a free monthly allowance, and each briefing, tailored CV, or cover letter makes two searches.
2. Copy your API key from the Tavily dashboard (Tavily's docs: [Quickstart](https://docs.tavily.com)).
3. In `config/.env`:

   ```
   WEB_SEARCH_API_KEY=tvly-...
   ```

Tavily receives the company name and job title, nothing from your CVs.

## 5. GitHub CLI

The [GitHub CLI](https://cli.github.com), `gh`, sets the `JOBFLOW_SEARCHES` and `JOBFLOW_TIMEZONE` variables that the daily import reads; `jobflow update-searches` uses it after every edit to your searches.

1. Install it: see [cli.github.com](https://cli.github.com) for your system (on Ubuntu and WSL, the instructions there add GitHub's package repository).
2. Log in: `gh auth login`, choose GitHub.com, and follow the browser prompt.
3. Check it from this project's folder: `gh variable list` should run without an error (it may list nothing yet).

## 6. GitHub secrets and the daily import

The daily import runs in GitHub Actions, so it needs its own copy of two keys. In your repository on GitHub, open **Settings → Secrets and variables → Actions**:

- **Secrets:** `DATABASE_URL` and `APIFY_TOKEN`, with the same values as in `config/.env`. Or from this folder: `gh secret set DATABASE_URL` and `gh secret set APIFY_TOKEN`, pasting each value when asked (it isn't echoed or saved in your shell history).
- **Variables:** `JOBFLOW_SEARCHES`, your searches, and `JOBFLOW_TIMEZONE`, the timezone in `config/profile.toml`. Run `uv run jobflow update-searches` to set both. Optionally `APIFY_MAX_WAIT_MINUTES` ([above](#accounts-and-keys)), which you set yourself.

The LLM and Tavily keys stay on your machine; GitHub never needs them.

Then open **Actions → "Daily job ingestion" → Run workflow** to test it once. GitHub also runs it on a schedule, every day at 00:17 Sydney time (01:17 during daylight saving; the cron line in `.github/workflows/daily-ingest.yml`, in UTC), but starts scheduled runs late, usually by several hours (4 to 8 hours each day in October 2026), and by a different amount each day. That lands in the morning, when Seek's 1-day search returns only the jobs listed since midnight, so have the import started on time each night ([section 7](#7-start-the-daily-import-on-time)). The schedule then stays as a fallback for a night that doesn't happen ([when a scheduled run imports](rules.md#when-the-daily-import-runs)). If a run fails, GitHub emails you, and the error is recorded in the database (`scraper_runs`). `uv run jobflow status` shows it all at a glance: whether the import is on, its last run started on time and its last scheduled one, which secrets are set, whether the published searches and timezone match yours, which sites it calls, and, from your database, why recent Apify runs failed (such as your Apify account reaching its monthly usage limit).

## 7. Start the daily import on time

Seek's 1-day search returns only the jobs listed today, so the import should start late in the evening, and on time, which GitHub's own schedule can't promise. A cron service can start it at the time you choose through GitHub's API (runs started this way begin within a minute or two). These steps use [cron-job.org](https://cron-job.org), which is free; any service that can send a web request with headers works.

1. **Create a GitHub token that can only start this repository's workflows.** On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**. Under **Repository access**, choose **Only select repositories** and pick this repository; under **Permissions**, set **Actions** to **Read and write** (GitHub adds **Metadata: Read-only** itself), and nothing else. The token can then start and read this repository's workflow runs, but not read your code or your secrets. Give it an expiry and note the date: once it expires, the import is no longer started on time (the scheduled fallback carries on). GitHub's docs: [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
2. **Create the cron job** at cron-job.org (sign up, then create a cronjob):
   - **URL:** `https://api.github.com/repos/OWNER/REPO/actions/workflows/daily-ingest.yml/dispatches`, with your GitHub user name and this repository's name.
   - **Schedule:** every day at **23:00**, in your own timezone (the service follows daylight saving).
   - **Advanced:** request method **POST**; request body `{"ref": "main"}` (your default branch); and three headers: `Authorization` with the value `Bearer ` followed by the token, `Accept` with `application/vnd.github+json`, and `X-GitHub-Api-Version` with `2022-11-28`.
3. **Test it** with the service's test run: GitHub answers `204 No Content`, and the run shows in the Actions tab (event `workflow_dispatch`) and in `uv run jobflow status` as started on time. A `401` means the token is wrong or has expired; a `403` or `404`, that it lacks the Actions permission or this repository, or that the URL is wrong.

Pick the time that suits you, but leave about an hour before midnight: the import takes some minutes, and a Seek search that starts after midnight sees only the new day's jobs. Seek's day is the listing country's: 23:00 in Sydney is 20:00 in Singapore and Hong Kong, so jobs listed there later that evening are missed. A run started on time is the day's import, and the scheduled run the next morning imports nothing, so you don't pay Apify twice; it imports only when the night's run didn't happen or failed ([rules](rules.md#when-the-daily-import-runs)). Being in the morning, it finds Seek's jobs listed since midnight only, so after a missed night catch up with `uv run jobflow ingest --posted-within-days 7`.

## Backups

Your job history, notes, and scores live only in your database. Check how far back your provider can restore it (Neon's restore window depends on your plan), and for a copy of your own, now and then run `pg_dump "$DATABASE_URL" > jobflow-backup.sql` (it needs PostgreSQL's client tools). That is the backup: restore it with `psql`. **Export CSV (All)** in the tracker is a summary, not a backup: it has only the jobs in the tab you export from (in My jobs, those with the statuses ticked: by default, not rejected, declined, or withdrawn ones), one row each, without their ads, score details, or status history, and can't be restored.

## If a key leaks

Revoke it at the provider straight away and create a new one: Neon (reset the database password), Apify (regenerate the token), your LLM provider, or Tavily. Then update `config/.env`, and for the database or Apify, the GitHub secret too. For the token that starts the import on time, delete it under **Fine-grained tokens** and put a new one in the cron job.
