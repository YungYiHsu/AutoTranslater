"""Abstract contract implemented by novel source extractors."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import NovelChapter


class BaseExtractor(ABC):
    """Extract one novel chapter from a supported source URL."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this strategy supports the supplied URL."""

    @abstractmethod
    def extract(self, url: str) -> NovelChapter:
        """Download and return a normalized novel chapter."""
