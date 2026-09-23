"""Offline tests for the external LLM translation strategy."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import AppConfig
from core.exceptions import ApiRequestError, InvalidLlmResponseError, TranslationError
from core.models import TextChunk
from core.prompt_contracts import CHAPTER_OUTPUT_CONTRACT
from translators.api_llm import (
    ApiLlmTranslator,
    _is_retryable_provider_error,
    _normalize_response,
)
from translators.base import BaseTranslator


class ProviderFailure(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class SequenceCallable:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def gemini_client(call: SequenceCallable) -> Any:
    return SimpleNamespace(models=SimpleNamespace(generate_content=call))


def prompt_file(tmp_path: Path, content: str = "請翻譯成繁體中文。") -> Path:
    path = tmp_path / "prompt.txt"
    path.write_text(content, encoding="utf-8")
    return path


def test_gemini_translation_uses_external_prompt_and_preserves_source(tmp_path: Path) -> None:
    response = SimpleNamespace(text="　夜幕降臨。\n")
    call = SequenceCallable([response])
    translator = ApiLlmTranslator(
        model="test-gemini",
        api_key="secret",
        prompt_path=prompt_file(tmp_path, "系統翻譯規則"),
        client=gemini_client(call),
    )
    chunk = TextChunk(index=0, text="夜が訪れた。")

    result = translator.translate(chunk)

    assert isinstance(translator, BaseTranslator)
    assert translator.provider == "gemini"
    assert translator.model == "test-gemini"
    assert result.source_chunk is chunk
    assert result.translated_text == "　夜幕降臨。\n"
    assert call.calls[0]["model"] == "test-gemini"
    assert call.calls[0]["contents"].startswith("系統翻譯規則\n\n夜が訪れた。")
    assert call.calls[0]["contents"].endswith(CHAPTER_OUTPUT_CONTRACT)
    assert call.calls[0]["config"] == {"temperature": 0.2}


def test_from_config_copies_provider_model_and_retry_count(tmp_path: Path) -> None:
    call = SequenceCallable([SimpleNamespace(text="翻譯")])
    config = AppConfig(model="configured-model", retry_attempts=1)
    translator = ApiLlmTranslator.from_config(
        config,
        "secret",
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
        sleeper=lambda _seconds: None,
    )
    assert translator.provider == "gemini"
    assert translator.model == "configured-model"


def test_translation_replaces_both_runtime_markers(tmp_path: Path) -> None:
    call = SequenceCallable([SimpleNamespace(text="愛麗絲出發。")])
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(tmp_path, "正文：{Novel_Content}\n詞彙：{Term_Memory}"),
        client=gemini_client(call),
    )
    translator.translate(TextChunk(0, "アリス出發。"), {"アリス": "愛麗絲"})
    assert call.calls[0]["contents"].startswith(
        "正文：アリス出發。\n詞彙：アリス → 愛麗絲"
    )
    assert call.calls[0]["contents"].endswith(CHAPTER_OUTPUT_CONTRACT)


def test_chapter_title_translation_uses_traditional_chinese_and_terms(tmp_path: Path) -> None:
    call = SequenceCallable([SimpleNamespace(text="愛麗絲的啟程")])
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
    )

    result = translator.translate_title("アリスの旅立ち", {"アリス": "愛麗絲"})

    assert result == "愛麗絲的啟程"
    assert "繁體中文" in call.calls[0]["contents"]
    assert "アリス → 愛麗絲" in call.calls[0]["contents"]
    assert "日文章節名稱：アリスの旅立ち" in call.calls[0]["contents"]


def test_chapter_title_and_body_use_separate_requests(tmp_path: Path) -> None:
    call = SequenceCallable(
        [
            SimpleNamespace(text="愛麗絲的啟程"),
            SimpleNamespace(text="愛麗絲出發了。"),
        ]
    )
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(
            tmp_path,
            "章節：{Chapter_Title}\n正文：{Novel_Content}\n詞彙：{Term_Memory}",
        ),
        client=gemini_client(call),
    )

    translated_title = translator.translate_title(
        "アリスの旅立ち",
        {"アリス": "愛麗絲"},
    )
    result = translator.translate(
        TextChunk(0, "アリスが旅立った。"),
        {"アリス": "愛麗絲"},
    )

    assert translated_title == "愛麗絲的啟程"
    assert result.translated_chapter_title is None
    assert result.translated_text == "愛麗絲出發了。"
    assert len(call.calls) == 2
    assert "日文章節名稱：アリスの旅立ち" in call.calls[0]["contents"]
    assert "アリスの旅立ち" not in call.calls[1]["contents"]


def test_transient_error_retries_then_succeeds(tmp_path: Path) -> None:
    call = SequenceCallable(
        [ProviderFailure(429), ProviderFailure(503), SimpleNamespace(text="成功翻譯")]
    )
    waits: list[float] = []
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        retry_attempts=2,
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
        sleeper=waits.append,
    )

    result = translator.translate(TextChunk(index=0, text="原文"))

    assert result.translated_text == "成功翻譯"
    assert len(call.calls) == 3
    assert waits == [0.5, 1.0]


def test_exhausted_transient_errors_are_wrapped(tmp_path: Path) -> None:
    call = SequenceCallable([ProviderFailure(503), ProviderFailure(503)])
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        retry_attempts=1,
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ApiRequestError, match="503"):
        translator.translate(TextChunk(index=0, text="原文"))
    assert len(call.calls) == 2


def test_permanent_error_is_not_retried(tmp_path: Path) -> None:
    call = SequenceCallable([ProviderFailure(401), SimpleNamespace(text="不應執行")])
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        retry_attempts=3,
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ApiRequestError, match="401"):
        translator.translate(TextChunk(index=0, text="原文"))
    assert len(call.calls) == 1


@pytest.mark.parametrize("empty_value", [None, "", " \n "])
def test_empty_provider_response_is_rejected(tmp_path: Path, empty_value: Any) -> None:
    call = SequenceCallable([SimpleNamespace(text=empty_value)])
    translator = ApiLlmTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(tmp_path),
        client=gemini_client(call),
    )
    with pytest.raises(InvalidLlmResponseError) as caught:
        translator.translate(TextChunk(index=0, text="原文"))
    assert caught.value.llm_output


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model": " ", "api_key": "key"},
        {"model": "model", "api_key": " "},
        {"model": "model", "api_key": "key", "retry_attempts": -1},
    ],
)
def test_constructor_rejects_invalid_settings(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ApiLlmTranslator(prompt_path=prompt_file(tmp_path), client=object(), **kwargs)


def test_missing_and_empty_prompt_are_rejected(tmp_path: Path) -> None:
    common: dict[str, Any] = {
        "model": "model",
        "api_key": "key",
        "client": object(),
    }
    with pytest.raises(TranslationError, match="read"):
        ApiLlmTranslator(prompt_path=tmp_path / "missing.txt", **common)
    with pytest.raises(TranslationError, match="empty"):
        ApiLlmTranslator(prompt_path=prompt_file(tmp_path, " \n"), **common)


def test_translate_rejects_wrong_input_type(tmp_path: Path) -> None:
    translator = ApiLlmTranslator(
        model="model",
        api_key="key",
        prompt_path=prompt_file(tmp_path),
        client=object(),
    )
    with pytest.raises(TypeError, match="TextChunk"):
        translator.translate("原文")  # type: ignore[arg-type]


def test_retry_classifier_handles_builtin_status_and_sdk_style_names() -> None:
    class APITimeoutError(Exception):
        pass

    assert _is_retryable_provider_error(TimeoutError())
    assert _is_retryable_provider_error(ConnectionError())
    assert _is_retryable_provider_error(ProviderFailure(408))
    assert _is_retryable_provider_error(ProviderFailure(500))
    assert _is_retryable_provider_error(APITimeoutError())
    assert not _is_retryable_provider_error(ProviderFailure(400))
    assert not _is_retryable_provider_error(ValueError())


def test_normalize_response_rejects_non_text_and_only_removes_full_fence() -> None:
    with pytest.raises(TranslationError):
        _normalize_response(123, "gemini")
    assert _normalize_response("  保留空白\n", "gemini") == "  保留空白\n"
    assert _normalize_response("前文```text\n內容\n```", "gemini") == "前文```text\n內容\n```"
