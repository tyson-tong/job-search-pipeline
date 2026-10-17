"""Chat completions from any OpenAI-compatible provider: the main and reviewer settings,
retries with backoff, settings a model rejects dropped and the request sent again, and
LLMError(fatal=True) for problems every later request would hit too."""
from __future__ import annotations

import json
import os
from time import sleep
from urllib.parse import urlsplit

import requests

from .settings import LOCAL_HOSTS

DEFAULT_BASE_URL = "https://api.openai.com/v1"
# A rate limit, a server error, or a network error is retried this many times, waiting as
# long as the provider asks (Retry-After) up to MAX_WAIT seconds, or 2, 4, 8... seconds otherwise.
RETRIES = 4
MAX_WAIT = 60
# Seconds to wait for a reply. A reply this slow may still be billed, so one that times out
# is asked for again only once (a slow model would be slow again).
READ_TIMEOUT = 300
# Settings sent when the provider accepts them, and dropped when it rejects them.
OPTIONAL = ("response_format", "temperature")
# Low, for answers that should come out the same each time: scores, CV edits, rankings.
TEMPERATURE = 0.1


class LLMError(RuntimeError):
    """The provider refused a request. `fatal` means every later request will fail too
    (no credit left, a bad key, an unknown model), so a batch should stop."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def base_url(reviewer: bool = False) -> str:
    # An empty setting (as in config/.env.example) means "use the default".
    name = ("LLM_REVIEW_BASE_URL" if reviewer and os.environ.get("LLM_REVIEW_BASE_URL")
            else "LLM_BASE_URL")
    url = (os.environ.get(name) or DEFAULT_BASE_URL).rstrip("/")
    # The API key goes with every request, so only a server on this machine may use plain http.
    parts = urlsplit(url)
    if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)):
        raise RuntimeError(f"{name} must be an https:// address (http:// only for a server on "
                           f"this machine, such as http://localhost), not {url}")
    return url


def credentials(reviewer: bool = False) -> tuple[str, str]:
    """The API key and model; for the reviewer, each LLM_REVIEW_* setting left empty falls
    back to the main one."""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if reviewer:
        review_key, review_model = (os.environ.get("LLM_REVIEW_API_KEY"),
                                    os.environ.get("LLM_REVIEW_MODEL"))
        # Never send the main provider's key to a different provider, nor its model name,
        # which the other provider would reject only once a batch is under way.
        if base_url(reviewer=True) != base_url() and not (review_key and review_model):
            raise RuntimeError("LLM_REVIEW_BASE_URL is a different provider: set its "
                               "LLM_REVIEW_API_KEY and LLM_REVIEW_MODEL in config/.env too")
        key, model = review_key or key, review_model or model
    if not key or not model:
        raise RuntimeError("Set LLM_API_KEY and LLM_MODEL in config/.env")
    return key, model


def complete(system: str, user: str, *, json_mode: bool = False, reviewer: bool = False,
             temperature: float = TEMPERATURE) -> tuple[str, str]:
    """Call an OpenAI-compatible chat-completions endpoint, as the reviewer if asked."""
    key, model = credentials(reviewer)
    body = {"model": model, "messages": [{"role": "system", "content": system},
             {"role": "user", "content": user}], "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    attempt, timeouts = 0, 0
    while True:
        try:
            response = requests.post(f"{base_url(reviewer)}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {key}"},
                                     timeout=(30, READ_TIMEOUT))
        except (requests.ConnectionError, requests.Timeout) as exc:
            # A dropped connection or a stalled provider may pass: retried like a rate limit.
            timeouts += isinstance(exc, requests.ReadTimeout)
            if attempt == RETRIES or timeouts > 1:
                raise
            sleep(2.0 ** (attempt + 1))
            attempt += 1
            continue
        if response.ok:
            return _content(response), model
        error = _error(response)
        # Some providers or models (Anthropic's OpenAI-compatible API) reject JSON mode or
        # temperature. Neither is needed: every prompt asks for JSON only, and parse_json
        # finds it. So drop the one the error names and ask again.
        rejected = [name for name in OPTIONAL if name in body and response.status_code == 400
                    and name in f"{error.get('message')} {error.get('param')}"]
        if rejected:
            del body[rejected[0]]
            continue
        # OpenAI reports an exhausted balance as a 429 too, but waiting won't fix that.
        quota = error.get("type") == "insufficient_quota" or error.get("code") in (
            "insufficient_quota", "credit_balance_exhausted")
        retry = (response.status_code == 429 and not quota) or response.status_code >= 500
        if not retry or attempt == RETRIES:
            break
        sleep(_wait(response, attempt))
        attempt += 1
    message = error.get("message") or response.reason or "request failed"
    # 402 is how some providers (OpenRouter, say) report credit run out.
    raise LLMError(f"{message} (LLM provider, HTTP {response.status_code})",
                   fatal=quota or response.status_code in (401, 402, 403, 404))


def _content(response: requests.Response) -> str:
    """The reply's text; LLMError if there is none (a refusal), or it was cut off."""
    try:
        choice = response.json()["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("The LLM provider's reply wasn't a chat completion") from None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("The LLM provider returned no text (it may have refused the request)")
    # A cut-off cover letter or briefing would otherwise pass as complete.
    if choice.get("finish_reason") == "length":
        raise LLMError("The LLM provider's reply was cut off at its length limit")
    return content


def _error(response: requests.Response) -> dict:
    """The "error" object of an OpenAI-style error response, or {}."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return {}
    return error if isinstance(error, dict) else {"message": str(error)} if error else {}


def _wait(response: requests.Response, attempt: int) -> float:
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        wait = 2.0 ** (attempt + 1)
    return min(max(wait, 1.0), MAX_WAIT)


def parse_json(content: str) -> dict:
    """The JSON object in a reply, also when the model wrapped it in prose or a code fence.
    LLMError if there is none: every prompt asks for an object."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        left, right = content.find("{"), content.rfind("}")
        try:
            parsed = json.loads(content[left:right + 1]) if 0 <= left < right else None
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, dict):
        raise LLMError("The LLM's reply wasn't the JSON object asked for")
    return parsed
