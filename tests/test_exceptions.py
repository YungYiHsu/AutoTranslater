"""Tests for the public domain exception hierarchy."""

from __future__ import annotations

import pytest

from core.config import ConfigurationError, MissingApiKeyError
from core.exceptions import (
    CheckpointError,
    ChunkingError,
    ExtractorError,
    FormatterError,
    GeminiFreeTierQuotaError,
    NovelTranslatorError,
    TranslationCancelled,
    TranslationError,
    UnsupportedUrlError,
)


@pytest.mark.parametrize(
    ("error_type", "parent_type"),
    [
        (ExtractorError, NovelTranslatorError),
        (GeminiFreeTierQuotaError, NovelTranslatorError),
        (UnsupportedUrlError, ExtractorError),
        (TranslationError, NovelTranslatorError),
        (TranslationCancelled, NovelTranslatorError),
        (FormatterError, NovelTranslatorError),
        (CheckpointError, NovelTranslatorError),
        (ChunkingError, NovelTranslatorError),
        (ConfigurationError, NovelTranslatorError),
        (MissingApiKeyError, ConfigurationError),
    ],
)
def test_exception_hierarchy(
    error_type: type[Exception],
    parent_type: type[Exception],
) -> None:
    assert issubclass(error_type, parent_type)
