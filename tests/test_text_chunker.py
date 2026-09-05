"""Tests for lossless, Japanese-aware text chunking."""

from __future__ import annotations

import hashlib

import pytest

from core.exceptions import ChunkingError
from core.text_chunker import TextChunker


def assert_valid_partition(text: str, max_chars: int) -> tuple[str, ...]:
    chunks = TextChunker(max_chars).split(text)

    assert "".join(chunk.text for chunk in chunks) == text
    assert all(chunk.char_count <= max_chars for chunk in chunks)
    assert tuple(chunk.index for chunk in chunks) == tuple(range(len(chunks)))
    assert all(
        chunk.content_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        for chunk in chunks
    )
    return tuple(chunk.text for chunk in chunks)


def test_short_text_stays_in_one_chunk() -> None:
    text = "　短い本文です。"

    assert assert_valid_partition(text, 100) == (text,)


def test_text_exactly_at_limit_stays_in_one_chunk() -> None:
    text = "あ" * 20

    assert assert_valid_partition(text, 20) == (text,)


def test_prefers_natural_paragraph_boundaries() -> None:
    first_paragraph = "第一段落。\n\n"
    second_paragraph = "第二段落。"
    text = first_paragraph + second_paragraph

    assert assert_valid_partition(text, len(first_paragraph)) == (
        first_paragraph,
        second_paragraph,
    )


def test_greedily_combines_small_paragraphs() -> None:
    text = "一。\n\n二。\n\n三。"

    parts = assert_valid_partition(text, len("一。\n\n二。\n\n"))

    assert parts == ("一。\n\n二。\n\n", "三。")


def test_preserves_multiple_blank_lines_exactly() -> None:
    text = "第一段。\n\n\n第二段。\n\n第三段。"

    parts = assert_valid_partition(text, 10)

    assert "".join(parts) == text
    assert "\n\n\n" in "".join(parts)


def test_uses_single_line_boundary_for_oversized_paragraph() -> None:
    text = "abcdefgh\nijklmnop"

    assert assert_valid_partition(text, 9) == ("abcdefgh\n", "ijklmnop")


def test_keeps_japanese_closing_quote_with_sentence() -> None:
    first_sentence = "「本当に大丈夫？」"
    second_sentence = "次の文章です。"
    text = first_sentence + second_sentence

    assert assert_valid_partition(text, len(first_sentence)) == (
        first_sentence,
        second_sentence,
    )


@pytest.mark.parametrize("ending", ["。", "！", "？", "!?」"])
def test_recognizes_japanese_sentence_endings(ending: str) -> None:
    first_sentence = f"最初の文{ending}"
    text = first_sentence + "次の文。"

    assert assert_valid_partition(text, len(first_sentence)) == (
        first_sentence,
        "次の文。",
    )


def test_uses_ascii_space_as_a_late_fallback() -> None:
    text = "alpha beta"

    assert assert_valid_partition(text, 6) == ("alpha ", "beta")


def test_hard_splits_a_single_oversized_sentence() -> None:
    text = "あ" * 25

    assert assert_valid_partition(text, 10) == ("あ" * 10, "あ" * 10, "あ" * 5)


def test_rebalances_trailing_formatting_only_part() -> None:
    text = "本文本文\n\n"

    parts = assert_valid_partition(text, 4)

    assert parts == ("本文本", "文\n\n")
    assert all(part.strip() for part in parts)


def test_rebalances_leading_formatting_only_part() -> None:
    text = "\n\n本文本文"

    parts = assert_valid_partition(text, 4)

    assert parts[0].startswith("\n\n")
    assert all(part.strip() for part in parts)


def test_realistic_extractor_shaped_text_is_lossless() -> None:
    text = (
        "　地の文が始まった。\n"
        "「会話文です」\n\n"
        "名前は山田です。\n\n\n"
        "　場面が変わった。次の文章も続いている。"
    )

    assert_valid_partition(text, 24)


@pytest.mark.parametrize("max_chars", [0, -1, True, 1.5])
def test_rejects_invalid_character_limit(max_chars: object) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        TextChunker(max_chars)  # type: ignore[arg-type]


def test_exposes_character_limit() -> None:
    assert TextChunker(4000).max_chars == 4000


def test_rejects_non_string_input() -> None:
    with pytest.raises(TypeError, match="string"):
        TextChunker(100).split(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t"])
def test_rejects_blank_input(text: str) -> None:
    with pytest.raises(ChunkingError, match="non-whitespace"):
        TextChunker(100).split(text)


def test_rejects_impossibly_large_whitespace_only_region() -> None:
    text = "甲" + ("\n" * 10) + "乙"

    with pytest.raises(ChunkingError, match="whitespace-only region"):
        TextChunker(3).split(text)
