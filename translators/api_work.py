"""Gemini work metadata translation strategy."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import AppConfig
from core.exceptions import TranslationError
from core.models import NovelWork, TranslatedNovelWork
from core.paths import get_resource_path
from translators.gemini_errors import raise_if_free_tier_quota
from translators.work_base import BaseWorkTranslator

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_ERROR_NAMES = (
    "connection",
    "internalserver",
    "ratelimit",
    "resourceexhausted",
    "serviceunavailable",
    "timeout",
)
_EXPECTED_KEYS = {"traditional_chinese_title", "traditional_chinese_synopsis"}


class _RetryableProviderError(Exception):
    """Internal marker consumed by tenacity."""


class ApiWorkTranslator(BaseWorkTranslator):
    """Translate a title and synopsis through one structured API request."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        retry_attempts: int = 3,
        prompt_path: Path | None = None,
        client: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if (
            not isinstance(retry_attempts, int)
            or isinstance(retry_attempts, bool)
            or retry_attempts < 0
        ):
            raise ValueError("retry_attempts must be a non-negative integer")
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._retry_attempts = retry_attempts
        path = prompt_path or get_resource_path("resources/prompts/work_metadata_translation.txt")
        self._system_prompt = self._load_prompt(path)
        self._client = client or self._create_client()
        self._sleeper = sleeper

    @classmethod
    def from_config(cls, config: AppConfig, api_key: str, **kwargs: Any) -> ApiWorkTranslator:
        return cls(
            model=config.model,
            api_key=api_key,
            retry_attempts=config.retry_attempts,
            **kwargs,
        )

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_identity(self) -> str:
        return hashlib.sha256(self._system_prompt.encode("utf-8")).hexdigest()

    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        if not isinstance(work, NovelWork):
            raise TypeError("work must be a NovelWork")
        source = json.dumps({"title": work.title, "synopsis": work.synopsis}, ensure_ascii=False)
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableProviderError),
            reraise=True,
            sleep=self._sleeper,
        )
        try:
            raw_text = retrying(self._translate_once, source)
        except _RetryableProviderError as exc:
            raise TranslationError(
                f"{self.provider} remained unavailable after {self._retry_attempts + 1} attempt(s)."
            ) from exc
        title, synopsis = self._parse_response(raw_text)
        return TranslatedNovelWork(
            source_work=work,
            translated_title=title,
            translated_synopsis=synopsis,
            provider=self.provider,
            model=self.model,
            prompt_identity=self.prompt_identity,
        )

    def _translate_once(self, source: str) -> Any:
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=source,
                config={
                    "system_instruction": self._system_prompt,
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                },
            )
            return response.text
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if _is_retryable_provider_error(exc):
                raise _RetryableProviderError(type(exc).__name__) from exc
            raise TranslationError(
                f"{self.provider} rejected the work metadata request ({type(exc).__name__})."
            ) from exc

    @staticmethod
    def _parse_response(value: Any) -> tuple[str, str]:
        if not isinstance(value, str) or not value.strip():
            raise TranslationError("翻譯 API 回傳了空白的作品資料。")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TranslationError("翻譯 API 回傳的作品資料不是有效 JSON。") from exc
        if not isinstance(payload, dict) or set(payload) != _EXPECTED_KEYS:
            raise TranslationError("翻譯 API 回傳的作品資料欄位不正確。")
        title = payload["traditional_chinese_title"]
        synopsis = payload["traditional_chinese_synopsis"]
        if not isinstance(title, str) or not title.strip():
            raise TranslationError("翻譯 API 回傳的中文作品名稱為空白。")
        if not isinstance(synopsis, str) or not synopsis.strip():
            raise TranslationError("翻譯 API 回傳的中文摘要為空白。")
        return title.strip(), synopsis.strip()

    def _create_client(self) -> Any:
        try:
            from google import genai

            return genai.Client(api_key=self._api_key)
        except Exception as exc:
            raise TranslationError(f"Unable to initialize the {self.provider} API client.") from exc

    @staticmethod
    def _load_prompt(path: Path) -> str:
        try:
            prompt = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as exc:
            raise TranslationError("Unable to read the work metadata prompt file.") from exc
        if not prompt:
            raise TranslationError("The work metadata prompt file is empty.")
        return prompt


def _is_retryable_provider_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS_CODES or status >= 500
    normalized_name = type(exc).__name__.replace("_", "").lower()
    return any(marker in normalized_name for marker in _RETRYABLE_ERROR_NAMES)


__all__ = ["ApiWorkTranslator"]
