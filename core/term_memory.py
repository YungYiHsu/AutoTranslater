"""Simple per-work proper-noun memory with validated atomic updates."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.exceptions import TermMemoryError
from formatters.utils import atomic_write_text

_ONLY_PUNCTUATION_OR_NUMBER = re.compile(r"^[\W\d_]+$", re.UNICODE)


@dataclass(frozen=True, slots=True)
class TermMemoryUpdate:
    """Summary of one old-memory-wins merge."""

    added: dict[str, str]
    rejected: int
    conflicts: int


class TermMemoryStore:
    """Read, match, validate, and atomically replace one terms.json file."""

    filename = "terms.json"

    def ensure(self, work_directory: Path) -> Path:
        path = Path(work_directory) / self.filename
        if path.exists():
            self.load(work_directory)
            return path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, "{}\n", encoding="utf-8")
        except OSError as exc:
            raise TermMemoryError("無法建立專有名詞記憶 terms.json。") from exc
        return path

    def load(self, work_directory: Path) -> dict[str, str]:
        path = Path(work_directory) / self.filename
        if not path.exists():
            self.ensure(work_directory)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TermMemoryError("專有名詞記憶 terms.json 已損壞或無法讀取。") from exc
        return self._parse(payload)

    def file_hash(self, work_directory: Path) -> str:
        """Return an exact hash used to detect edits made during a preview."""
        path = self.ensure(work_directory)
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TermMemoryError("無法讀取專有名詞記憶 terms.json。") from exc

    def replace_if_unchanged(
        self,
        work_directory: Path,
        terms: Mapping[str, str],
        *,
        expected_hash: str,
    ) -> Path:
        """Atomically replace memory only when the previewed file is unchanged."""
        path = self.ensure(work_directory)
        if self.file_hash(work_directory) != expected_hash:
            raise TermMemoryError("terms.json 在整理期間已被修改，請重新執行整理。")
        validated = self._parse(dict(terms))
        rendered = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
        try:
            atomic_write_text(path, rendered, encoding="utf-8")
        except OSError as exc:
            raise TermMemoryError("無法儲存整理後的專有名詞記憶。") from exc
        return path

    def match(self, text: str, terms: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        normalized_text = self._normalize(text)
        matched = {
            source: translation
            for source, translation in terms.items()
            if self._normalize(source) in normalized_text
        }
        return dict(sorted(matched.items(), key=lambda item: (-len(item[0]), item[0])))

    def update(
        self,
        work_directory: Path,
        candidates: Mapping[str, str],
        *,
        source_text: str,
        translated_text: str,
    ) -> TermMemoryUpdate:
        current = self.load(work_directory)
        merged, update = self.merge_candidates(
            current,
            candidates,
            source_text=source_text,
            translated_text=translated_text,
        )

        if update.added:
            rendered = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
            try:
                atomic_write_text(
                    Path(work_directory) / self.filename,
                    rendered,
                    encoding="utf-8",
                )
            except OSError as exc:
                raise TermMemoryError("無法更新專有名詞記憶 terms.json。") from exc
        return update

    def merge_candidates(
        self,
        current: Mapping[str, str],
        candidates: Mapping[str, str],
        *,
        source_text: str,
        translated_text: str,
    ) -> tuple[dict[str, str], TermMemoryUpdate]:
        """Validate and merge candidates in memory without writing terms.json."""
        merged = dict(current)
        valid: dict[str, str] = {}
        rejected = 0
        for raw_source, raw_translation in candidates.items():
            pair = self._validate_candidate(
                raw_source,
                raw_translation,
                source_text=source_text,
                translated_text=translated_text,
            )
            if pair is None:
                rejected += 1
                continue
            source, translation = pair
            valid[source] = translation

        added: dict[str, str] = {}
        conflicts = 0
        for source, translation in valid.items():
            existing = merged.get(source)
            if existing is None:
                merged[source] = translation
                added[source] = translation
            elif existing != translation:
                conflicts += 1
        return dict(sorted(merged.items())), TermMemoryUpdate(added, rejected, conflicts)

    @staticmethod
    def _normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", value)

    @classmethod
    def _parse(cls, payload: Any) -> dict[str, str]:
        if not isinstance(payload, dict):
            raise TermMemoryError("terms.json 必須是原詞與譯名組成的 JSON object。")
        result: dict[str, str] = {}
        for source, translation in payload.items():
            if (
                not isinstance(source, str)
                or not isinstance(translation, str)
                or not source.strip()
                or not translation.strip()
                or "\n" in source
                or "\r" in source
                or "\n" in translation
                or "\r" in translation
            ):
                raise TermMemoryError("terms.json 含有空白、換行或非文字的原詞／譯名。")
            clean_source = source.strip()
            clean_translation = translation.strip()
            if clean_source in result and result[clean_source] != clean_translation:
                raise TermMemoryError("terms.json 含有重複且衝突的原詞。")
            result[clean_source] = clean_translation
        return dict(sorted(result.items()))

    @classmethod
    def _validate_candidate(
        cls,
        source: object,
        translation: object,
        *,
        source_text: str,
        translated_text: str,
    ) -> tuple[str, str] | None:
        if not isinstance(source, str) or not isinstance(translation, str):
            return None
        source = source.strip()
        translation = translation.strip()
        if (
            not source
            or not translation
            or "\n" in source
            or "\r" in source
            or "\n" in translation
            or "\r" in translation
            or _ONLY_PUNCTUATION_OR_NUMBER.fullmatch(source)
            or _ONLY_PUNCTUATION_OR_NUMBER.fullmatch(translation)
            or len(source) > 100
            or len(translation) > 100
        ):
            return None
        if cls._normalize(source) not in cls._normalize(source_text):
            return None
        if cls._normalize(translation) not in cls._normalize(translated_text):
            return None
        return source, translation


__all__ = ["TermMemoryStore", "TermMemoryUpdate"]
