"""Validated, atomic checkpoints for resumable chapter translation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.exceptions import CheckpointError
from core.models import NovelChapter, TextChunk, TranslatedChunk

_SCHEMA_VERSION = 3


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointJob:
    """Immutable identity and source chunks for one translation run."""

    source_chapter: NovelChapter
    chunks: tuple[TextChunk, ...]
    provider: str
    model: str
    prompt_hash: str
    source_hash: str
    job_id: str

    @classmethod
    def create(
        cls,
        *,
        source_chapter: NovelChapter,
        chunks: tuple[TextChunk, ...],
        provider: str,
        model: str,
        system_prompt: str,
    ) -> CheckpointJob:
        """Create a content-addressed job and validate its reconstruction contract."""
        if not isinstance(source_chapter, NovelChapter):
            raise TypeError("source_chapter must be a NovelChapter")
        if not isinstance(chunks, tuple) or not chunks:
            raise ValueError("chunks must be a non-empty tuple")
        if not all(isinstance(chunk, TextChunk) for chunk in chunks):
            raise TypeError("all chunks must be TextChunk instances")
        if tuple(chunk.index for chunk in chunks) != tuple(range(len(chunks))):
            raise ValueError("chunks must be ordered consecutively from index 0")
        if "".join(chunk.text for chunk in chunks) != source_chapter.original_text:
            raise ValueError("chunks must reconstruct source_chapter.original_text exactly")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be a non-empty string")

        normalized_provider = provider.strip()
        normalized_model = model.strip()
        source_hash = _sha256(source_chapter.original_text)
        prompt_hash = _sha256(system_prompt)
        identity = {
            "source_url": source_chapter.source_url,
            "source_hash": source_hash,
            "provider": normalized_provider,
            "model": normalized_model,
            "prompt_hash": prompt_hash,
            "chunk_hashes": [chunk.content_hash for chunk in chunks],
        }
        serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            source_chapter=source_chapter,
            chunks=chunks,
            provider=normalized_provider,
            model=normalized_model,
            prompt_hash=prompt_hash,
            source_hash=source_hash,
            job_id=_sha256(serialized),
        )

    def metadata(self) -> dict[str, Any]:
        """Return the exact JSON identity persisted alongside translations."""
        return {
            "job_id": self.job_id,
            "source_url": self.source_chapter.source_url,
            "title": self.source_chapter.title,
            "chapter_title": self.source_chapter.chapter_title,
            "source_hash": self.source_hash,
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "chunks": [
                {"index": chunk.index, "source_hash": chunk.content_hash} for chunk in self.chunks
            ],
        }


class CheckpointStore:
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
        self._validate_existing_metadata(job)
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
        self._validate_existing_metadata(job)
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
        self._validate_existing_metadata(job)
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
        self._validate_existing_metadata(job)
        completed = self.load(job)
        if (
            chunk.index >= len(job.chunks)
            or chunk.source_hash != job.chunks[chunk.index].content_hash
        ):
            raise CheckpointError("Translated chunk does not belong to this checkpoint job.")
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
        self._validate_existing_metadata(job)
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
        self._validate_existing_metadata(job)
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
        removed = False
        try:
            if directory.exists():
                shutil.rmtree(directory)
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
        self._validate_existing_metadata(job)
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

    def _validate_existing_metadata(self, job: CheckpointJob) -> None:
        if self.path_for(job).exists():
            self._validate_metadata(job)

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


__all__ = ["CheckpointJob", "CheckpointStore"]
