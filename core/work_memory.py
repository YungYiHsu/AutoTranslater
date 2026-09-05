"""Validated, atomic work-level translation memory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.exceptions import WorkDirectoryConflictError, WorkMemoryError
from core.models import NovelWork, TranslatedNovelWork
from formatters.utils import atomic_write_text

_CURRENT_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = {1, 2}


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChapterCompletion:
    """Minimal durable proof that one chapter previously completed."""

    source_hash: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class WorkMemory:
    """Validated persisted metadata needed to reuse a work translation."""

    ncode: str
    source_title: str
    source_synopsis: str
    translated_title: str
    translated_synopsis: str
    provider: str
    model: str
    prompt_identity: str
    synopsis_generated_hash: str
    completed_chapters: dict[int, ChapterCompletion]

    def matches_source(self, work: NovelWork) -> bool:
        return (
            self.ncode == work.ncode
            and text_hash(self.source_title) == text_hash(work.title)
            and text_hash(self.source_synopsis) == text_hash(work.synopsis)
        )

    def translated_work(self, source_work: NovelWork) -> TranslatedNovelWork:
        return TranslatedNovelWork(
            source_work=source_work,
            translated_title=self.translated_title,
            translated_synopsis=self.translated_synopsis,
            provider=self.provider,
            model=self.model,
            prompt_identity=self.prompt_identity,
        )


class WorkMemoryStore:
    """Read and atomically replace one work.json file."""

    filename = "work.json"

    def load(self, work_directory: Path, work: NovelWork) -> WorkMemory | None:
        path = Path(work_directory) / self.filename
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkMemoryError("作品記憶 work.json 已損壞或無法讀取。") from exc
        memory = self._parse(payload)
        if memory.ncode != work.ncode:
            raise WorkDirectoryConflictError(
                f"資料夾「{work_directory.name}」已屬於另一部作品"
                f"（{memory.ncode}），目前作品為 {work.ncode}。"
            )
        return memory

    def save(
        self,
        work_directory: Path,
        translated: TranslatedNovelWork,
        synopsis_generated_hash: str,
    ) -> Path:
        path = Path(work_directory) / self.filename
        work = translated.source_work
        existing = self.load(work_directory, work)
        completed = existing.completed_chapters if existing is not None else {}
        payload = {
            "schema_version": _CURRENT_SCHEMA_VERSION if completed else 1,
            "identity": {"ncode": work.ncode, "source_url": work.source_url},
            "source": {
                "title": work.title,
                "author": work.author,
                "synopsis": work.synopsis,
                "title_hash": text_hash(work.title),
                "synopsis_hash": text_hash(work.synopsis),
            },
            "translation": {
                "title": translated.translated_title,
                "synopsis": translated.translated_synopsis,
                "provider": translated.provider,
                "model": translated.model,
                "prompt_identity": translated.prompt_identity,
            },
            "files": {"synopsis_generated_hash": synopsis_generated_hash},
            "completed_chapters": self._serialize_completions(completed),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if not completed:
            payload.pop("completed_chapters")
        try:
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            raise WorkMemoryError("無法儲存作品記憶 work.json。") from exc
        return path

    def save_completions(
        self,
        work_directory: Path,
        work: NovelWork,
        completions: dict[int, ChapterCompletion],
    ) -> Path:
        """Replace the compact completion map while preserving all work metadata."""
        path = Path(work_directory) / self.filename
        memory = self.load(work_directory, work)
        if memory is None:
            raise WorkMemoryError("缺少 work.json，無法記錄章節完成狀態。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            payload["schema_version"] = _CURRENT_SCHEMA_VERSION
            payload["completed_chapters"] = self._serialize_completions(completions)
            payload["updated_at"] = datetime.now(UTC).isoformat()
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except WorkMemoryError:
            raise
        except Exception as exc:
            raise WorkMemoryError("無法更新章節完成紀錄。") from exc
        return path

    @staticmethod
    def _parse(payload: Any) -> WorkMemory:
        try:
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS
            ):
                raise ValueError
            identity = payload["identity"]
            source = payload["source"]
            translation = payload["translation"]
            files = payload["files"]
            values = {
                "ncode": identity["ncode"],
                "source_title": source["title"],
                "source_synopsis": source["synopsis"],
                "translated_title": translation["title"],
                "translated_synopsis": translation["synopsis"],
                "provider": translation["provider"],
                "model": translation["model"],
                "prompt_identity": translation["prompt_identity"],
                "synopsis_generated_hash": files["synopsis_generated_hash"],
            }
            if not all(isinstance(value, str) and value.strip() for value in values.values()):
                raise ValueError
            if source.get("title_hash") != text_hash(values["source_title"]):
                raise ValueError
            if source.get("synopsis_hash") != text_hash(values["source_synopsis"]):
                raise ValueError
            completions = WorkMemoryStore._parse_completions(payload.get("completed_chapters", {}))
            return WorkMemory(**values, completed_chapters=completions)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkMemoryError("作品記憶 work.json 的格式無效或版本不相容。") from exc

    @staticmethod
    def _parse_completions(payload: Any) -> dict[int, ChapterCompletion]:
        if not isinstance(payload, dict):
            raise TypeError
        result: dict[int, ChapterCompletion] = {}
        for raw_number, raw_completion in payload.items():
            if (
                not isinstance(raw_number, str)
                or not raw_number.isascii()
                or not raw_number.isdigit()
                or int(raw_number) <= 0
                or not isinstance(raw_completion, dict)
            ):
                raise ValueError
            source_hash = raw_completion.get("source_hash")
            completed_at = raw_completion.get("completed_at")
            if not isinstance(source_hash, str) or not isinstance(completed_at, str):
                raise TypeError
            if source_hash and (
                len(source_hash) != 64
                or any(character not in "0123456789abcdef" for character in source_hash)
            ):
                raise ValueError
            if not completed_at.strip():
                raise ValueError
            result[int(raw_number)] = ChapterCompletion(source_hash, completed_at)
        return result

    @staticmethod
    def _serialize_completions(
        completions: dict[int, ChapterCompletion],
    ) -> dict[str, dict[str, str]]:
        return {
            str(number): {
                "source_hash": completion.source_hash,
                "completed_at": completion.completed_at,
            }
            for number, completion in sorted(completions.items())
        }


__all__ = ["ChapterCompletion", "WorkMemory", "WorkMemoryStore", "text_hash"]
