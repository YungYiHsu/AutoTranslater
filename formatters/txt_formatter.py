"""UTF-8 text output strategy."""

from __future__ import annotations

import logging
from pathlib import Path

from core.exceptions import FormatterError
from core.models import TranslatedChapter
from formatters.base import BaseFormatter
from formatters.utils import (
    TXT_BODY_SEPARATOR,
    atomic_write_text,
    build_output_stem,
    merge_translated_text,
)


class TxtFormatter(BaseFormatter):
    """Save a translated chapter as Windows-friendly UTF-8 text."""

    def __init__(self, *, overwrite: bool = False) -> None:
        self._overwrite = overwrite
        self._logger = logging.getLogger("novel_translator.formatter.txt")

    def save(self, chapter: TranslatedChapter, output_dir: Path) -> Path:
        """Save chapter metadata and translated content with a UTF-8 BOM."""
        if not isinstance(chapter, TranslatedChapter):
            raise TypeError("chapter must be a TranslatedChapter")
        output_path = Path(output_dir)
        content = self._render(chapter)
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            destination = output_path / f"{build_output_stem(chapter)}.txt"
            if destination.exists() and not self._overwrite:
                raise FormatterError("TXT output already exists; overwrite was not confirmed.")
            atomic_write_text(destination, content, encoding="utf-8-sig")
        except FormatterError:
            raise
        except OSError as exc:
            raise FormatterError("Unable to save the TXT output file.") from exc

        self._logger.info("Saved TXT output: %s", destination)
        return destination

    @staticmethod
    def _render(chapter: TranslatedChapter) -> str:
        source = chapter.source_chapter
        translated_text = merge_translated_text(chapter)
        return (
            f"作品：{chapter.translated_work_title}\n"
            f"章節：{chapter.translated_chapter_title}\n"
            f"日文作品名：{source.title}\n"
            f"日文章節名：{source.chapter_title}\n"
            f"來源：{source.source_url}\n"
            f"翻譯引擎：{chapter.provider} / {chapter.model}\n\n"
            f"{TXT_BODY_SEPARATOR}\n\n"
            f"{translated_text}"
        )


__all__ = ["TxtFormatter"]
