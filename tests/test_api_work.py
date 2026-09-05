"""Offline tests for structured work metadata API translation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import TranslationError
from core.models import NovelChapterEntry, NovelWork
from core.prompt_contracts import WORK_METADATA_OUTPUT_CONTRACT
from translators.api_work import ApiWorkTranslator
from translators.work_base import BaseWorkTranslator


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


def make_work() -> NovelWork:
    return NovelWork(
        "n1234ab",
        "https://ncode.syosetu.com/n1234ab/",
        "作品名",
        "作者",
        "作品摘要。",
        (
            NovelChapterEntry(
                1,
                "第一章",
                "https://ncode.syosetu.com/n1234ab/1/",
            ),
        ),
    )


def prompt_file(tmp_path: Path) -> Path:
    path = tmp_path / "work-prompt.txt"
    path.write_text("只輸出 JSON", encoding="utf-8")
    return path


def test_gemini_translates_title_and_synopsis_in_one_request(tmp_path: Path) -> None:
    response = SimpleNamespace(
        text=json.dumps(
            {
                "traditional_chinese_title": "中文作品名",
                "traditional_chinese_synopsis": "中文摘要。",
            },
            ensure_ascii=False,
        )
    )
    call = SequenceCallable([response])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=call))
    translator = ApiWorkTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(tmp_path),
        client=client,
    )

    result = translator.translate(make_work())

    assert isinstance(translator, BaseWorkTranslator)
    assert result.translated_title == "中文作品名"
    assert result.translated_synopsis == "中文摘要。"
    assert len(call.calls) == 1
    prompt = call.calls[0]["contents"]
    assert "作品名稱：\n作品名" in prompt
    assert "作品摘要：\n作品摘要。" in prompt
    assert prompt.endswith(WORK_METADATA_OUTPUT_CONTRACT)
    assert "system_instruction" not in call.calls[0]["config"]
    assert call.calls[0]["config"]["response_mime_type"] == "application/json"


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not-json",
        "{}",
        '{"traditional_chinese_title":"名稱","traditional_chinese_synopsis":""}',
        '```json\n{"traditional_chinese_title":"名稱","traditional_chinese_synopsis":"摘要"}\n```',
    ],
)
def test_invalid_structured_responses_are_rejected(tmp_path: Path, response: str) -> None:
    call = SequenceCallable([SimpleNamespace(text=response)])
    translator = ApiWorkTranslator(
        model="model",
        api_key="secret",
        prompt_path=prompt_file(tmp_path),
        client=SimpleNamespace(models=SimpleNamespace(generate_content=call)),
    )
    with pytest.raises(TranslationError):
        translator.translate(make_work())
