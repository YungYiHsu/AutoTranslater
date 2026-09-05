"""Tests for shared output helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from formatters.utils import (
    atomic_write_text,
    build_output_stem,
    extract_chapter_number,
    merge_translated_text,
    sanitize_filename_component,
)
from tests.formatter_helpers import make_translated_chapter


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  測試小說. ", "測試小說"),
        ("章<一>:「開始」/\\|?*", "章_一__「開始」_____"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("...", "untitled"),
    ],
)
def test_sanitize_filename_component(value: str, expected: str) -> None:
    assert sanitize_filename_component(value) == expected


def test_sanitize_filename_component_truncates_and_validates() -> None:
    assert sanitize_filename_component("abcdef", max_length=3) == "abc"
    with pytest.raises(TypeError):
        sanitize_filename_component(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        sanitize_filename_component("name", max_length=0)


def test_build_output_stem_is_safe_and_bounded() -> None:
    chapter = make_translated_chapter(title="小說:測試", chapter_title="第一/章")
    assert build_output_stem(chapter) == "0001 - 小說_測試"
    assert len(build_output_stem(chapter, max_length=8)) <= 8
    with pytest.raises(ValueError):
        build_output_stem(chapter, max_length=0)


def test_build_output_stem_pads_chapter_number_to_four_digits() -> None:
    chapter = make_translated_chapter(source_url="https://ncode.syosetu.com/n1234ab/123/")
    assert build_output_stem(chapter) == "0123 - 測試小說"


def test_build_output_stem_falls_back_when_url_has_no_chapter_number() -> None:
    chapter = make_translated_chapter(
        source_url="https://example.test/local-sample",
        chapter_title="第一/章",
    )
    assert build_output_stem(chapter) == "第一_章 - 測試小說"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ncode.syosetu.com/n1234ab/12/", "12"),
        ("https://ncode.syosetu.com/n1234ab/001?view=1", "001"),
        ("https://ncode.syosetu.com/n1234ab/", None),
        ("https://example.test/chapter/１２/", None),
    ],
)
def test_extract_chapter_number_uses_exact_final_ascii_digits(
    url: str,
    expected: str | None,
) -> None:
    assert extract_chapter_number(url) == expected


def test_atomic_write_text_creates_parent_and_leaves_no_temp_file(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "result.txt"
    atomic_write_text(destination, "繁體中文", encoding="utf-8")
    assert destination.read_text(encoding="utf-8") == "繁體中文"
    assert list(destination.parent.glob("*.tmp")) == []


def test_merge_translated_text_preserves_exact_boundaries() -> None:
    chapter = make_translated_chapter(translations=("甲\n\n", "　乙。"))
    assert merge_translated_text(chapter) == "甲\n\n　乙。"
