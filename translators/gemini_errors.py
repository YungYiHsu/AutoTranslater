"""Provider-error inspection shared by every Gemini strategy."""

from __future__ import annotations

import math
import re

from core.exceptions import GeminiFreeTierQuotaError

_FREE_TIER_MARKERS = (
    "free_tier",
    "free tier",
    "freetier",
)
_RETRY_AFTER = re.compile(
    r"(?:retry\s+in|retrydelay['\"]?\s*[:=]\s*['\"]?)\s*"
    r"(?P<seconds>\d+(?:\.\d+)?)\s*s",
    re.IGNORECASE,
)


def free_tier_quota_error(exc: Exception) -> GeminiFreeTierQuotaError | None:
    """Convert only an explicit Gemini free-tier 429 into a user-facing error."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status != 429:
        return None

    details = " ".join(
        str(value)
        for value in (
            exc,
            getattr(exc, "response_json", None),
            getattr(exc, "body", None),
            getattr(exc, "message", None),
            exc.args,
        )
        if value is not None
    )
    lowered = details.lower()
    if not any(marker in lowered for marker in _FREE_TIER_MARKERS):
        return None

    match = _RETRY_AFTER.search(details)
    retry_after = math.ceil(float(match.group("seconds"))) if match else None
    return GeminiFreeTierQuotaError(retry_after)


def raise_if_free_tier_quota(exc: Exception) -> None:
    """Raise the normalized quota error while preserving the SDK cause."""
    quota_error = free_tier_quota_error(exc)
    if quota_error is not None:
        raise quota_error from exc


__all__ = ["free_tier_quota_error", "raise_if_free_tier_quota"]
