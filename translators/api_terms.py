"""Gemini analysis of proper-noun mappings after a chapter is translated."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.api_usage import DailyApiUsage
from core.config import AppConfig
from core.exceptions import TermMemoryError
from core.gemini_settings import request_config
from core.models import TranslatedChapter
from core.paths import get_resource_path
from core.prompt_composer import format_term_pairs
from translators.api_errors import invalid_response, request_error, response_text
from translators.gemini_errors import raise_if_free_tier_quota
from translators.term_base import BaseTermAnalyzer

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


class _RetryableTermAnalysisError(Exception):
    pass


class ApiTermAnalyzer(BaseTermAnalyzer):
    """Analyze aligned Japanese and translated text with compact JSON output."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        retry_attempts: int = 0,
        prompt_path: Path | None = None,
        client: Any | None = None,
        usage: DailyApiUsage | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_batch_chars: int = 60_000,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if retry_attempts < 0:
            raise ValueError("retry_attempts must be non-negative")
        if max_batch_chars <= 0:
            raise ValueError("max_batch_chars must be positive")
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._retry_attempts = retry_attempts
        path = prompt_path or get_resource_path("resources/prompts/term_analysis.txt")
        try:
            self._prompt = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as exc:
            raise TermMemoryError("無法讀取專有名詞分析 Prompt。") from exc
        if not self._prompt:
            raise TermMemoryError("專有名詞分析 Prompt 不可為空白。")
        self._client = client or self._create_client()
        self._usage = usage
        self._sleeper = sleeper
        self._max_batch_chars = max_batch_chars

    @classmethod
    def from_config(cls, config: AppConfig, api_key: str, **kwargs: Any) -> ApiTermAnalyzer:
        return cls(
            model=config.model,
            api_key=api_key,
            retry_attempts=config.retry_attempts,
            **kwargs,
        )

    @property
    def checkpoint_identity(self) -> str:
        """Bind completion markers to the exact automatic-analysis Prompt."""
        return hashlib.sha256(self._prompt.encode("utf-8")).hexdigest()

    def analyze(
        self,
        chapter: TranslatedChapter,
        used_terms: Mapping[str, str],
    ) -> dict[str, str]:
        if not isinstance(chapter, TranslatedChapter):
            raise TypeError("chapter must be a TranslatedChapter")
        batches = self._build_batches(chapter, used_terms)
        merged: dict[str, str] = {}
        ambiguous: set[str] = set()
        for batch in batches:
            payload = self._request(batch)
            for source, translation in payload.items():
                existing = merged.get(source)
                if existing is None:
                    merged[source] = translation
                elif existing != translation:
                    ambiguous.add(source)
        for source in ambiguous:
            merged.pop(source, None)
        return merged

    def _request(self, content: str) -> dict[str, str]:
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableTermAnalysisError),
            reraise=True,
            sleep=self._sleeper,
        )
        try:
            raw = retrying(self._request_once, content)
        except _RetryableTermAnalysisError as exc:
            raise request_error(exc) from exc
        return self._parse_response(raw)

    def _request_once(self, content: str) -> Any:
        try:
            if self._usage is not None:
                self._usage.record(self._model)
            response = self._client.models.generate_content(
                model=self._model,
                contents=content,
                config=request_config({
                    "system_instruction": self._prompt,
                    "temperature": 0.1,
                    "response_mime_type": "application/json",
                }),
            )
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if self._is_retryable(exc):
                raise _RetryableTermAnalysisError(type(exc).__name__) from exc
            raise request_error(exc) from exc
        return response_text(response)

    def _build_batches(
        self,
        chapter: TranslatedChapter,
        used_terms: Mapping[str, str],
    ) -> tuple[str, ...]:
        source = chapter.source_chapter
        header = (
            f"日文作品名稱：{source.title}\n"
            f"日文章節名稱：{source.chapter_title}\n\n"
        )
        used = format_term_pairs(used_terms)
        if used:
            header += f"本章翻譯使用過的固定譯名：\n{used}\n\n"
        pairs = [
            f"【日文原文】\n{item.source_chunk.text}\n\n"
            f"【繁體中文譯文】\n{item.translated_text}\n"
            for item in chapter.chunks
        ]
        batches: list[str] = []
        current = header
        for pair in pairs:
            if len(current) > len(header) and len(current) + len(pair) > self._max_batch_chars:
                batches.append(current)
                current = header
            current += pair + "\n"
        batches.append(current)
        return tuple(batches)

    @staticmethod
    def _parse_response(value: Any) -> dict[str, str]:
        if not isinstance(value, str) or not value.strip():
            raise invalid_response("LLM 回傳內容無效", value)
        fenced = _CODE_FENCE.fullmatch(value)
        if fenced:
            value = fenced.group(1)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise invalid_response("LLM 回傳格式錯誤", value) from exc
        if not isinstance(payload, dict):
            raise invalid_response("LLM 回傳格式錯誤", value)
        result: dict[str, str] = {}
        for source, translation in payload.items():
            if not isinstance(source, str) or not isinstance(translation, str):
                raise invalid_response("LLM 回傳格式錯誤", value)
            result[source] = translation
        return result

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
            raise TermMemoryError("無法初始化 Gemini 專有名詞分析器。") from exc


__all__ = ["ApiTermAnalyzer"]
