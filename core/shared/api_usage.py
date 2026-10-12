"""Persist today's per-model API attempts without storing request content."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from formatters.utils import atomic_write_text


def quota_date(now: datetime | None = None) -> date:
    """Gemini RPD resets at midnight in America/Los_Angeles, including DST."""
    zone = ZoneInfo("America/Los_Angeles")
    return datetime.now(zone).date() if now is None else now.astimezone(zone).date()


class DailyApiUsage:
    """Small, thread-safe counter using Gemini's Pacific quota day."""

    def __init__(self, path: Path, *, today: Callable[[], date] = quota_date) -> None:
        self.path = path
        self._today = today
        self._lock = threading.Lock()
        self._failed_date: str | None = None

    def _load(self, day: str) -> dict[str, int]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("Invalid API usage file")
        legacy = "timezone" not in data
        local_day = date.today().isoformat()  # noqa: DTZ011 - legacy files used the local date.
        legacy_today = legacy and data.get("date") == local_day
        if data.get("date") != day and not legacy_today:
            self._save(day, {})
            return {}
        models = data.get("models")
        if not isinstance(models, dict) or any(
            not isinstance(key, str)
            or type(value) is not int
            or value < 0
            for key, value in models.items()
        ):
            raise ValueError("Invalid API usage counts")
        if legacy:
            self._save(day, models)
        return models

    def _save(self, day: str, models: dict[str, int]) -> None:
        atomic_write_text(
            self.path,
            json.dumps({"date": day, "timezone": "America/Los_Angeles", "models": models},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def record(self, model: str) -> None:
        """Count an attempt; a recording failure must not interrupt translation."""
        with self._lock:
            day = self._today().isoformat()
            try:
                models = self._load(day)
                models[model] = models.get(model, 0) + 1
                self._save(day, models)
            except (OSError, ValueError, TypeError):
                self._failed_date = day
                logging.getLogger(__name__).exception("Unable to record daily API usage")

    def count(self, model: str) -> int | None:
        """Return None when today's count cannot be reported reliably."""
        with self._lock:
            day = self._today().isoformat()
            try:
                models = self._load(day)
                return None if self._failed_date == day else models.get(model, 0)
            except (OSError, ValueError, TypeError):
                return None


class ObservedApiUsage(DailyApiUsage):
    """A run-scoped observer; retries notify once and share the original counter."""

    def __init__(self, usage: DailyApiUsage, on_first_request: Callable[[], None],
                 *, count_requests: bool = True) -> None:
        self._usage = usage
        self._count_requests = count_requests
        self._on_first_request: Callable[[], None] | None = on_first_request

    def record(self, model: str) -> None:
        callback, self._on_first_request = self._on_first_request, None
        if callback is not None:
            callback()
        if self._count_requests:
            self._usage.record(model)

    def count(self, model: str) -> int | None:
        return self._usage.count(model)
