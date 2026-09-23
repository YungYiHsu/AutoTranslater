"""Safe, user-facing classification of provider failures and invalid responses."""

from __future__ import annotations

from typing import Any

from core.exceptions import ApiRequestError, GeminiFreeTierQuotaError, InvalidLlmResponseError


def request_error(exc: Exception) -> ApiRequestError | GeminiFreeTierQuotaError:
    """Classify an exception chain without exposing SDK internals or request data."""
    chain = _exception_chain(exc)
    for item in chain:
        code = _status_code(item)
        if code == 429:
            return GeminiFreeTierQuotaError()
        if code is not None:
            return ApiRequestError(f"錯誤碼：{code}")
    if any(_is_timeout(item) for item in chain):
        return ApiRequestError("請求逾時")
    if any(_is_connection(item) for item in chain):
        return ApiRequestError("網路連線失敗")
    return ApiRequestError("API 請求失敗，原因未知")


def response_text(response: Any) -> str:
    """Read response text while preserving non-text finish metadata for diagnosis."""
    try:
        value = response.text
    except Exception as exc:
        raise InvalidLlmResponseError(
            "LLM 回傳內容無效", _response_summary(response)
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise InvalidLlmResponseError("LLM 回傳內容無效", _response_summary(response))
    return value


def invalid_response(reason: str, output: Any) -> InvalidLlmResponseError:
    rendered = output if isinstance(output, str) else repr(output)
    return InvalidLlmResponseError(reason, rendered)


def _exception_chain(exc: Exception) -> tuple[Exception, ...]:
    result: list[Exception] = []
    current: BaseException | None = exc
    while isinstance(current, Exception) and current not in result:
        result.append(current)
        current = current.__cause__ or current.__context__
    return tuple(result)


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
    return None


def _normalized_name(exc: Exception) -> str:
    return type(exc).__name__.replace("_", "").lower()


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in _normalized_name(exc)


def _is_connection(exc: Exception) -> bool:
    name = _normalized_name(exc)
    return isinstance(exc, ConnectionError) or any(
        marker in name for marker in ("connection", "dns", "network")
    )


def _response_summary(response: Any) -> str:
    lines: list[str] = []
    prompt_feedback = getattr(response, "prompt_feedback", None)
    if prompt_feedback is not None:
        lines.append(f"prompt_feedback: {prompt_feedback}")
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        lines.append("candidates: []")
    for index, candidate in enumerate(candidates):
        lines.append(f"candidate {index + 1} finish_reason: {getattr(candidate, 'finish_reason', None)}")
        ratings = getattr(candidate, "safety_ratings", None)
        if ratings:
            lines.append(f"candidate {index + 1} safety_ratings: {ratings}")
    return "\n".join(lines) or "(空回傳：沒有可顯示的 LLM 輸出)"


__all__ = ["invalid_response", "request_error", "response_text"]
