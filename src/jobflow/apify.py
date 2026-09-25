from __future__ import annotations

import os
import time

import requests

API = "https://api.apify.com/v2"


def run_task(task_id: str) -> tuple[str, str, list[dict]]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is required")
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(f"{API}/actor-tasks/{task_id}/runs", headers=headers,
                             timeout=30)
    response.raise_for_status()
    run = response.json()["data"]
    run_id = run["id"]
    while run["status"] in {"READY", "RUNNING", "TIMING-OUT"}:
        time.sleep(10)
        response = requests.get(f"{API}/actor-runs/{run_id}", headers=headers,
                                timeout=30)
        response.raise_for_status()
        run = response.json()["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run_id} ended with {run['status']}")
    dataset_id = run["defaultDatasetId"]
    response = requests.get(f"{API}/datasets/{dataset_id}/items",
                            headers=headers, params={"format": "json", "clean": "true"},
                            timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected dataset format for run {run_id}")
    return run_id, dataset_id, payload


def ingest_configured_tasks() -> None:
    from .ingest import save_run

    ids = [value.strip() for value in os.environ.get("APIFY_TASK_IDS", "").split(",")
           if value.strip()]
    if not ids:
        raise RuntimeError("Set APIFY_TASK_IDS to comma-separated saved Apify Task IDs")
    for task_id in ids:
        run_id, dataset_id, jobs = run_task(task_id)
        added = save_run(task_id, run_id, dataset_id, jobs)
        print(f"{task_id}: imported {len(jobs)} items, {added} new jobs")
