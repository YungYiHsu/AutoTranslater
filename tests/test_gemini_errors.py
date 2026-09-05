"""Tests for recognizing Gemini free-tier quota failures."""

from __future__ import annotations

import pytest

from core.exceptions import GeminiFreeTierQuotaError
from translators.gemini_errors import free_tier_quota_error, raise_if_free_tier_quota


class GeminiFailure(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_recognizes_free_tier_429_and_rounds_retry_seconds_up() -> None:
    source = GeminiFailure(
        429,
        "Quota exceeded for generate_content_free_tier_requests. "
        "Please retry in 33.12s.",
    )
    error = free_tier_quota_error(source)
    assert isinstance(error, GeminiFreeTierQuotaError)
    assert error.retry_after_seconds == 34


@pytest.mark.parametrize(
    "source",
    [
        GeminiFailure(429, "Ordinary temporary rate limit"),
        GeminiFailure(503, "free_tier unavailable"),
    ],
)
def test_does_not_misclassify_other_provider_failures(source: Exception) -> None:
    assert free_tier_quota_error(source) is None
    raise_if_free_tier_quota(source)


def test_normalized_error_preserves_original_as_cause() -> None:
    source = GeminiFailure(429, "Free tier request quota exhausted")
    with pytest.raises(GeminiFreeTierQuotaError) as caught:
        raise_if_free_tier_quota(source)
    assert caught.value.__cause__ is source
