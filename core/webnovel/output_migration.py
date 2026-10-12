"""One-pass migration from the legacy flat output layout."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_RESERVED_SITE_FOLDERS = {"syosetu", "kakuyomu"}


@dataclass(frozen=True, slots=True)
class OutputMigrationResult:
    """Summary of legacy work directories handled during one scan."""

    moved: tuple[str, ...]
    conflicts: tuple[str, ...]
    failures: tuple[str, ...]


def migrate_legacy_syosetu_outputs(output_root: Path) -> OutputMigrationResult:
    """Move verified legacy Syosetu work folders under ``Syosetu``.

    Unknown folders are intentionally ignored. Existing destinations are
    reported as conflicts and are never merged or overwritten.
    """
    root = Path(output_root)
    target_root = root / "Syosetu"
    target_root.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    conflicts: list[str] = []
    failures: list[str] = []

    try:
        candidates = tuple(root.iterdir())
    except OSError as exc:
        return OutputMigrationResult((), (), (f"無法讀取輸出資料夾：{exc}",))

    for source in candidates:
        if not source.is_dir() or source.name.casefold() in _RESERVED_SITE_FOLDERS:
            continue
        if not _is_legacy_syosetu_work(source / "work.json"):
            continue
        target = target_root / source.name
        if target.exists():
            conflicts.append(source.name)
            continue
        try:
            source.rename(target)
        except OSError as exc:
            failures.append(f"{source.name}：{exc}")
        else:
            moved.append(source.name)

    return OutputMigrationResult(tuple(moved), tuple(conflicts), tuple(failures))


def _is_legacy_syosetu_work(path: Path) -> bool:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
            return False
        identity = payload["identity"]
        ncode = identity["ncode"]
        source_url = identity["source_url"]
        if not isinstance(ncode, str) or not isinstance(source_url, str):
            return False
        normalized_ncode = ncode.strip().lower()
        parsed = urlsplit(source_url.strip())
        path_parts = [part.lower() for part in parsed.path.split("/") if part]
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname == "ncode.syosetu.com"
            and path_parts == [normalized_ncode]
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return False


__all__ = ["OutputMigrationResult", "migrate_legacy_syosetu_outputs"]
