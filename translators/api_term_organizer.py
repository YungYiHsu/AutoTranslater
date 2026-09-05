"""Gemini suggestions for manually organizing redundant proper-noun mappings."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import AppConfig
from core.exceptions import TermMemoryError
from core.paths import get_resource_path
from core.term_organizer import TermOrganizationProposal
from translators.gemini_errors import raise_if_free_tier_quota
from translators.organizer_base import BaseTermOrganizer

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_ERROR_NAMES = (
    "connection",
    "internalserver",
    "ratelimit",
    "resourceexhausted",
    "serviceunavailable",
    "timeout",
)
_CODE_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n?(.*?)\n?```\s*\Z", re.DOTALL)


class _RetryableOrganizationError(Exception):
    pass


class ApiTermOrganizer(BaseTermOrganizer):
    """Ask Gemini to approve only conservative canonicalization proposals."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        retry_attempts: int = 3,
        prompt_path: Path | None = None,
        client: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_batch_terms: int = 500,
        max_batch_chars: int = 40_000,
        overlap_terms: int = 5,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if retry_attempts < 0:
            raise ValueError("retry_attempts must be non-negative")
        if max_batch_terms <= 0:
            raise ValueError("max_batch_terms must be positive")
        if max_batch_chars <= 0:
            raise ValueError("max_batch_chars must be positive")
        if overlap_terms < 0 or overlap_terms >= max_batch_terms:
            raise ValueError("overlap_terms must be between zero and max_batch_terms")
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._retry_attempts = retry_attempts
        path = prompt_path or get_resource_path("resources/prompts/term_organization.txt")
        try:
            self._prompt = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as exc:
            raise TermMemoryError("無法讀取專有名詞整理 Prompt。") from exc
        if not self._prompt:
            raise TermMemoryError("專有名詞整理 Prompt 不可為空白。")
        self._client = client or self._create_client()
        self._sleeper = sleeper
        self._max_batch_terms = max_batch_terms
        self._max_batch_chars = max_batch_chars
        self._overlap_terms = overlap_terms

    @classmethod
    def from_config(cls, config: AppConfig, api_key: str, **kwargs: Any) -> ApiTermOrganizer:
        return cls(
            model=config.model,
            api_key=api_key,
            retry_attempts=config.retry_attempts,
            **kwargs,
        )

    def batches(self, terms: Mapping[str, str]) -> tuple[dict[str, str], ...]:
        return self._build_batches(terms)

    def organize(
        self,
        terms: Mapping[str, str],
    ) -> tuple[TermOrganizationProposal, ...]:
        if len(terms) < 2:
            return ()
        content = json.dumps(
            {"terms": dict(terms)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._request(content)

    def _build_batches(self, terms: Mapping[str, str]) -> tuple[dict[str, str], ...]:
        if len(terms) < 2:
            return ()
        ordered = sorted(
            terms.items(),
            key=lambda item: (
                unicodedata.normalize("NFKC", item[0]).casefold(),
                item[0],
            ),
        )
        batches: list[dict[str, str]] = []
        start = 0
        while start < len(ordered):
            current: list[tuple[str, str]] = []
            end = start
            while end < len(ordered) and len(current) < self._max_batch_terms:
                candidate = [*current, ordered[end]]
                payload = json.dumps(
                    {"terms": dict(candidate)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if current and len(payload) > self._max_batch_chars:
                    break
                current = candidate
                end += 1
            batches.append(dict(current))
            if end >= len(ordered):
                break
            overlap = min(self._overlap_terms, max(0, len(current) - 1))
            start = end - overlap
        return tuple(batches)

    def _request(self, content: str) -> tuple[TermOrganizationProposal, ...]:
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableOrganizationError),
            reraise=True,
            sleep=self._sleeper,
        )
        try:
            raw = retrying(self._request_once, content)
        except _RetryableOrganizationError as exc:
            raise TermMemoryError("Gemini 專有名詞整理重試後仍無法完成。") from exc
        return self._parse_response(raw)

    def _request_once(self, content: str) -> Any:
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=content,
                config={
                    "system_instruction": self._prompt,
                    "temperature": 0.1,
                    "response_mime_type": "application/json",
                },
            )
            return response.text
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if self._is_retryable(exc):
                raise _RetryableOrganizationError(type(exc).__name__) from exc
            raise TermMemoryError(
                f"Gemini 拒絕專有名詞整理請求（{type(exc).__name__}）。"
            ) from exc

    @staticmethod
    def _parse_response(value: Any) -> tuple[TermOrganizationProposal, ...]:
        if not isinstance(value, str) or not value.strip():
            raise TermMemoryError("Gemini 回傳了空白的專有名詞整理結果。")
        fenced = _CODE_FENCE.fullmatch(value)
        if fenced:
            value = fenced.group(1)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TermMemoryError("Gemini 回傳的專有名詞整理結果不是有效 JSON。") from exc
        if not isinstance(payload, dict) or set(payload) != {"groups"}:
            raise TermMemoryError("Gemini 專有名詞整理結果欄位不正確。")
        groups = payload["groups"]
        if not isinstance(groups, list):
            raise TermMemoryError("Gemini 專有名詞整理 groups 必須是陣列。")
        result: list[TermOrganizationProposal] = []
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"source", "translation", "remove"}:
                raise TermMemoryError("Gemini 專有名詞整理群組格式不正確。")
            source = group["source"]
            translation = group["translation"]
            remove = group["remove"]
            if (
                not isinstance(source, str)
                or not isinstance(translation, str)
                or not source.strip()
                or not translation.strip()
                or not isinstance(remove, list)
                or not all(isinstance(item, str) and item.strip() for item in remove)
            ):
                raise TermMemoryError("Gemini 專有名詞整理群組含有無效內容。")
            result.append(
                TermOrganizationProposal(
                    source.strip(),
                    translation.strip(),
                    tuple(item.strip() for item in remove),
                )
            )
        return tuple(result)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "code", None)
        if isinstance(status, int):
            return status in _RETRYABLE_STATUS_CODES or status >= 500
        name = type(exc).__name__.replace("_", "").lower()
        return any(marker in name for marker in _RETRYABLE_ERROR_NAMES)

    def _create_client(self) -> Any:
        try:
            from google import genai

            return genai.Client(api_key=self._api_key)
        except Exception as exc:
            raise TermMemoryError("無法初始化 Gemini 專有名詞整理器。") from exc


__all__ = ["ApiTermOrganizer"]
