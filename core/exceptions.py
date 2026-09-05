"""Domain-specific exceptions exposed by the translation pipeline."""

from __future__ import annotations


class NovelTranslatorError(Exception):
    """Base class for expected application errors."""


class GeminiFreeTierQuotaError(NovelTranslatorError):
    """Raised when Gemini explicitly reports exhausted free-tier quota."""

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Gemini API 已達免費方案的使用上限，因此目前無法繼續。")


class ExtractorError(NovelTranslatorError):
    """Raised when novel content cannot be extracted."""


class UnsupportedUrlError(ExtractorError):
    """Raised when no extractor supports a supplied URL."""


class WorkNotFoundError(ExtractorError):
    """Raised when a requested novel work does not exist."""


class ChapterNotFoundError(ExtractorError):
    """Raised when a requested chapter is absent from a work index."""


class WorkMemoryError(NovelTranslatorError):
    """Raised when work-level memory is invalid or cannot be saved."""


class TermMemoryError(NovelTranslatorError):
    """Raised when proper-noun memory is invalid or cannot be saved."""


class WorkDirectoryConflictError(WorkMemoryError):
    """Raised when a same-named directory belongs to another work."""


class WorkSetupCancelled(NovelTranslatorError):
    """Raised when the user declines overwriting a modified synopsis file."""


class TranslationError(NovelTranslatorError):
    """Raised when a text chunk cannot be translated."""


class TranslationCancelled(NovelTranslatorError):
    """Raised when a user cancels between translation chunks."""


class ChunkingError(NovelTranslatorError):
    """Raised when source text cannot be represented as valid chunks."""


class FormatterError(NovelTranslatorError):
    """Raised when translated output cannot be saved."""


class CheckpointError(NovelTranslatorError):
    """Raised when checkpoint state cannot be read or written."""
