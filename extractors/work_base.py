"""Abstract contract for extracting a novel work and its chapter index."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from core.models import NovelWork

CatalogProgress = Callable[[int, int, str], None]


class BaseWorkExtractor(ABC):
    """Extract work-level metadata independently from chapter content."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this extractor supports the supplied work URL."""

    @abstractmethod
    def extract(self, url: str) -> NovelWork:
        """Download and normalize a work and its complete chapter index."""

    def extract_with_progress(
        self,
        url: str,
        *,
        progress: CatalogProgress | None = None,
        force_refresh: bool = False,
    ) -> NovelWork:
        """Extract with optional catalog progress; basic strategies ignore the extras."""
        return self.extract(url)

    def resolve_chapter(self, work: NovelWork, number: int) -> NovelWork:
        """Ensure a requested chapter exists; basic strategies use the loaded index."""
        work.get_chapter(number)
        return work


__all__ = ["BaseWorkExtractor", "CatalogProgress"]
