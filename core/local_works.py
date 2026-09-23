"""Discover valid local work memories for the editable GUI selector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class LocalWorkOption:
    """One locally remembered work shown in the GUI dropdown."""

    ncode: str
    source_url: str
    source_title: str
    translated_title: str
    site: str = "syosetu"

    @property
    def label(self) -> str:
        identifier = self.ncode.upper() if self.site == "syosetu" else self.ncode
        return f"{self.translated_title}｜{self.source_title}（{identifier}）"


def discover_local_works(output_directory: Path) -> tuple[LocalWorkOption, ...]:
    """Return valid immediate-child work memories without modifying any files."""
    root = Path(output_directory)
    if not root.is_dir():
        return ()
    found: dict[str, LocalWorkOption] = {}
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "work.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            option = _parse_option(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        found.setdefault(f"{option.site}:{option.ncode}", option)
    return tuple(
        sorted(
            found.values(),
            key=lambda item: (item.translated_title.casefold(), item.site, item.ncode),
        )
    )


def _parse_option(payload: Any) -> LocalWorkOption:
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        raise ValueError
    identity = payload["identity"]
    source = payload["source"]
    translation = payload["translation"]
    ncode = identity["ncode"]
    source_url = identity["source_url"]
    source_title = source["title"]
    translated_title = translation["title"]
    values = (ncode, source_url, source_title, translated_title)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError
    normalized_ncode = ncode.strip().lower()
    parsed = urlsplit(source_url.strip())
    parts = [part.lower() for part in parsed.path.split("/") if part]
    if parsed.hostname == "ncode.syosetu.com" and parts == [normalized_ncode]:
        site = "syosetu"
    elif parsed.hostname == "kakuyomu.jp" and parts == ["works", normalized_ncode]:
        site = "kakuyomu"
    else:
        raise ValueError
    return LocalWorkOption(
        normalized_ncode,
        source_url.strip(),
        source_title.strip(),
        translated_title.strip(),
        site,
    )


__all__ = ["LocalWorkOption", "discover_local_works"]
