"""Company research on the web (Tavily), for interview briefings, and for tailored CVs'
reviews and cover letters when a key is set."""
from __future__ import annotations

import concurrent.futures
import os
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from ..db import fetch_job

# A search for "the company" finds other companies, whose facts and figures would then
# count as research: the cover letter's check allows numbers from it.
NO_EMPLOYER = "No company research: the ad names no employer, so only the job ad is used."


def web_search(company: str, title: str) -> list[dict]:
    key = os.environ.get("WEB_SEARCH_API_KEY")
    if not key:
        raise RuntimeError("Set WEB_SEARCH_API_KEY for current company research (Tavily)")

    def search(query: str) -> list[dict]:
        response = requests.post("https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "topic": "news" if "latest" in query else "general",
                  "search_depth": "basic", "max_results": 5,
                  "include_answer": False}, timeout=45)
        response.raise_for_status()
        return response.json().get("results", [])

    # The two searches are independent, so they run side by side.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [row for rows in pool.map(search, (
            f'"{company}" official company products about', f'"{company}" latest news {title}'))
            for row in rows]
    seen = set()
    unique = []
    for row in results:
        url = row.get("url")
        if url and url not in seen:
            seen.add(url)
            unique.append({"title": row.get("title") or "", "url": url,
                           "content": (row.get("content") or "")[:900]})
    return unique[:10]


def research(job: dict) -> tuple[list[dict], list[str]]:
    """Company web research for a reviewer or cover letter; optional, so a failed search
    (Tavily down, or out of credits) is a note, not a failed job."""
    if not os.environ.get("WEB_SEARCH_API_KEY"):
        return [], ["No company research: WEB_SEARCH_API_KEY is not set, so only the job ad "
                    "is used."]
    if not job["company"]:
        return [], [NO_EMPLOYER]
    try:
        return web_search(job["company"], job["title"]), []
    except Exception as exc:
        return [], [f"No company research: the web search failed ({exc}), so only the job ad "
                    "is used."]


def research_soon(job_id: int) -> Future:
    """Start research() for the job in the background: the web search then overlaps
    choosing (maybe scoring) the CV and drafting. .result() gives what research() returns."""
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(lambda: research(fetch_job(job_id)))
    pool.shutdown(wait=False)
    return future
