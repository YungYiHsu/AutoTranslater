"""Resolve stable work folders independently from mutable work titles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.exceptions import WorkDirectoryConflictError
from formatters.utils import sanitize_filename_component


def resolve_work_directory(
    output_root: Path,
    *,
    work_id: str,
    source_url: str,
    title: str,
) -> Path:
    """Reuse a directory with the same identity, otherwise derive one from title."""
    root = Path(output_root)
    matches: list[Path] = []
    if root.is_dir():
        for directory in root.iterdir():
            if directory.is_dir() and _memory_matches(
                directory / "work.json", work_id=work_id, source_url=source_url
            ):
                matches.append(directory)
    if len(matches) > 1:
        names = "、".join(sorted(directory.name for directory in matches))
        raise WorkDirectoryConflictError(f"同一作品識別碼存在多個資料夾（{names}），請先手動整理。")
    if matches:
        return matches[0]
    return root / sanitize_filename_component(title)


def _memory_matches(path: Path, *, work_id: str, source_url: str) -> bool:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8-sig"))
        identity = payload["identity"]
        stored_id = identity.get("work_id") or identity.get("ncode")
        stored_url = identity["source_url"]
        return (
            isinstance(stored_id, str)
            and stored_id.strip().lower() == work_id.strip().lower()
            and isinstance(stored_url, str)
            and stored_url.strip().rstrip("/") == source_url.strip().rstrip("/")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return False


__all__ = ["resolve_work_directory"]
