from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg.types.json


TRACKING_PARAMS = {"trk", "trackingid", "refid", "position", "pagenum"}


def canonical_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.casefold() not in TRACKING_PARAMS and not k.casefold().startswith("utm_")]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def clean_text(job: dict) -> str:
    value = job.get("descriptionText") or job.get("descriptionHtml") or ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def identity(job: dict) -> tuple[str, str | None]:
    url = canonical_url(job.get("link"))
    source_id = str(job.get("id") or "").strip() or None
    if source_id:
        return f"linkedin:{source_id}", url
    if url:
        return f"url:{hashlib.sha256(url.encode()).hexdigest()}", url
    parts = "|".join(str(job.get(k) or "").casefold().strip()
                     for k in ("companyName", "title", "location"))
    desc = clean_text(job).casefold()[:1500]
    return f"fingerprint:{hashlib.sha256((parts + '|' + desc).encode()).hexdigest()}", None


def posted_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def save_run(actor_id: str, run_id: str, dataset_id: str, jobs: list[dict]) -> int:
    from .db import connect

    with connect() as conn:
        run = conn.execute(
            """INSERT INTO scraper_runs(actor_id, apify_run_id, dataset_id, status,
                   started_at, finished_at, items_seen)
               VALUES (%s, %s, %s, 'SUCCEEDED', now(), now(), %s)
               ON CONFLICT (apify_run_id) DO UPDATE SET dataset_id=EXCLUDED.dataset_id,
                   status='SUCCEEDED', finished_at=now(), items_seen=EXCLUDED.items_seen
               RETURNING id""", (actor_id, run_id, dataset_id, len(jobs))).fetchone()
        run_pk = run[0]
        inserted = 0
        for job in jobs:
            key, url = identity(job)
            raw = psycopg.types.json.Jsonb(job)
            row = conn.execute(
                """INSERT INTO jobs(canonical_key, source_job_id, canonical_url, title,
                       company, location, posted_at, description, raw_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (canonical_key) DO UPDATE SET
                       last_seen_at=now(), raw_json=EXCLUDED.raw_json,
                       description=coalesce(EXCLUDED.description, jobs.description),
                       canonical_url=coalesce(EXCLUDED.canonical_url, jobs.canonical_url)
                   RETURNING id, (xmax = 0)""",
                (key, str(job.get("id") or "") or None, url,
                 job.get("title") or "Untitled", job.get("companyName"),
                 job.get("location"), posted_date(job.get("postedAt")),
                 clean_text(job), raw)).fetchone()
            job_pk, was_inserted = row
            inserted += int(was_inserted)
            conn.execute(
                """INSERT INTO job_observations(job_id, scraper_run_id, observed_json)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                (job_pk, run_pk, raw))
        return inserted
