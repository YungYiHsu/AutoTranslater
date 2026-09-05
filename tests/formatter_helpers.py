"""Factories shared by formatter tests."""

from __future__ import annotations

from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk


def make_translated_chapter(
    *,
    title: str = "測試小說",
    chapter_title: str = "第一章",
    source_url: str = "https://example.test/novel/1?part=1&lang=ja",
    translations: tuple[str, ...] = ("第一段。\n\n", "第二段。"),
    translated_work_title: str | None = None,
    translated_chapter_title: str | None = None,
) -> TranslatedChapter:
    """Build a small, internally consistent translated chapter."""
    source_texts = tuple(f"原文區塊 {index}" for index in range(len(translations)))
    source = NovelChapter(
        title=title,
        chapter_title=chapter_title,
        source_url=source_url,
        original_text="".join(source_texts),
    )
    chunks = tuple(
        TranslatedChunk(
            source_chunk=TextChunk(index=index, text=source_text),
            translated_text=translation,
        )
        for index, (source_text, translation) in enumerate(
            zip(source_texts, translations, strict=True)
        )
    )
    return TranslatedChapter(
        source_chapter=source,
        chunks=chunks,
        provider="fake",
        model="deterministic-v1",
        translated_work_title=translated_work_title,
        translated_chapter_title=translated_chapter_title,
    )
