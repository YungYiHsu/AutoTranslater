"""Abstract contract implemented by translation engines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from core.models import TextChunk, TranslatedChunk


class BaseTranslator(ABC):
    """Translate source chunks using one interchangeable engine."""

    @property
    @abstractmethod
    def provider(self) -> str:
        """Return the provider identity stored with translation results."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model identity stored with translation results."""

    @property
    @abstractmethod
    def checkpoint_identity(self) -> str:
        """Return a stable identity for all effective translation instructions."""

    @abstractmethod
    def translate(
        self,
        chunk: TextChunk,
        terms: Mapping[str, str] | None = None,
    ) -> TranslatedChunk:
        """Translate exactly one source chunk."""

    @abstractmethod
    def translate_title(
        self,
        title: str,
        terms: Mapping[str, str] | None = None,
    ) -> str:
        """Translate one Japanese chapter title into Traditional Chinese."""
