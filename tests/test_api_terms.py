"""Gemini proper-noun analysis tests without network access."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import TermMemoryError
from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk
from translators.api_terms import ApiTermAnalyzer


class SequenceCallable:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def make_chapter(parts: tuple[tuple[str, str], ...] = (("アリス與クロム。", "愛麗絲與克羅姆。"),)) -> TranslatedChapter:
    source_text = "".join(source for source, _translation in parts)
    source = NovelChapter("作品", "章節", "https://example.test/1", source_text)
    translated = tuple(
        TranslatedChunk(TextChunk(index, original), translation)
        for index, (original, translation) in enumerate(parts)
    )
    return TranslatedChapter(source, translated, "gemini", "model")


def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "terms-prompt.txt"
    path.write_text("只輸出 JSON", encoding="utf-8")
    return path


def test_analyzer_sends_only_used_memory_and_returns_simple_mapping(tmp_path: Path) -> None:
    call = SequenceCallable(
        [SimpleNamespace(text=json.dumps({"クロム": "克羅姆"}, ensure_ascii=False))]
    )
    analyzer = ApiTermAnalyzer(
        model="model",
        api_key="key",
        prompt_path=prompt_path(tmp_path),
        client=SimpleNamespace(models=SimpleNamespace(generate_content=call)),
    )
    result = analyzer.analyze(make_chapter(), {"アリス": "愛麗絲"})
    assert result == {"クロム": "克羅姆"}
    content = call.calls[0]["contents"]
    assert "アリス → 愛麗絲" in content
    assert "未使用詞" not in content
    assert call.calls[0]["config"]["response_mime_type"] == "application/json"


def test_long_analysis_batches_and_discards_cross_batch_conflict(tmp_path: Path) -> None:
    call = SequenceCallable(
        [
            SimpleNamespace(text='{"クロ":"克羅","新詞":"新譯"}'),
            SimpleNamespace(text='{"クロ":"庫羅"}'),
        ]
    )
    analyzer = ApiTermAnalyzer(
        model="model",
        api_key="key",
        prompt_path=prompt_path(tmp_path),
        client=SimpleNamespace(models=SimpleNamespace(generate_content=call)),
        max_batch_chars=40,
    )
    result = analyzer.analyze(make_chapter((("クロ。", "克羅。"), ("クロ新詞。", "庫羅新譯。"))), {})
    assert result == {"新詞": "新譯"}
    assert len(call.calls) == 2


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    call = SequenceCallable([SimpleNamespace(text="not-json")])
    analyzer = ApiTermAnalyzer(
        model="model",
        api_key="key",
        prompt_path=prompt_path(tmp_path),
        client=SimpleNamespace(models=SimpleNamespace(generate_content=call)),
    )
    with pytest.raises(TermMemoryError, match="有效 JSON"):
        analyzer.analyze(make_chapter(), {})
