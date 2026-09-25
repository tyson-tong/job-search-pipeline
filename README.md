# Job Search Workflow

A private, cloud-scheduled job discovery and application workspace. PostgreSQL stores job records and provenance. CV source YAML lives in `cv/source`; per-job versions are generated under `cv/tailored` and are ignored by Git. Interview-preparation HTML is generated under `interviews` and is also ignored by Git.

## Design

1. GitHub Actions runs saved Apify Tasks weekly. Saved Tasks preserve each scraper's configured input. It polls each run, reads the default dataset JSON, and upserts jobs into PostgreSQL.
2. A canonical key prefers the LinkedIn job ID, then a normalized URL, then a title/company/location/description fingerprint. Raw JSON and every scraper-run observation are retained for traceability.
3. Matching will rank jobs against each CV variant with a requirement-by-requirement evidence report. A missing statement is reported as “not evidenced in CV,” not as proof that the candidate lacks the skill.
4. Tailoring will start from one of the source RenderCV YAML files and write a separate draft per job. It must not invent skills, experience, metrics, or credentials. Review the YAML and rendered document before using it.
5. `jobflow interview-prep JOB_ID --email-file interview-email.txt` is the intended manual interface. It will create an HTML briefing from the selected job, email details, relevant CV evidence, and researched company information. Email text should be passed only at invocation and never persisted to PostgreSQL or Git.

## CV source files

The initial source set is copied from `your private CV folder`:

- `CV_dev.yaml`
- `CV_strat.yaml`
- `CV_harvard_cse.yaml`

They remain unchanged in `cv/source`. This repository must be **private** before these files are pushed. Do not add RenderCV PDFs or DOCX files unless you specifically want those versions tracked too.

## Cloud setup

Suggested low-maintenance setup: a managed PostgreSQL service (Neon or Supabase), GitHub Actions for the weekly run, and private object storage for raw datasets and generated files if retention grows. Start with PostgreSQL JSONB for raw job payloads; object storage can be added without changing job matching. A private bucket is required if CVs or generated application files are uploaded there.

Configure repository secrets/variables:

- Secret `DATABASE_URL`: TLS-enabled Postgres connection string.
- Secret `APIFY_TOKEN`: Apify API token.
- Variable `APIFY_TASK_IDS`: comma-separated saved task IDs.
- Later: LLM and web-search provider keys as secrets. Never put these in YAML, source code, or GitHub variables if they are credentials.

The workflow cron uses UTC because GitHub Actions cron does not follow local daylight-saving time. Dispatch it manually from the Actions tab to run immediately.

## Local use

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[documents]'
cp .env.example .env   # fill locally; .env is ignored
set -a && . ./.env && set +a
jobflow migrate
jobflow ingest
jobflow import-json sample-jobs.json
jobflow list --limit 50
jobflow match 123
jobflow tailor 123 --cv strat
jobflow interview-prep 123 --email-file ./interview-email.txt
```

`DATABASE_URL` and `APIFY_TOKEN` are required for cloud ingestion. `import-json` can load a saved Apify JSON array for a local or one-off import.

## Current implementation status

- Implemented: PostgreSQL schema, Apify saved-task run and polling, dataset retrieval, JSON import, exact-key deduplication, run provenance, and weekly GitHub Actions workflow.
- Implemented: LLM-assisted requirement/evidence scoring for each CV variant; tailored RenderCV YAML drafts with factual constraints and review marker; optional PDF rendering when RenderCV is installed; manual interview-prep command with three specialist agents, a synthesizer, current web search, and a standalone HTML file.
- Follow-up improvements: fuzzy duplicate review for jobs with different source IDs, persistent application-status CLI commands, export/history UI, and optional pgvector retrieval.

Matching and document generation do not run automatically on ingestion. They call paid LLM/search services and benefit from a human review step.
