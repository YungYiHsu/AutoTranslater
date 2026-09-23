from __future__ import annotations

import json
from pathlib import Path

from core.output_migration import migrate_legacy_syosetu_outputs


def _write_work(path: Path, *, url: str, ncode: str = "n1234ab") -> None:
    path.mkdir(parents=True)
    (path / "work.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": {"ncode": ncode, "source_url": url},
            }
        ),
        encoding="utf-8",
    )


def test_moves_only_verified_legacy_syosetu_work(tmp_path: Path) -> None:
    legacy = tmp_path / "舊作品"
    _write_work(legacy, url="https://ncode.syosetu.com/n1234ab/")
    unknown = tmp_path / "未知作品"
    _write_work(unknown, url="https://example.com/n1234ab/")
    (tmp_path / "Kakuyomu").mkdir()

    result = migrate_legacy_syosetu_outputs(tmp_path)

    assert result.moved == ("舊作品",)
    assert not legacy.exists()
    assert (tmp_path / "Syosetu" / "舊作品" / "work.json").is_file()
    assert unknown.is_dir()
    assert (tmp_path / "Kakuyomu").is_dir()
    assert result.conflicts == ()
    assert result.failures == ()


def test_existing_destination_is_reported_without_merging(tmp_path: Path) -> None:
    legacy = tmp_path / "同名作品"
    _write_work(legacy, url="https://ncode.syosetu.com/n1234ab/")
    destination = tmp_path / "Syosetu" / "同名作品"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("保留", encoding="utf-8")

    result = migrate_legacy_syosetu_outputs(tmp_path)

    assert result.moved == ()
    assert result.conflicts == ("同名作品",)
    assert legacy.is_dir()
    assert marker.read_text(encoding="utf-8") == "保留"
