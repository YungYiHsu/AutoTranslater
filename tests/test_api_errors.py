from __future__ import annotations

from types import SimpleNamespace

from core.exceptions import ApiRequestError, GeminiFreeTierQuotaError, InvalidLlmResponseError
from translators.api_errors import request_error, response_text


class StatusFailure(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("provider details must not be displayed")
        self.status_code = code


class NetworkFailure(ConnectionError):
    pass


class ProviderTimeout(Exception):
    pass


def test_numeric_status_is_the_only_reported_detail() -> None:
    error = request_error(StatusFailure(503))
    assert isinstance(error, ApiRequestError)
    assert str(error) == "錯誤碼：503"


def test_429_is_reported_as_quota_exhaustion() -> None:
    error = request_error(StatusFailure(429))
    assert isinstance(error, GeminiFreeTierQuotaError)
    assert str(error) == "額度不足（429）"


def test_statusless_failures_have_stable_categories() -> None:
    assert str(request_error(NetworkFailure())) == "網路連線失敗"
    assert str(request_error(ProviderTimeout())) == "請求逾時"
    assert str(request_error(RuntimeError("secret detail"))) == "API 請求失敗，原因未知"


def test_blocked_response_preserves_finish_metadata_without_prompt() -> None:
    candidate = SimpleNamespace(finish_reason="SAFETY", safety_ratings=["blocked"])
    response = SimpleNamespace(text=None, prompt_feedback="blocked", candidates=[candidate])
    try:
        response_text(response)
    except InvalidLlmResponseError as error:
        assert "SAFETY" in error.llm_output
        assert "blocked" in error.llm_output
        assert "prompt" not in error.llm_output.lower().replace("prompt_feedback", "")
    else:
        raise AssertionError("invalid response should raise")
