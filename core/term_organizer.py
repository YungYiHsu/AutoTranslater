"""Conservative, user-confirmed organization of one terms.json file."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.exceptions import TermMemoryError
from core.term_memory import TermMemoryStore

if TYPE_CHECKING:
    from translators.organizer_base import BaseTermOrganizer


@dataclass(frozen=True, slots=True)
class TermOrganizationProposal:
    """One model response containing independent additions and removals."""

    additions: tuple[tuple[str, str], ...]
    removals: tuple[str, ...]


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

    def select_changes(
        self,
        *,
        additions: Collection[str],
        removals: Collection[str],
    ) -> TermOrganizationPlan:
        """Return a plan containing only user-selected additions and removals."""
        selected_additions = set(additions)
        selected_removals = set(removals)
        available_additions = self.added
        available_removals = self.removed
        if not selected_additions <= available_additions.keys():
            raise ValueError("selected additions must come from this organization plan")
        if not selected_removals <= available_removals.keys():
            raise ValueError("selected removals must come from this organization plan")

        organized = dict(self.original_terms)
        for source in selected_additions:
            organized[source] = available_additions[source]
        for source in selected_removals:
            organized.pop(source, None)
        return TermOrganizationPlan(
            expected_hash=self.expected_hash,
            original_terms=dict(self.original_terms),
            organized_terms=dict(sorted(organized.items())),
            proposals=self.proposals,
            rejected_proposals=self.rejected_proposals,
            api_requests=self.api_requests,
            batch_number=self.batch_number,
            total_batches=self.total_batches,
        )


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
        suggestion = self._organizer.organize(current_batch)
        self._emit(progress, "validating")
        organized = dict(original)
        accepted, rejected = self._validate(suggestion, original, current_batch)
        for source, translation in accepted.additions:
            organized[source] = translation
        for source in accepted.removals:
            organized.pop(source, None)
        proposals = (accepted,) if accepted.additions or accepted.removals else ()
        return TermOrganizationPlan(
            expected_hash,
            original,
            dict(sorted(organized.items())),
            proposals,
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
    def _validate(
        proposal: TermOrganizationProposal,
        original: dict[str, str],
        batch_terms: dict[str, str],
    ) -> tuple[TermOrganizationProposal, int]:
        """Keep safe independent changes and preserve old values on conflicts."""
        rejected = 0
        translations_by_source: dict[str, set[str]] = {}
        for source, translation in proposal.additions:
            translations_by_source.setdefault(source, set()).add(translation)

        requested_removals = set(proposal.removals)
        additions: dict[str, str] = {}
        for source, translations in translations_by_source.items():
            if (
                len(translations) != 1
                or source in requested_removals
                or not TermOrganizationService._valid_text(source)
            ):
                rejected += 1
                continue
            translation = next(iter(translations))
            existing = original.get(source)
            if (
                not TermOrganizationService._valid_text(translation)
                or (existing is not None and existing != translation)
            ):
                rejected += 1
                continue
            if existing is None:
                additions[source] = translation

        removals: set[str] = set()
        for source in requested_removals:
            if (
                source in translations_by_source
                or source not in original
                or source not in batch_terms
                or not TermOrganizationService._valid_text(source)
            ):
                rejected += 1
                continue
            removals.add(source)
        return (
            TermOrganizationProposal(
                tuple(sorted(additions.items())),
                tuple(sorted(removals)),
            ),
            rejected,
        )

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
    def _emit(progress: ProgressCallback | None, stage: str) -> None:
        if progress is not None:
            progress(stage)
__all__ = [
    "TermOrganizationBatch",
    "TermOrganizationPlan",
    "TermOrganizationProposal",
    "TermOrganizationService",
]
