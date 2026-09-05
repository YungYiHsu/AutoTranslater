"""Validated, atomic checkpoints for resumable chapter translation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.exceptions import CheckpointError
from core.models import NovelChapter, TextChunk, TranslatedChunk

_SCHEMA_VERSION = 1


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
    """Load and update one JSON checkpoint per content-addressed job."""

    def __init__(
        self,
        directory: Path,
        *,
        replace_file: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self._directory = Path(directory)
        self._replace_file = replace_file

    def path_for(self, job: CheckpointJob) -> Path:
        """Return the deterministic, portable checkpoint path for a job."""
        if not isinstance(job, CheckpointJob):
            raise TypeError("job must be a CheckpointJob")
        return self._directory / f"{job.job_id}.json"

    def load(self, job: CheckpointJob) -> tuple[TranslatedChunk, ...]:
        """Load the validated consecutive prefix already translated for a job."""
        path = self.path_for(job)
        if not path.exists():
            return ()
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"Unable to read valid checkpoint JSON from {path.name}."
            ) from exc
        return self._validate_state(raw, job)

    def save_chunk(self, job: CheckpointJob, chunk: TranslatedChunk) -> Path:
        """Append exactly the next translated chunk and atomically persist it."""
        if not isinstance(chunk, TranslatedChunk):
            raise TypeError("chunk must be a TranslatedChunk")
        completed = list(self.load(job))
        if (
            chunk.index >= len(job.chunks)
            or chunk.source_hash != job.chunks[chunk.index].content_hash
        ):
            raise CheckpointError("Translated chunk does not belong to this checkpoint job.")

        if chunk.index < len(completed):
            if completed[chunk.index].translated_text != chunk.translated_text:
                raise CheckpointError("Checkpoint already contains a different translation.")
            return self.path_for(job)
        if chunk.index != len(completed):
            raise CheckpointError("Translated chunks must be saved consecutively without gaps.")

        completed.append(chunk)
        state = {
            "schema_version": _SCHEMA_VERSION,
            "job": job.metadata(),
            "translations": [
                {
                    "index": translated.index,
                    "source_hash": translated.source_hash,
                    "translated_text": translated.translated_text,
                }
                for translated in completed
            ],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        destination = self.path_for(job)
        self._atomic_write(destination, state)
        return destination

    def clear(self, job: CheckpointJob) -> bool:
        """Delete a job checkpoint when explicitly requested by the caller."""
        path = self.path_for(job)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CheckpointError(f"Unable to remove checkpoint {path.name}.") from exc
        return True

    def _validate_state(
        self,
        raw: Any,
        job: CheckpointJob,
    ) -> tuple[TranslatedChunk, ...]:
        if not isinstance(raw, dict):
            raise CheckpointError("Checkpoint root must be a JSON object.")
        if raw.get("schema_version") != _SCHEMA_VERSION:
            raise CheckpointError("Checkpoint schema version is unsupported.")
        if raw.get("job") != job.metadata():
            raise CheckpointError("Checkpoint identity does not match the current job.")

        translations = raw.get("translations")
        if not isinstance(translations, list):
            raise CheckpointError("Checkpoint translations must be a list.")
        completed: list[TranslatedChunk] = []
        for expected_index, item in enumerate(translations):
            if not isinstance(item, dict) or set(item) != {
                "index",
                "source_hash",
                "translated_text",
            }:
                raise CheckpointError("Checkpoint contains an invalid translation entry.")
            if expected_index >= len(job.chunks) or item["index"] != expected_index:
                raise CheckpointError("Checkpoint translation indexes are not consecutive.")
            source_chunk = job.chunks[expected_index]
            if item["source_hash"] != source_chunk.content_hash:
                raise CheckpointError("Checkpoint source chunk hash does not match.")
            translated_text = item["translated_text"]
            if not isinstance(translated_text, str) or not translated_text.strip():
                raise CheckpointError("Checkpoint translated text must not be blank.")
            completed.append(
                TranslatedChunk(
                    source_chunk=source_chunk,
                    translated_text=translated_text,
                )
            )
        return tuple(completed)

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
