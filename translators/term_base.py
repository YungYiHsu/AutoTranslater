"""Abstract contract for post-translation proper-noun analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from core.models import TranslatedChapter


class BaseTermAnalyzer(ABC):
    @property
    def checkpoint_identity(self) -> str:
        """Return a stable identity for automatic term-analysis behavior."""
        return f"{type(self).__module__}.{type(self).__qualname__}"

    @abstractmethod
    def analyze(
        self,
        chapter: TranslatedChapter,
        used_terms: Mapping[str, str],
    ) -> dict[str, str]:
        """Return newly observed Japanese-to-Chinese term candidates."""


__all__ = ["BaseTermAnalyzer"]
