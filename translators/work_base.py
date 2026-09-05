"""Translation strategy for work-level title and synopsis metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import NovelWork, TranslatedNovelWork


class BaseWorkTranslator(ABC):
    """Translate one work's metadata without using chapter chunks."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Return the provider stored in work memory."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model stored in work memory."""

    @property
    @abstractmethod
    def prompt_identity(self) -> str:
        """Return a stable hash of the effective metadata prompt."""

    @abstractmethod
    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        """Translate a work title and synopsis in one operation."""


__all__ = ["BaseWorkTranslator"]
