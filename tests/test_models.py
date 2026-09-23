"""Tests for immutable data shared by pipeline strategies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk


def make_chapter() -> NovelChapter:
    return NovelChapter(
        title=" 小說標題 ",
        chapter_title=" 第一話 ",
        source_url=" https://example.test/novel/1 ",
        original_text="最初の段落。\n\n次の段落。",
    )


def test_novel_chapter_trims_metadata_but_preserves_text() -> None:
    chapter = make_chapter()

    assert chapter.title == "小說標題"
    assert chapter.chapter_title == "第一話"
    assert chapter.source_url == "https://example.test/novel/1"
    assert chapter.original_text == "最初の段落。\n\n次の段落。"


@pytest.mark.parametrize("field_name", ["title", "chapter_title", "source_url", "original_text"])
def test_novel_chapter_rejects_blank_required_fields(field_name: str) -> None:
    values: dict[str, Any] = {
        "title": "小說標題",
        "chapter_title": "第一話",
        "source_url": "https://example.test/novel/1",
        "original_text": "本文。",
    }
    values[field_name] = "   "

    with pytest.raises(ValueError, match=field_name):
        NovelChapter(**values)


def test_models_are_immutable() -> None:
    chapter = make_chapter()

    with pytest.raises(FrozenInstanceError):
        chapter.title = "別的標題"  # type: ignore[misc]


def test_text_chunk_generates_stable_metadata_and_preserves_text() -> None:
    text = "一行目。\n\n二行目。"
    first = TextChunk(index=0, text=text)
    second = TextChunk(index=3, text=text)

    assert first.char_count == len(text)
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    assert first.text == text


@pytest.mark.parametrize("index", [-1, True, 1.5])
def test_text_chunk_rejects_invalid_indexes(index: object) -> None:
    with pytest.raises(ValueError, match="index"):
        TextChunk(index=index, text="本文")  # type: ignore[arg-type]


def test_text_chunk_rejects_blank_text() -> None:
    with pytest.raises(ValueError, match="text"):
        TextChunk(index=0, text="\n\t")


def test_translated_chunk_is_tied_to_exact_source() -> None:
    source = TextChunk(index=2, text="原文")
    translated = TranslatedChunk(source_chunk=source, translated_text="譯文")

    assert translated.source_chunk is source
    assert translated.index == 2
    assert translated.source_hash == source.content_hash


def test_translated_chunk_rejects_blank_translation() -> None:
    with pytest.raises(ValueError, match="translated_text"):
        TranslatedChunk(source_chunk=TextChunk(0, "原文"), translated_text=" ")


def test_translated_chapter_accepts_ordered_complete_chunks() -> None:
    chunks = tuple(
        TranslatedChunk(TextChunk(index, f"原文{index}"), f"譯文{index}") for index in range(2)
    )

    chapter = TranslatedChapter(
        source_chapter=make_chapter(),
        chunks=chunks,
        provider=" fake ",
        model=" deterministic-v1 ",
    )

    assert chapter.chunks == chunks
    assert chapter.provider == "fake"
    assert chapter.model == "deterministic-v1"


def test_translated_chapter_rejects_missing_or_out_of_order_chunks() -> None:
    chunk = TranslatedChunk(TextChunk(1, "原文"), "譯文")

    with pytest.raises(ValueError, match="ordered consecutively"):
        TranslatedChapter(make_chapter(), (chunk,), "fake", "test")

    with pytest.raises(ValueError, match="non-empty tuple"):
        TranslatedChapter(make_chapter(), (), "fake", "test")
