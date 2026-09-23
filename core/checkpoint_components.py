"""Independent title,正文-chunk, and term-update checkpoint storage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.checkpoint import CheckpointJob
from core.exceptions import CheckpointError
from core.models import TranslatedChunk

_SCHEMA_VERSION = 3
_LEGACY_SCHEMA_VERSION = 2


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ComponentCheckpointStore:
    """Persist independently reusable translation stages under one job directory."""

    def __init__(
        self,
        directory: Path,
        *,
        replace_file: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self._directory = Path(directory)
        self._replace_file = replace_file

    def path_for(self, job: CheckpointJob) -> Path:
        """Return the deterministic directory containing this job's checkpoints."""
        if not isinstance(job, CheckpointJob):
            raise TypeError("job must be a CheckpointJob")
        return self._directory / job.job_id

    def load_title(self, job: CheckpointJob) -> str | None:
        self._ensure_migrated(job)
        path = self._title_path(job)
        if not path.exists():
            return None
        state = self._read_json(path, "title checkpoint")
        expected = {
            "schema_version": _SCHEMA_VERSION,
            "component": "title",
            "job_id": job.job_id,
            "chapter_title": job.source_chapter.chapter_title,
        }
        if not isinstance(state, dict) or any(
            state.get(key) != value for key, value in expected.items()
        ):
            raise CheckpointError("Title checkpoint identity does not match the current job.")
        translated_title = state.get("translated_title")
        if not isinstance(translated_title, str) or not translated_title.strip():
            raise CheckpointError("Title checkpoint translated title must not be blank.")
        return translated_title.strip()

    def save_title(self, job: CheckpointJob, translated_title: str) -> Path:
        """Save a translated title before the first正文 chunk completes."""
        if not isinstance(translated_title, str) or not translated_title.strip():
            raise CheckpointError("Translated title must not be blank.")
        self._ensure_migrated(job)
        self._ensure_metadata(job)
        normalized = translated_title.strip()
        path = self._title_path(job)
        existing = self.load_title(job)
        if existing is not None:
            if existing != normalized:
                raise CheckpointError("Title checkpoint already contains a different translation.")
            return path
        self._atomic_write(
            path,
            {
                "schema_version": _SCHEMA_VERSION,
                "component": "title",
                "job_id": job.job_id,
                "chapter_title": job.source_chapter.chapter_title,
                "translated_title": normalized,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        return path

    def load(self, job: CheckpointJob) -> tuple[TranslatedChunk, ...]:
        """Load the validated consecutive正文 chunk prefix."""
        self._ensure_migrated(job)
        directory = self._chunks_directory(job)
        if not directory.exists():
            return ()
        paths = sorted(directory.glob("*.json"))
        if [path.name for path in paths] != [
            f"{index:04d}.json" for index in range(len(paths))
        ]:
            raise CheckpointError("Checkpoint chunk indexes are not consecutive.")
        completed: list[TranslatedChunk] = []
        for index, path in enumerate(paths):
            if index >= len(job.chunks):
                raise CheckpointError("Checkpoint contains more chunks than the current job.")
            state = self._read_json(path, "chunk checkpoint")
            source_chunk = job.chunks[index]
            expected = {
                "schema_version": _SCHEMA_VERSION,
                "component": "chunk",
                "job_id": job.job_id,
                "index": index,
                "source_hash": source_chunk.content_hash,
            }
            if not isinstance(state, dict) or any(
                state.get(key) != value for key, value in expected.items()
            ):
                raise CheckpointError("Chunk checkpoint identity does not match the current job.")
            translated_text = state.get("translated_text")
            if not isinstance(translated_text, str) or not translated_text.strip():
                raise CheckpointError("Checkpoint translated text must not be blank.")
            completed.append(TranslatedChunk(source_chunk, translated_text))
        return tuple(completed)

    def save_chunk(self, job: CheckpointJob, chunk: TranslatedChunk) -> Path:
        """Save exactly the next正文 chunk as its own atomic file."""
        if not isinstance(chunk, TranslatedChunk):
            raise TypeError("chunk must be a TranslatedChunk")
        self._ensure_migrated(job)
        completed = self.load(job)
        if (
            chunk.index >= len(job.chunks)
            or chunk.source_hash != job.chunks[chunk.index].content_hash
        ):
            raise CheckpointError("Translated chunk does not belong to this checkpoint job.")
        if chunk.translated_chapter_title is not None:
            self.save_title(job, chunk.translated_chapter_title)
        path = self._chunk_path(job, chunk.index)
        if chunk.index < len(completed):
            if completed[chunk.index].translated_text != chunk.translated_text:
                raise CheckpointError("Checkpoint already contains a different translation.")
            return path
        if chunk.index != len(completed):
            raise CheckpointError("Translated chunks must be saved consecutively without gaps.")
        self._ensure_metadata(job)
        self._atomic_write(
            path,
            {
                "schema_version": _SCHEMA_VERSION,
                "component": "chunk",
                "job_id": job.job_id,
                "index": chunk.index,
                "source_hash": chunk.source_hash,
                "translated_text": chunk.translated_text,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        return path

    def term_completed(
        self,
        job: CheckpointJob,
        chunk: TranslatedChunk,
        analyzer_identity: str,
    ) -> bool:
        """Whether terms.json was successfully updated for this exact translation."""
        self._ensure_migrated(job)
        path = self._term_path(job, chunk.index)
        if not path.exists():
            return False
        state = self._read_json(path, "term checkpoint")
        expected = self._term_identity(job, chunk, analyzer_identity)
        return bool(
            isinstance(state, dict)
            and all(state.get(key) == value for key, value in expected.items())
            and state.get("success") is True
        )

    def save_term_completed(
        self,
        job: CheckpointJob,
        chunk: TranslatedChunk,
        analyzer_identity: str,
    ) -> Path:
        """Record only successful terms.json persistence, never candidate mappings."""
        self._ensure_migrated(job)
        self._ensure_metadata(job)
        path = self._term_path(job, chunk.index)
        self._atomic_write(
            path,
            {
                **self._term_identity(job, chunk, analyzer_identity),
                "success": True,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        return path

    def clear(self, job: CheckpointJob) -> bool:
        """Delete every component for one exact job after explicit retranslation."""
        directory = self.path_for(job)
        legacy = self._legacy_path(job)
        removed = False
        try:
            if directory.exists():
                shutil.rmtree(directory)
                removed = True
            if legacy.exists():
                legacy.unlink()
                removed = True
        except OSError as exc:
            raise CheckpointError(f"Unable to remove checkpoint {job.job_id}.") from exc
        return removed

    def clear_components(
        self,
        job: CheckpointJob,
        *,
        title: bool,
        chunks: bool,
        terms: bool,
    ) -> bool:
        """Remove only explicitly selected component checkpoints."""
        self._ensure_migrated(job)
        targets = (
            (self._title_path(job), title),
            (self._chunks_directory(job), chunks),
            (self.path_for(job) / "terms", terms),
        )
        removed = False
        try:
            for path, selected in targets:
                if not selected or not path.exists():
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed = True
        except OSError as exc:
            raise CheckpointError("Unable to clear selected checkpoint components.") from exc
        return removed

    def _ensure_migrated(self, job: CheckpointJob) -> None:
        legacy_path = self._legacy_path(job)
        if not legacy_path.exists():
            if self.path_for(job).exists():
                self._validate_metadata(job)
            return
        raw = self._read_json(legacy_path, "legacy checkpoint")
        completed = self._validate_legacy_state(raw, job)
        self._ensure_metadata(job)
        if completed:
            title = completed[0].translated_chapter_title
            if title is not None and not self._title_path(job).exists():
                self._write_title_state(job, title)
        for translated in completed:
            path = self._chunk_path(job, translated.index)
            if not path.exists():
                self._write_chunk_state(job, translated)
        try:
            legacy_path.unlink()
        except OSError as exc:
            raise CheckpointError(f"Unable to finish migrating {legacy_path.name}.") from exc

    def _validate_legacy_state(
        self, raw: Any, job: CheckpointJob
    ) -> tuple[TranslatedChunk, ...]:
        if not isinstance(raw, dict):
            raise CheckpointError("Checkpoint root must be a JSON object.")
        if raw.get("schema_version") != _LEGACY_SCHEMA_VERSION:
            raise CheckpointError("Checkpoint schema version is unsupported.")
        if raw.get("job") != job.metadata():
            raise CheckpointError("Checkpoint identity does not match the current job.")
        translations = raw.get("translations")
        if not isinstance(translations, list):
            raise CheckpointError("Checkpoint translations must be a list.")
        completed: list[TranslatedChunk] = []
        for index, item in enumerate(translations):
            if not isinstance(item, dict) or set(item) != {
                "index",
                "source_hash",
                "translated_text",
                "translated_chapter_title",
            }:
                raise CheckpointError("Checkpoint contains an invalid translation entry.")
            if index >= len(job.chunks) or item["index"] != index:
                raise CheckpointError("Checkpoint translation indexes are not consecutive.")
            source_chunk = job.chunks[index]
            if item["source_hash"] != source_chunk.content_hash:
                raise CheckpointError("Checkpoint source chunk hash does not match.")
            translated_text = item["translated_text"]
            if not isinstance(translated_text, str) or not translated_text.strip():
                raise CheckpointError("Checkpoint translated text must not be blank.")
            translated_title = item["translated_chapter_title"]
            if index == 0:
                if not isinstance(translated_title, str) or not translated_title.strip():
                    raise CheckpointError(
                        "Checkpoint first chunk must contain a translated chapter title."
                    )
            elif translated_title is not None:
                raise CheckpointError(
                    "Checkpoint later chunks must not contain a translated chapter title."
                )
            completed.append(
                TranslatedChunk(source_chunk, translated_text, translated_title)
            )
        return tuple(completed)

    def _ensure_metadata(self, job: CheckpointJob) -> None:
        directory = self.path_for(job)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "metadata.json"
        if path.exists():
            self._validate_metadata(job)
        else:
            self._atomic_write(
                path, {"schema_version": _SCHEMA_VERSION, "job": job.metadata()}
            )

    def _validate_metadata(self, job: CheckpointJob) -> None:
        path = self.path_for(job) / "metadata.json"
        if not path.exists():
            raise CheckpointError("Checkpoint metadata is missing.")
        if self._read_json(path, "checkpoint metadata") != {
            "schema_version": _SCHEMA_VERSION,
            "job": job.metadata(),
        }:
            raise CheckpointError("Checkpoint identity does not match the current job.")

    def _write_title_state(self, job: CheckpointJob, translated_title: str) -> None:
        self._atomic_write(
            self._title_path(job),
            {
                "schema_version": _SCHEMA_VERSION,
                "component": "title",
                "job_id": job.job_id,
                "chapter_title": job.source_chapter.chapter_title,
                "translated_title": translated_title.strip(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def _write_chunk_state(self, job: CheckpointJob, chunk: TranslatedChunk) -> None:
        self._atomic_write(
            self._chunk_path(job, chunk.index),
            {
                "schema_version": _SCHEMA_VERSION,
                "component": "chunk",
                "job_id": job.job_id,
                "index": chunk.index,
                "source_hash": chunk.source_hash,
                "translated_text": chunk.translated_text,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    @staticmethod
    def _term_identity(
        job: CheckpointJob,
        chunk: TranslatedChunk,
        analyzer_identity: str,
    ) -> dict[str, Any]:
        if not isinstance(analyzer_identity, str) or not analyzer_identity.strip():
            raise CheckpointError("Term analyzer identity must not be blank.")
        return {
            "schema_version": _SCHEMA_VERSION,
            "component": "terms",
            "job_id": job.job_id,
            "index": chunk.index,
            "source_hash": chunk.source_hash,
            "translation_hash": _sha256(chunk.translated_text),
            "analyzer_identity": analyzer_identity.strip(),
        }

    def _title_path(self, job: CheckpointJob) -> Path:
        return self.path_for(job) / "title.json"

    def _chunks_directory(self, job: CheckpointJob) -> Path:
        return self.path_for(job) / "chunks"

    def _chunk_path(self, job: CheckpointJob, index: int) -> Path:
        return self._chunks_directory(job) / f"{index:04d}.json"

    def _term_path(self, job: CheckpointJob, index: int) -> Path:
        return self.path_for(job) / "terms" / f"{index:04d}.done.json"

    def _legacy_path(self, job: CheckpointJob) -> Path:
        return self._directory / f"{job.job_id}.json"

    @staticmethod
    def _read_json(path: Path, label: str) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Unable to read valid {label} from {path.name}.") from exc

    def _atomic_write(self, path: Path, state: dict[str, Any]) -> None:
        serialized = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as temporary:
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            self._replace_file(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise CheckpointError(f"Unable to save checkpoint {path.name}.") from exc


__all__ = ["ComponentCheckpointStore"]
