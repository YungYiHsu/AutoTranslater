"""Gemini work metadata translation strategy."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.api_usage import DailyApiUsage
from core.config import AppConfig
from core.exceptions import TranslationError
from core.gemini_settings import request_config
from core.models import NovelWork, TranslatedNovelWork
from core.paths import get_resource_path
from core.prompt_composer import compose_work_metadata_prompt, validate_work_metadata_template
from core.prompt_contracts import WORK_METADATA_OUTPUT_CONTRACT, append_prompt_contract
from translators.api_errors import invalid_response, request_error, response_text
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
        retry_attempts: int = 0,
        prompt_path: Path | None = None,
        client: Any | None = None,
        usage: DailyApiUsage | None = None,
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
        self._prompt = self._load_prompt(path)
        validate_work_metadata_template(self._prompt)
        self._client = client or self._create_client()
        self._usage = usage
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
        effective = append_prompt_contract(self._prompt, WORK_METADATA_OUTPUT_CONTRACT)
        return hashlib.sha256(effective.encode("utf-8")).hexdigest()

    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        if not isinstance(work, NovelWork):
            raise TypeError("work must be a NovelWork")
        prompt = compose_work_metadata_prompt(self._prompt, work.title, work.synopsis)
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableProviderError),
            reraise=True,
            sleep=self._sleeper,
        )
        try:
            raw_text = retrying(self._translate_once, prompt)
        except _RetryableProviderError as exc:
            raise request_error(exc) from exc
        title, synopsis = self._parse_response(raw_text)
        return TranslatedNovelWork(
            source_work=work,
            translated_title=title,
            translated_synopsis=synopsis,
            provider=self.provider,
            model=self.model,
            prompt_identity=self.prompt_identity,
        )

    def _translate_once(self, prompt: str) -> Any:
        try:
            if self._usage is not None:
                self._usage.record(self._model)
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=request_config({
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                }),
            )
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if _is_retryable_provider_error(exc):
                raise _RetryableProviderError(type(exc).__name__) from exc
            raise request_error(exc) from exc
        return response_text(response)

    @staticmethod
    def _parse_response(value: Any) -> tuple[str, str]:
        if not isinstance(value, str) or not value.strip():
            raise invalid_response("LLM 回傳內容無效", value)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise invalid_response("LLM 回傳格式錯誤", value) from exc
        if not isinstance(payload, dict) or set(payload) != _EXPECTED_KEYS:
            raise invalid_response("LLM 回傳格式錯誤", value)
        title = payload["traditional_chinese_title"]
        synopsis = payload["traditional_chinese_synopsis"]
        if not isinstance(title, str) or not title.strip():
            raise invalid_response("LLM 回傳內容無效", value)
        if not isinstance(synopsis, str) or not synopsis.strip():
            raise invalid_response("LLM 回傳內容無效", value)
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
