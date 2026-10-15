from types import SimpleNamespace

import pytest
import requests

from jobflow import llm
from jobflow.batch import run_each


class Reply:
    """An HTTP response from the LLM provider, as requests.post returns it."""

    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.ok, self.reason = status < 400, "Error"

    def json(self):
        return self.body


def answer(content, finish_reason="stop"):
    return Reply(200, {"choices": [{"message": {"content": content},
                                    "finish_reason": finish_reason}]})


@pytest.fixture
def provider(monkeypatch):
    """A fake provider: each request takes the next of provider.replies (raising it if
    it's an exception); the waits between retries go in provider.waits."""
    fake = SimpleNamespace(replies=[], waits=[])

    def post(*args, **kwargs):
        reply = fake.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", fake.waits.append)
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    return fake


def test_reviewer_llm_settings_fall_back_but_never_leak_the_main_key(monkeypatch):
    # Sending your main LLM key to a reviewer's different provider would leak it.
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    assert llm.credentials(reviewer=True) == ("main-key", "main-model")
    monkeypatch.setenv("LLM_REVIEW_MODEL", "review-model")
    assert llm.credentials(reviewer=True) == ("main-key", "review-model")
    assert llm.credentials() == ("main-key", "main-model")
    # Another provider without its own key must not receive the main key.
    monkeypatch.setenv("LLM_REVIEW_BASE_URL", "https://other.example/v1")
    with pytest.raises(RuntimeError, match="LLM_REVIEW_API_KEY"):
        llm.credentials(reviewer=True)
    monkeypatch.setenv("LLM_REVIEW_API_KEY", "other-key")
    assert llm.credentials(reviewer=True) == ("other-key", "review-model")
    # Nor the main model's name, which the other provider would reject mid-batch.
    monkeypatch.delenv("LLM_REVIEW_MODEL")
    with pytest.raises(RuntimeError, match="LLM_REVIEW_MODEL"):
        llm.credentials(reviewer=True)
    monkeypatch.setenv("LLM_REVIEW_MODEL", "review-model")
    assert llm.base_url(reviewer=True) == "https://other.example/v1"
    # Plain http would send the key unencrypted, except to a server on this machine.
    monkeypatch.setenv("LLM_REVIEW_BASE_URL", "http://other.example/v1")
    with pytest.raises(RuntimeError, match="LLM_REVIEW_BASE_URL must be an https"):
        llm.credentials(reviewer=True)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert llm.base_url() == "http://localhost:11434/v1"


def test_llm_retries_rate_limits_but_not_an_empty_balance(provider):
    # Both come back as HTTP 429 from OpenAI: a rate limit passes if you wait, an empty
    # balance never does and must stop a batch rather than fail every job in turn.
    limited = {"error": {"message": "Rate limit reached", "type": "requests",
                         "code": "rate_limit_exceeded"}}
    provider.replies[:] = [Reply(429, limited, {"Retry-After": "3"}), answer("ok")]
    assert llm.complete("s", "u") == ("ok", "model") and provider.waits == [3.0]

    empty = {"error": {"message": "You have no credits remaining.", "type": "insufficient_quota",
                       "code": "credit_balance_exhausted"}}
    provider.replies[:] = [Reply(429, empty)]
    with pytest.raises(llm.LLMError, match="no credits remaining") as caught:
        llm.complete("s", "u")
    assert caught.value.fatal and not provider.replies and provider.waits == [3.0]


def test_llm_drops_settings_the_provider_rejects(monkeypatch):
    # Anthropic's OpenAI-compatible API rejects JSON mode, and newer Claude models reject
    # temperature, each with a 400 that failed every job in a scoring run.
    sent = []

    def post(url, json, **kwargs):
        sent.append(sorted(k for k in json if k in llm.OPTIONAL))
        if "response_format" in json:
            return Reply(400, {"error": {
                "message": "response_format.type: Input should be 'json_schema'"}})
        if "temperature" in json:
            return Reply(400, {"error": {"message": "`temperature` is deprecated for this model."}})
        return answer('```json\n{"a": 1}\n```')

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "model")
    content, _ = llm.complete("s", "u", json_mode=True)
    assert llm.parse_json(content) == {"a": 1}
    assert sent == [["response_format", "temperature"], ["temperature"], []]


def test_llm_retries_a_dropped_connection_and_rejects_an_empty_reply(provider):
    # A network blip failed the job outright; an empty reply crashed later, far from its cause.
    provider.replies[:] = [requests.ConnectionError("reset"), answer("ok")]
    assert llm.complete("s", "u") == ("ok", "model") and provider.waits == [2.0]
    provider.replies[:] = [answer(None)]
    with pytest.raises(llm.LLMError, match="no text"):
        llm.complete("s", "u")
    # A reply cut off at the length limit once passed as a complete letter or briefing.
    provider.replies[:] = [answer("Dear Hiring Manager, I", "length")]
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.complete("s", "u")
    # A reply too slow may still be billed: asked for again once, not four times.
    provider.replies[:] = [requests.ReadTimeout(), requests.ReadTimeout(), answer("ok")]
    with pytest.raises(requests.ReadTimeout):
        llm.complete("s", "u")


def test_a_bad_key_stops_the_batch_instead_of_failing_every_job():
    # A bad key or an empty balance fails every request: tailor-cv, cover-letter,
    # interview-prep, and the tracker's scoring must stop, not try (and bill) each job.
    tried = []

    def work(job_id):
        tried.append(job_id)
        if job_id == 1:
            raise llm.LLMError("Invalid key (LLM provider, HTTP 401)", fatal=True)
        if job_id == 3:
            raise RuntimeError("one job's own problem")
    # One worker, so no other job is already under way when the first one fails.
    assert run_each([1, 2, 3, 4], work, workers=1) == ([1], [2, 3, 4]) and tried == [1]
    # Any other failure doesn't stop the rest.
    tried.clear()
    assert run_each([3, 4, 5], work) == ([3], []) and sorted(tried) == [3, 4, 5]
