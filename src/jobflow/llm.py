from __future__ import annotations

import json
import os

import requests


def complete(system: str, user: str, *, json_mode: bool = False) -> tuple[str, str]:
    """Call an OpenAI-compatible chat-completions endpoint."""
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not key or not model:
        raise RuntimeError("Set LLM_API_KEY and LLM_MODEL")
    body = {"model": model, "messages": [{"role": "system", "content": system},
             {"role": "user", "content": user}], "temperature": 0.1}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    response = requests.post(f"{base}/chat/completions", json=body,
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
