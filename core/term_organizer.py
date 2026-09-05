"""Conservative, user-confirmed organization of one terms.json file."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.exceptions import TermMemoryError
from core.term_memory import TermMemoryStore

if TYPE_CHECKING:
    from translators.organizer_base import BaseTermOrganizer


@dataclass(frozen=True, slots=True)
class TermOrganizationProposal:
    """One model-proposed canonical mapping and exact keys to remove."""

    source: str
    translation: str
    remove: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TermOrganizationBatch:
    """One raw-memory batch that the user will review independently."""

    number: int
    total: int
    terms: dict[str, str]


@dataclass(frozen=True, slots=True)
class TermOrganizationPlan:
    """Validated preview that can be atomically applied after confirmation."""

    expected_hash: str
    original_terms: dict[str, str]
    organized_terms: dict[str, str]
    proposals: tuple[TermOrganizationProposal, ...]
    rejected_proposals: int
    api_requests: int
    batch_number: int = 1
    total_batches: int = 1

    @property
    def changed(self) -> bool:
        return self.original_terms != self.organized_terms

    @property
    def added(self) -> dict[str, str]:
        return {
            source: translation
            for source, translation in self.organized_terms.items()
            if source not in self.original_terms
        }

    @property
    def removed(self) -> dict[str, str]:
        return {
            source: translation
            for source, translation in self.original_terms.items()
            if source not in self.organized_terms
        }

    def preview_text(self) -> str:
        lines = [
            f"批次：{self.batch_number}/{self.total_batches}",
            f"整理前：{len(self.original_terms)} 筆",
            f"整理後：{len(self.organized_terms)} 筆",
            f"新增：{len(self.added)} 筆　移除：{len(self.removed)} 筆",
        ]
        if self.added:
            lines.extend(("", "【將新增】"))
            lines.extend(f"{source} → {translation}" for source, translation in self.added.items())
        if self.removed:
            lines.extend(("", "【將移除】"))
            lines.extend(
                f"{source} → {translation}" for source, translation in self.removed.items()
            )
        if self.rejected_proposals:
            lines.extend(("", f"已忽略不安全或衝突的建議：{self.rejected_proposals} 組"))
        return "\n".join(lines)


ProgressCallback = Callable[[str], None]


class TermOrganizationService:
    """Find, validate, preview, and apply manual memory cleanup."""

    def __init__(
        self,
        organizer: BaseTermOrganizer,
        *,
        store: TermMemoryStore | None = None,
    ) -> None:
        from translators.organizer_base import BaseTermOrganizer

        if not isinstance(organizer, BaseTermOrganizer):
            raise TypeError("organizer must implement BaseTermOrganizer")
        self._organizer = organizer
        self._store = store or TermMemoryStore()

    def estimate_requests(self, work_directory: Path) -> int:
        return len(self.prepare_batches(work_directory))

    def prepare_batches(
        self,
        work_directory: Path,
    ) -> tuple[TermOrganizationBatch, ...]:
        expected_hash = self._store.file_hash(work_directory)
        terms = self._store.load(work_directory)
        if self._store.file_hash(work_directory) != expected_hash:
            raise TermMemoryError("terms.json 在讀取期間已被修改，請重新執行整理。")
        raw_batches = self._organizer.batches(terms)
        total = len(raw_batches)
        return tuple(
            TermOrganizationBatch(number, total, dict(batch))
            for number, batch in enumerate(raw_batches, start=1)
        )

    def analyze_batch(
        self,
        work_directory: Path,
        batch: TermOrganizationBatch,
        *,
        progress: ProgressCallback | None = None,
    ) -> TermOrganizationPlan:
        if not isinstance(batch, TermOrganizationBatch):
            raise TypeError("batch must be a TermOrganizationBatch")
        self._emit(progress, "scanning")
        expected_hash = self._store.file_hash(work_directory)
        original = self._store.load(work_directory)
        if self._store.file_hash(work_directory) != expected_hash:
            raise TermMemoryError("terms.json 在讀取期間已被修改，請重新執行整理。")
        current_batch = {
            source: original[source]
            for source in batch.terms
            if source in original
        }
        if len(current_batch) < 2:
            return TermOrganizationPlan(
                expected_hash,
                original,
                dict(original),
                (),
                0,
                0,
                batch.number,
                batch.total,
            )

        self._emit(progress, "requesting")
        suggestions = self._organizer.organize(current_batch)
        self._emit(progress, "validating")
        organized = dict(original)
        accepted: list[TermOrganizationProposal] = []
        consolidated, rejected = self._consolidate(suggestions)
        for suggestion in consolidated:
            if not self._is_safe(suggestion, original, current_batch):
                rejected += 1
                continue
            organized[suggestion.source] = suggestion.translation
            for source in suggestion.remove:
                if source != suggestion.source:
                    organized.pop(source, None)
            accepted.append(suggestion)
        return TermOrganizationPlan(
            expected_hash,
            original,
            dict(sorted(organized.items())),
            tuple(accepted),
            rejected,
            1,
            batch.number,
            batch.total,
        )

    def apply(self, work_directory: Path, plan: TermOrganizationPlan) -> Path:
        if not isinstance(plan, TermOrganizationPlan):
            raise TypeError("plan must be a TermOrganizationPlan")
        return self._store.replace_if_unchanged(
            work_directory,
            plan.organized_terms,
            expected_hash=plan.expected_hash,
        )

    @staticmethod
    def _is_safe(
        proposal: TermOrganizationProposal,
        original: dict[str, str],
        batch_terms: dict[str, str],
    ) -> bool:
        if (
            not TermOrganizationService._valid_text(proposal.source)
            or not TermOrganizationService._valid_text(proposal.translation)
            or not proposal.remove
            or len(set(proposal.remove)) != len(proposal.remove)
        ):
            return False
        existing = original.get(proposal.source)
        if existing is not None and existing != proposal.translation:
            return False
        represented: set[str] = set()
        if existing == proposal.translation:
            represented.add(proposal.source)
        for source in proposal.remove:
            if (
                source not in original
                or source not in batch_terms
                or not TermOrganizationService._valid_text(source)
            ):
                return False
            represented.add(source)
        return len(represented) >= 2

    @staticmethod
    def _valid_text(value: str) -> bool:
        stripped = value.strip()
        return (
            bool(stripped)
            and len(stripped) <= 200
            and "\n" not in stripped
            and "\r" not in stripped
            and any(unicodedata.category(char).startswith("L") for char in stripped)
        )

    @staticmethod
    def _consolidate(
        suggestions: tuple[TermOrganizationProposal, ...],
    ) -> tuple[tuple[TermOrganizationProposal, ...], int]:
        """Deduplicate one response and discard internally conflicting advice."""
        unique = tuple(dict.fromkeys(suggestions))
        by_source: dict[str, list[TermOrganizationProposal]] = {}
        for proposal in unique:
            by_source.setdefault(proposal.source, []).append(proposal)

        conflicting: set[TermOrganizationProposal] = set()
        for proposals in by_source.values():
            if len({proposal.translation for proposal in proposals}) > 1:
                conflicting.update(proposals)

        removal_owners: dict[str, set[tuple[str, str]]] = {}
        for proposal in unique:
            owner = (proposal.source, proposal.translation)
            for source in proposal.remove:
                removal_owners.setdefault(source, set()).add(owner)
        conflicting_removals = {
            source for source, owners in removal_owners.items() if len(owners) > 1
        }
        canonical_sources = {proposal.source for proposal in unique}
        for proposal in unique:
            if any(source in conflicting_removals for source in proposal.remove):
                conflicting.add(proposal)
            if any(
                source != proposal.source and source in canonical_sources
                for source in proposal.remove
            ):
                conflicting.add(proposal)

        merged: list[TermOrganizationProposal] = []
        for source, proposals in by_source.items():
            valid = [proposal for proposal in proposals if proposal not in conflicting]
            if not valid:
                continue
            translation = valid[0].translation
            removals = tuple(
                sorted({item for proposal in valid for item in proposal.remove})
            )
            merged.append(TermOrganizationProposal(source, translation, removals))
        return tuple(sorted(merged, key=lambda item: item.source)), len(conflicting)

    @staticmethod
    def _emit(progress: ProgressCallback | None, stage: str) -> None:
        if progress is not None:
            progress(stage)
__all__ = [
    "TermOrganizationBatch",
    "TermOrganizationPlan",
    "TermOrganizationProposal",
    "TermOrganizationService",
]
