"""Immutable data exchanged between extraction, translation, and output strategies."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


_NCODE = re.compile(r"n[0-9a-z]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NovelChapterEntry:
    """One selectable chapter listed on a work index page."""

    number: int
    title: str
    source_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number <= 0:
            raise ValueError("number must be a positive integer")
        _require_non_blank(self.title, "title")
        _require_non_blank(self.source_url, "source_url")
        segments = [part for part in urlsplit(self.source_url).path.split("/") if part]
        if not segments or segments[-1] != str(self.number):
            raise ValueError("source_url chapter number must match number")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "source_url", self.source_url.strip())


@dataclass(frozen=True, slots=True)
class NovelWork:
    """Metadata and the complete selectable chapter index for one work."""

    ncode: str
    source_url: str
    title: str
    author: str
    synopsis: str
    chapters: tuple[NovelChapterEntry, ...]

    def __post_init__(self) -> None:
        _require_non_blank(self.ncode, "ncode")
        _require_non_blank(self.source_url, "source_url")
        _require_non_blank(self.title, "title")
        _require_non_blank(self.author, "author")
        _require_non_blank(self.synopsis, "synopsis")
        normalized_ncode = self.ncode.strip().lower()
        if _NCODE.fullmatch(normalized_ncode) is None:
            raise ValueError("ncode has an invalid format")
        segments = [part.lower() for part in urlsplit(self.source_url).path.split("/") if part]
        if segments != [normalized_ncode]:
            raise ValueError("source_url must be the matching work home URL")
        if not isinstance(self.chapters, tuple) or not self.chapters:
            raise ValueError("chapters must be a non-empty tuple")
        if not all(isinstance(chapter, NovelChapterEntry) for chapter in self.chapters):
            raise TypeError("all chapters must be NovelChapterEntry instances")
        numbers = tuple(chapter.number for chapter in self.chapters)
        if len(numbers) != len(set(numbers)):
            raise ValueError("chapter numbers must be unique")
        if numbers != tuple(sorted(numbers)):
            raise ValueError("chapters must be ordered by number")
        expected_prefix = f"/{normalized_ncode}/"
        if any(
            not urlsplit(chapter.source_url).path.lower().startswith(expected_prefix)
            for chapter in self.chapters
        ):
            raise ValueError("all chapters must belong to this work")
        object.__setattr__(self, "ncode", normalized_ncode)
        object.__setattr__(self, "source_url", self.source_url.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "author", self.author.strip())
        object.__setattr__(self, "synopsis", self.synopsis.strip())

    def get_chapter(self, number: int) -> NovelChapterEntry:
        """Return one real chapter or raise a domain-specific error."""
        from core.exceptions import ChapterNotFoundError

        for chapter in self.chapters:
            if chapter.number == number:
                return chapter
        raise ChapterNotFoundError(f"作品中不存在第 {number} 章。")


@dataclass(frozen=True, slots=True)
class TranslatedNovelWork:
    """A work title and synopsis translated independently from its chapters."""

    source_work: NovelWork
    translated_title: str
    translated_synopsis: str
    provider: str
    model: str
    prompt_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_work, NovelWork):
            raise TypeError("source_work must be a NovelWork")
        for field_name in (
            "translated_title",
            "translated_synopsis",
            "provider",
            "model",
            "prompt_identity",
        ):
            value = getattr(self, field_name)
            _require_non_blank(value, field_name)
            object.__setattr__(self, field_name, value.strip())


@dataclass(frozen=True, slots=True)
class NovelChapter:
    """A single chapter extracted from a source before translation."""

    title: str
    chapter_title: str
    source_url: str
    original_text: str

    def __post_init__(self) -> None:
        _require_non_blank(self.title, "title")
        _require_non_blank(self.chapter_title, "chapter_title")
        _require_non_blank(self.source_url, "source_url")
        _require_non_blank(self.original_text, "original_text")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "chapter_title", self.chapter_title.strip())
        object.__setattr__(self, "source_url", self.source_url.strip())


@dataclass(frozen=True, slots=True)
class TextChunk:
    """A stable, ordered piece of source text ready for translation."""

    index: int
    text: str
    content_hash: str = field(init=False)
    char_count: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 0:
            raise ValueError("index must be a non-negative integer")
        _require_non_blank(self.text, "text")
        object.__setattr__(
            self,
            "content_hash",
            hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(self, "char_count", len(self.text))


@dataclass(frozen=True, slots=True)
class TranslatedChunk:
    """A translation tied directly to the exact source chunk used to create it."""

    source_chunk: TextChunk
    translated_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_chunk, TextChunk):
            raise TypeError("source_chunk must be a TextChunk")
        _require_non_blank(self.translated_text, "translated_text")

    @property
    def index(self) -> int:
        """Return the source chunk index for convenient ordering."""
        return self.source_chunk.index

    @property
    def source_hash(self) -> str:
        """Return the source hash used for checkpoint validation."""
        return self.source_chunk.content_hash


@dataclass(frozen=True, slots=True)
class TranslatedChapter:
    """A complete chapter translation and the engine identity that produced it."""

    source_chapter: NovelChapter
    chunks: tuple[TranslatedChunk, ...]
    provider: str
    model: str
    translated_work_title: str | None = None
    translated_chapter_title: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_chapter, NovelChapter):
            raise TypeError("source_chapter must be a NovelChapter")
        if not isinstance(self.chunks, tuple) or not self.chunks:
            raise ValueError("chunks must be a non-empty tuple")
        if not all(isinstance(chunk, TranslatedChunk) for chunk in self.chunks):
            raise TypeError("all chunks must be TranslatedChunk instances")

        indexes = tuple(chunk.index for chunk in self.chunks)
        if indexes != tuple(range(len(self.chunks))):
            raise ValueError("translated chunks must be ordered consecutively from index 0")

        _require_non_blank(self.provider, "provider")
        _require_non_blank(self.model, "model")
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "model", self.model.strip())
        work_title = self.translated_work_title or self.source_chapter.title
        chapter_title = self.translated_chapter_title or self.source_chapter.chapter_title
        _require_non_blank(work_title, "translated_work_title")
        _require_non_blank(chapter_title, "translated_chapter_title")
        object.__setattr__(self, "translated_work_title", work_title.strip())
        object.__setattr__(self, "translated_chapter_title", chapter_title.strip())
