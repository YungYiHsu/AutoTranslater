"""Tests for plain-text output."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import FormatterError
from formatters.txt_formatter import TxtFormatter
from tests.formatter_helpers import make_translated_chapter


def test_save_writes_windows_friendly_text_and_metadata(tmp_path: Path) -> None:
    chapter = make_translated_chapter(
        translations=("第一段。\n\n", "　第二段。"),
        translated_work_title="中文作品",
        translated_chapter_title="中文第一章",
    )
    destination = TxtFormatter().save(chapter, tmp_path / "nested")

    assert destination.name == "0001 - 測試小說.txt"
    assert destination.read_bytes().startswith(b"\xef\xbb\xbf")
    content = destination.read_text(encoding="utf-8-sig")
    assert "作品：中文作品" in content
    assert "章節：中文第一章" in content
    assert "日文作品名：測試小說" in content
    assert "日文章節名：第一章" in content
    assert "翻譯引擎：fake / deterministic-v1" in content
    assert content.endswith("第一段。\n\n　第二段。")


def test_save_requires_explicit_overwrite_for_an_existing_file(tmp_path: Path) -> None:
    chapter = make_translated_chapter()
    first = TxtFormatter().save(chapter, tmp_path)
    with pytest.raises(FormatterError, match="already exists"):
        TxtFormatter().save(chapter, tmp_path)
    second = TxtFormatter(overwrite=True).save(chapter, tmp_path)
    assert first.name == "0001 - 測試小說.txt"
    assert second == first


def test_save_wraps_filesystem_errors(tmp_path: Path) -> None:
    chapter = make_translated_chapter()
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("occupied", encoding="utf-8")
    with pytest.raises(FormatterError, match="TXT"):
        TxtFormatter().save(chapter, output_file)


def test_save_rejects_wrong_model_type(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        TxtFormatter().save(object(), tmp_path)  # type: ignore[arg-type]
