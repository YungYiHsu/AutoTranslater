"""Gemini LLM translation strategy."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import AppConfig
from core.exceptions import TranslationError
from core.models import TextChunk, TranslatedChunk
from core.paths import get_resource_path
from core.prompt_composer import (
    compose_translation_prompt,
    format_term_pairs,
    validate_translation_template,
)
from translators.base import BaseTranslator
from translators.gemini_errors import raise_if_free_tier_quota

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_ERROR_NAMES = (
    "connection",
    "internalserver",
    "ratelimit",
    "resourceexhausted",
    "serviceunavailable",
    "timeout",
)
_CODE_FENCE = re.compile(r"\A\s*```(?:\w+)?\s*\n?(.*?)\n?```\s*\Z", re.DOTALL)


class _RetryableProviderError(Exception):
    """Internal marker consumed by tenacity."""


class ApiLlmTranslator(BaseTranslator):
    """Translate one chunk through Gemini."""

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
        self._prompt_path = prompt_path or get_resource_path("resources/prompts/translation.txt")
        self._system_prompt = self._load_prompt(self._prompt_path)
        validate_translation_template(self._system_prompt)
        self._client = client or self._create_client()
        self._sleeper = sleeper
        self._logger = logging.getLogger("novel_translator.translator.api")

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        api_key: str,
        **kwargs: Any,
    ) -> ApiLlmTranslator:
        """Construct the strategy from validated application settings."""
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
    def checkpoint_identity(self) -> str:
        return hashlib.sha256(self._system_prompt.encode("utf-8")).hexdigest()

    def translate(
        self,
        chunk: TextChunk,
        terms: Mapping[str, str] | None = None,
    ) -> TranslatedChunk:
        """Translate exactly one source chunk, retrying only transient failures."""
        if not isinstance(chunk, TextChunk):
            raise TypeError("chunk must be a TextChunk")

        self._logger.info(
            "Translating chunk %d (%d characters) with %s/%s",
            chunk.index,
            chunk.char_count,
            self.provider,
            self.model,
        )
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableProviderError),
            reraise=True,
            sleep=self._sleeper,
        )
        try:
            translated_text = retrying(self._translate_once, chunk.text, terms or {})
        except _RetryableProviderError as exc:
            raise TranslationError(
                f"{self.provider} remained unavailable after {self._retry_attempts + 1} attempt(s)."
            ) from exc

        return TranslatedChunk(source_chunk=chunk, translated_text=translated_text)

    def translate_title(
        self,
        title: str,
        terms: Mapping[str, str] | None = None,
    ) -> str:
        """Translate a chapter title with the same retry policy and glossary."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        retrying = Retrying(
            stop=stop_after_attempt(self._retry_attempts + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type(_RetryableProviderError),
            reraise=True,
            sleep=self._sleeper,
        )
        term_text = format_term_pairs(terms or {})
        prompt = (
            "請將以下日文章節名稱翻譯成繁體中文。保持原意與語氣，"
            "只輸出翻譯後的章節名稱，不要加入引號或說明。"
        )
        if term_text:
            prompt += f"\n\n固定專有名詞譯名：\n{term_text}"
        prompt += f"\n\n日文章節名稱：{title.strip()}"
        try:
            return retrying(self._translate_title_once, prompt)
        except _RetryableProviderError as exc:
            raise TranslationError(
                f"{self.provider} remained unavailable after "
                f"{self._retry_attempts + 1} attempt(s)."
            ) from exc

    def _translate_title_once(self, prompt: str) -> str:
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={"temperature": 0.2},
            )
            raw_text = response.text
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if _is_retryable_provider_error(exc):
                raise _RetryableProviderError(type(exc).__name__) from exc
            raise TranslationError(
                f"{self.provider} rejected the chapter title request ({type(exc).__name__})."
            ) from exc
        return _normalize_response(raw_text, self.provider).strip()

    def _translate_once(self, source_text: str, terms: Mapping[str, str]) -> str:
        try:
            prompt = compose_translation_prompt(self._system_prompt, source_text, terms)
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "temperature": 0.2,
                },
            )
            raw_text = response.text
        except Exception as exc:
            raise_if_free_tier_quota(exc)
            if _is_retryable_provider_error(exc):
                raise _RetryableProviderError(type(exc).__name__) from exc
            raise TranslationError(
                f"{self.provider} rejected the translation request ({type(exc).__name__})."
            ) from exc

        return _normalize_response(raw_text, self.provider)

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
            raise TranslationError("Unable to read the translation prompt file.") from exc
        if not prompt:
            raise TranslationError("The translation prompt file is empty.")
        return prompt


def _is_retryable_provider_error(exc: Exception) -> bool:
    """Classify common transient errors without coupling to one SDK version."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS_CODES or status >= 500

    normalized_name = type(exc).__name__.replace("_", "").lower()
    return any(marker in normalized_name for marker in _RETRYABLE_ERROR_NAMES)


def _normalize_response(value: Any, provider: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranslationError(f"{provider} returned an empty translation.")
    fenced = _CODE_FENCE.fullmatch(value)
    return fenced.group(1).strip() if fenced else value


__all__ = ["ApiLlmTranslator"]
