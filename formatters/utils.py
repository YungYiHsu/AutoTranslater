"""Shared filename, collision, and atomic-write helpers for output strategies."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from pathlib import Path

from core.models import TranslatedChapter

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
    re.IGNORECASE,
)
TXT_BODY_SEPARATOR = "=" * 40


def sanitize_filename_component(value: str, *, max_length: int = 80) -> str:
    """Return a portable Windows-safe filename component."""
    if not isinstance(value, str):
        raise TypeError("filename component must be a string")
    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")

    sanitized = unicodedata.normalize("NFC", value)
    sanitized = _INVALID_FILENAME_CHARS.sub("_", sanitized)
    sanitized = sanitized.strip(" .")
    sanitized = sanitized[:max_length].rstrip(" .")
    if not sanitized:
        sanitized = "untitled"
    if _RESERVED_WINDOWS_NAME.fullmatch(sanitized):
        sanitized = f"_{sanitized}"
    return sanitized


def build_output_stem(chapter: TranslatedChapter, *, max_length: int = 160) -> str:
    """Build a safe output stem with a four-digit chapter label first."""
    if max_length <= 0:
        raise ValueError("max_length must be greater than zero")
    work_title = sanitize_filename_component(chapter.source_chapter.title, max_length=80)
    source_number = chapter.source_chapter.chapter_number
    chapter_number = str(source_number) if source_number is not None else None
    chapter_label = (
        chapter_number.zfill(4)
        if chapter_number is not None
        else sanitize_filename_component(chapter.source_chapter.chapter_title, max_length=80)
    )
    stem = f"{chapter_label} - {work_title}"
    return stem[:max_length].rstrip(" .") or "untitled"


def atomic_write_text(path: Path, content: str, *, encoding: str) -> None:
    """Write text through a temporary file in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def merge_translated_text(chapter: TranslatedChapter) -> str:
    """Concatenate translated chunks in their already-validated order."""
    return "".join(chunk.translated_text for chunk in chapter.chunks)


def read_translated_text_from_txt(path: Path, *, allow_empty: bool = False) -> str:
    """Read the editable body below the fixed TXT metadata separator."""
    content = Path(path).read_text(encoding="utf-8-sig")
    marker = f"\n{TXT_BODY_SEPARATOR}\n\n"
    _header, separator, body = content.partition(marker)
    if not separator:
        raise ValueError("TXT format is invalid because the body separator is missing.")
    if not allow_empty and not body.strip():
        raise ValueError("TXT translated body is empty.")
    return body


__all__ = [
    "TXT_BODY_SEPARATOR",
    "atomic_write_text",
    "build_output_stem",
    "merge_translated_text",
    "read_translated_text_from_txt",
    "sanitize_filename_component",
]
