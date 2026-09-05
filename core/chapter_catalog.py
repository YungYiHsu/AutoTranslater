"""Validated, replace-in-place cache for one work's chapter index."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.exceptions import ExtractorError, WorkDirectoryConflictError
from core.models import NovelChapterEntry
from formatters.utils import atomic_write_text

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ChapterCatalog:
    ncode: str
    source_url: str
    last_page: int
    chapters: tuple[NovelChapterEntry, ...]


class ChapterCatalogStore:
    """Read and atomically replace the fixed ``chapters.json`` cache."""

    filename = "chapters.json"

    def load(self, work_directory: Path, ncode: str) -> ChapterCatalog | None:
        path = Path(work_directory) / self.filename
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            catalog = self._parse(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ExtractorError("章節目錄快取 chapters.json 已損壞，請重新整理完整目錄。") from exc
        if catalog.ncode != ncode:
            raise WorkDirectoryConflictError(
                f"資料夾「{work_directory.name}」已屬於另一部作品"
                f"（{catalog.ncode}），目前作品為 {ncode}。"
            )
        return catalog

    def save(self, work_directory: Path, catalog: ChapterCatalog) -> Path:
        directory = Path(work_directory)
        self._validate_existing_work_owner(directory, catalog.ncode)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.filename
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "identity": {"ncode": catalog.ncode, "source_url": catalog.source_url},
            "last_page": catalog.last_page,
            "chapters": [
                {"number": item.number, "title": item.title, "source_url": item.source_url}
                for item in catalog.chapters
            ],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            atomic_write_text(
                path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            raise ExtractorError("無法儲存章節目錄快取 chapters.json。") from exc
        return path

    @staticmethod
    def _validate_existing_work_owner(directory: Path, ncode: str) -> None:
        memory_path = directory / "work.json"
        if not memory_path.exists():
            return
        try:
            payload = json.loads(memory_path.read_text(encoding="utf-8-sig"))
            existing = payload["identity"]["ncode"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ExtractorError("作品記憶 work.json 已損壞，無法安全更新章節快取。") from exc
        if existing != ncode:
            raise WorkDirectoryConflictError(
                f"資料夾「{directory.name}」已屬於另一部作品（{existing}），目前作品為 {ncode}。"
            )

    @staticmethod
    def _parse(payload: Any) -> ChapterCatalog:
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError
        identity = payload["identity"]
        ncode = identity["ncode"]
        source_url = identity["source_url"]
        last_page = payload["last_page"]
        raw_chapters = payload["chapters"]
        if (
            not isinstance(ncode, str)
            or not ncode.strip()
            or not isinstance(source_url, str)
            or not source_url.strip()
            or not isinstance(last_page, int)
            or isinstance(last_page, bool)
            or last_page <= 0
            or not isinstance(raw_chapters, list)
            or not raw_chapters
        ):
            raise ValueError
        chapters = tuple(
            NovelChapterEntry(item["number"], item["title"], item["source_url"])
            for item in raw_chapters
            if isinstance(item, dict)
        )
        if len(chapters) != len(raw_chapters):
            raise ValueError
        numbers = [chapter.number for chapter in chapters]
        if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
            raise ValueError
        return ChapterCatalog(ncode.strip().lower(), source_url.strip(), last_page, chapters)


__all__ = ["ChapterCatalog", "ChapterCatalogStore"]
