# Job Search Workflow Template

A single-person job discovery and application workflow. Each person deploys their own copy with their own PostgreSQL database, Apify account/tasks, LLM/search credentials, and private CV directory. This template contains no personal CVs or API credentials.

## Data separation

- Template repository: workflow code, migrations, prompts, and example configuration only.
- Private local profile: profile.toml (ignored by Git) points to the user's CV folder and local output directories.
- Private CVs: kept outside this repository. cv_directory must contain CV_*.yaml RenderCV files.
- Secrets: API keys and DATABASE_URL belong in local .env or GitHub Actions secrets, never profile TOML or Git.
- Job and run data: stored in that user's PostgreSQL database.
- Generated documents: written to configured local output directories, ignored by Git.
- Interview email: supplied to the manual CLI command, used for that run, and not written to the database or Git. Relevant email content is sent to the configured LLM provider.

Copy profile.example.toml to profile.toml and update the paths for your own installation. The real profile.toml is excluded from Git.

## Personal deployment setup

1. Create a private repository from this template for each person; do not share a deployment repo containing profiles, job records, secrets, or outputs.
2. Create a managed PostgreSQL database for that person.
3. Copy .env.example to .env locally and fill in their database, Apify, LLM, and web-search credentials.
4. Set the repository's GitHub Actions secret DATABASE_URL, secret APIFY_TOKEN, and variable APIFY_TASK_IDS to that person's own values.
5. Configure saved Apify Task IDs in the repository variable. These IDs are configuration, not credentials, but reveal that user's search setup.
6. Copy profile.example.toml to profile.toml and set a private CV directory containing their RenderCV YAML files.
7. Run CV matching, tailored drafts, and interview preparation locally. Weekly ingestion runs in GitHub Actions.

## Cloud vs local responsibilities

The scheduled GitHub Actions workflow only runs the saved Apify Tasks and imports job JSON into PostgreSQL. CV matching, tailoring, and interview preparation run from the user's local clone so CV files need not be uploaded to GitHub or object storage. Hosted LLM and web-search providers receive relevant CV, job, or email text when those commands are invoked; choose providers whose data handling is acceptable.

If moving CV operations into cloud compute later, put CVs in a private per-user object-storage bucket and grant access only to that user's workflow. Do not store CV contents in PostgreSQL or GitHub Actions variables.

## Install and configure

    python -m venv .venv
    . .venv/bin/activate
    pip install -e '.[documents]'
    cp .env.example .env
    cp profile.example.toml profile.toml
    # Edit profile.toml and .env locally.
    set -a && . ./.env && set +a
    jobflow migrate
    jobflow ingest

The .env file and profile.toml are ignored. GitHub Actions uses task IDs and secrets configured in each person's deployment repository.

## Commands

    jobflow import-json sample-jobs.json
    jobflow list --limit 50
    jobflow match 123
    jobflow tailor 123 --cv strat
    jobflow interview-prep 123 --email-file ./interview-email.txt

The interview email can also be passed on standard input with jobflow interview-prep 123 --email-file -.

CV matching compares requirements against explicit evidence in each CV and reports missing CV evidence separately from a claim that the candidate lacks a skill. Tailoring creates a fact-preserving RenderCV YAML draft and review marker; inspect every claim before use. RenderCV generates output when installed.

## Apify schedule

The weekly workflow runs saved Apify Tasks, which preserve each scraper's configured input. It polls each run, reads its dataset JSON, and upserts jobs into PostgreSQL. GitHub Actions cron uses UTC; edit .github/workflows/weekly-ingest.yml to change the day/time.

Set these in each person's private deployment repository:

- Secret DATABASE_URL: TLS-enabled Postgres connection string.
- Secret APIFY_TOKEN: Apify API token.
- Variable APIFY_TASK_IDS: comma-separated saved Task IDs.
- LLM_API_KEY and WEB_SEARCH_API_KEY are used by local CLI commands and can stay only in local .env.

The code supports an OpenAI-compatible chat-completions endpoint configured by LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL.
