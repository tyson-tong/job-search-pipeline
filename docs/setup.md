# Setup

Everything needed once, before the daily routine in the [README](../README.md). You need [uv](https://docs.astral.sh/uv/) and a GitHub account; [RenderCV](https://docs.rendercv.com) is optional (it renders tailored CVs as PDFs).

1. **Make your own copy.** Create a *private* repository from this template (GitHub: "Use this template"), then clone it.
2. **Install and create your settings files**, from the repository folder:

   ```
   uv sync --extra documents
   uv run jobflow init
   ```

   `init` creates `config/.env`, `config/profile.toml`, and `config/searches.toml` from the `.example` templates. It never overwrites an existing file, so it's safe to rerun. These files are ignored by Git, and `config/.env` is readable only by you.
3. **Get your accounts and keys** (sections 1 to 5 below) and put them in `config/.env`.
4. **Point it at your CVs.** In `config/profile.toml`, set `cv_directory` to the folder with your RenderCV files, kept outside this repository. Files must be named `CV_<variant>.yaml`; the variant name is what `--cv` takes. The profile also sets the output folder, the scoring weights, and your timezone.
5. **Define your searches** in `config/searches.toml`: see [searches.md](searches.md).
6. **Schedule the daily import**: see [section 6](#6-github-secrets-and-the-daily-import).

## Accounts and keys

Sign-up screens and prices change, so the steps below describe what to look for rather than exact buttons, and link each provider's own documentation. Check current pricing on each provider's site before you start.

| Service | Needed for | Goes in |
|---|---|---|
| [PostgreSQL on Neon](#1-database-neon) | everything | `DATABASE_URL` in `config/.env`, and a GitHub secret |
| [Apify](#2-apify-job-scrapers) | the daily import | `APIFY_TOKEN` in `config/.env`, and a GitHub secret |
| [An LLM provider](#3-llm-provider) | scoring, tailor-cv, interview-prep | `LLM_API_KEY`, `LLM_MODEL` (and `LLM_BASE_URL`) in `config/.env` |
| [Tavily](#4-tavily-web-search) | interview-prep; optional for tailor-cv and cover-letter | `WEB_SEARCH_API_KEY` in `config/.env` |
| [GitHub CLI](#5-github-cli) | `jobflow update-searches`, setting GitHub secrets | logged in on your machine |

Keep every key out of Git: `config/.env` is ignored by Git and readable only by you. Never paste a key into `config/searches.toml`, `config/profile.toml`, or an issue.

Optional: `APIFY_MAX_WAIT_MINUTES` in `config/.env` sets how long to wait for each Apify run (default 60). `jobflow` reads `config/.env` by itself; variables already set in your shell take precedence. The database tables are created the first time you run any command other than `init`.

## 1. Database (Neon)

Any PostgreSQL database reachable from the internet works (Supabase, Amazon RDS, and others). These steps use [Neon](https://neon.tech), which has a free tier.

1. Sign up at [neon.tech](https://neon.tech) and create a project. Choose the region nearest to you; the tracker talks to the database on every page load.
2. Open the project's **Connect** (connection details) panel and copy the connection string. Use the **direct** connection, not the pooled one (the pooled host name contains `-pooler`): the direct one is the one this project is tested with.
3. Make sure the string ends with `sslmode=require` (Neon's usually does), so the connection is encrypted.
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

Scoring, tailored CVs, and interview briefings use any OpenAI-compatible chat-completions API.

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

**Another provider** (for example a hosted open model, or a gateway): set `LLM_BASE_URL` to its OpenAI-compatible endpoint (usually ending in `/v1`), and `LLM_API_KEY` and `LLM_MODEL` to its key and model name. Use an `https://` address, so your key isn't sent unencrypted.

**Optional: a different model as reviewer.** `tailor-cv` and `cover-letter` have a second model call review each draft as the hiring manager would. By default that's `LLM_MODEL`; a different model makes a better critic, since it doesn't share the drafting model's blind spots. In `config/.env`:

```
LLM_REVIEW_MODEL=<model name>     # same provider, another model
LLM_REVIEW_BASE_URL=              # or another provider's endpoint...
LLM_REVIEW_API_KEY=               # ...with its own key (required when the endpoint differs)
```

Leave any of them empty to use the main setting. `CV_REVIEW.md` and `COVER_LETTER_REVIEW.md` name the model that reviewed.

What each command sends: scoring, tailoring, and interview prep send job text and your CV text. See the README's [Privacy](../README.md#privacy) section, and choose a provider whose data handling you accept.

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

The [GitHub CLI](https://cli.github.com), `gh`, sets the `JOBFLOW_SEARCHES` variable that the daily import reads; `jobflow update-searches` uses it after every edit to your searches.

1. Install it: see [cli.github.com](https://cli.github.com) for your system (on Ubuntu and WSL, the instructions there add GitHub's package repository).
2. Log in: `gh auth login`, choose GitHub.com, and follow the browser prompt.
3. Check it from this project's folder: `gh variable list` should run without an error (it may list nothing yet).

## 6. GitHub secrets and the daily import

The daily import runs in GitHub Actions, so it needs its own copy of two keys. In your repository on GitHub, open **Settings → Secrets and variables → Actions**:

- **Secrets:** `DATABASE_URL` and `APIFY_TOKEN`, with the same values as in `config/.env`. Or from this folder: `gh secret set DATABASE_URL` and `gh secret set APIFY_TOKEN`, pasting each value when asked (it isn't echoed or saved in your shell history).
- **Variable:** `JOBFLOW_SEARCHES`, your searches. Run `uv run jobflow update-searches` to set it.

The LLM and Tavily keys stay on your machine; GitHub never needs them.

Then open **Actions → "Daily job ingestion" → Run workflow** to test it once. After that it runs every day at 00:30 Sydney time (01:30 during daylight saving). To change the time, edit the cron line in `.github/workflows/daily-ingest.yml`; cron times are in UTC. If a run fails, GitHub emails you, and the error is recorded in the database (`scraper_runs`).

## If a key leaks

Revoke it at the provider straight away and create a new one: Neon (reset the database password), Apify (regenerate the token), your LLM provider, or Tavily. Then update `config/.env`, and for the database or Apify, the GitHub secret too.
