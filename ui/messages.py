"""Thread-safe messages emitted by the desktop worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WorkerMessageKind = Literal[
    "work_found",
    "work_progress",
    "catalog_progress",
    "work_setup_done",
    "api_key_required",
    "work_overwrite_required",
    "completion_progress",
    "chapter_progress",
    "prepare_done",
    "progress",
    "run_done",
    "term_organization_progress",
    "term_organization_done",
    "term_organization_applied",
    "cancelled",
    "error",
]


@dataclass(frozen=True, slots=True)
class WorkerMessage:
    kind: WorkerMessageKind
    payload: Any = None


__all__ = ["WorkerMessage", "WorkerMessageKind"]
