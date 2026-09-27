from __future__ import annotations

import json
import os

import requests

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def base_url(reviewer: bool = False) -> str:
    # An empty setting (as in config/.env.example) means "use the default".
    url = (reviewer and os.environ.get("LLM_REVIEW_BASE_URL")) or os.environ.get("LLM_BASE_URL")
    return (url or DEFAULT_BASE_URL).rstrip("/")


def credentials(reviewer: bool = False) -> tuple[str, str]:
    """The API key and model; for the reviewer, each LLM_REVIEW_* setting left empty falls
    back to the main one."""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if reviewer:
        review_key = os.environ.get("LLM_REVIEW_API_KEY")
        # Never send the main provider's key to a different provider.
        if base_url(reviewer=True) != base_url() and not review_key:
            raise RuntimeError("LLM_REVIEW_BASE_URL is a different provider: set its "
                               "LLM_REVIEW_API_KEY in config/.env too")
        key, model = review_key or key, os.environ.get("LLM_REVIEW_MODEL") or model
    if not key or not model:
        raise RuntimeError("Set LLM_API_KEY and LLM_MODEL in config/.env")
    return key, model


def complete(system: str, user: str, *, json_mode: bool = False,
             reviewer: bool = False) -> tuple[str, str]:
    """Call an OpenAI-compatible chat-completions endpoint, as the reviewer if asked."""
    key, model = credentials(reviewer)
    body = {"model": model, "messages": [{"role": "system", "content": system},
             {"role": "user", "content": user}], "temperature": 0.1}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    response = requests.post(f"{base_url(reviewer)}/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {key}"}, timeout=180)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"], model


def parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        left, right = content.find("{"), content.rfind("}")
        if left < 0 or right <= left:
            raise
        return json.loads(content[left:right + 1])
