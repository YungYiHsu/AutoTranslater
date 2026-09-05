"""Abstract contract implemented by translated-output formatters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from core.models import TranslatedChapter


class BaseFormatter(ABC):
    """Save a completed chapter translation in one output format."""

    @abstractmethod
    def save(self, chapter: TranslatedChapter, output_dir: Path) -> Path:
        """Save a chapter and return the resulting file path."""
