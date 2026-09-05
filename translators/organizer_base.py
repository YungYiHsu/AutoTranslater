"""Abstract contract for manual proper-noun memory organization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from core.term_organizer import TermOrganizationProposal


class BaseTermOrganizer(ABC):
    @abstractmethod
    def batches(self, terms: Mapping[str, str]) -> tuple[dict[str, str], ...]:
        """Split a complete memory into API-sized, possibly overlapping batches."""

    @abstractmethod
    def organize(
        self,
        terms: Mapping[str, str],
    ) -> tuple[TermOrganizationProposal, ...]:
        """Analyze exactly one batch and return proposed changes."""


__all__ = ["BaseTermOrganizer"]
