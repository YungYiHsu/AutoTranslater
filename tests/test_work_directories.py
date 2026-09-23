from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.exceptions import WorkDirectoryConflictError
from core.work_directories import resolve_work_directory


def _memory(directory: Path, *, work_id: str, source_url: str) -> None:
    directory.mkdir(parents=True)
    (directory / "work.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": {"ncode": work_id, "source_url": source_url},
            }
        ),
        encoding="utf-8",
    )


def test_reuses_identity_directory_when_title_changes(tmp_path: Path) -> None:
    old = tmp_path / "舊作品名稱"
    _memory(old, work_id="n1234ab", source_url="https://ncode.syosetu.com/n1234ab/")

    resolved = resolve_work_directory(
        tmp_path,
        work_id="n1234ab",
        source_url="https://ncode.syosetu.com/n1234ab/",
        title="新作品名稱",
    )

    assert resolved == old
    assert not (tmp_path / "新作品名稱").exists()


def test_duplicate_identity_directories_are_not_guessed(tmp_path: Path) -> None:
    for name in ("作品甲", "作品乙"):
        _memory(
            tmp_path / name,
            work_id="123456789",
            source_url="https://kakuyomu.jp/works/123456789",
        )

    with pytest.raises(WorkDirectoryConflictError, match="多個資料夾"):
        resolve_work_directory(
            tmp_path,
            work_id="123456789",
            source_url="https://kakuyomu.jp/works/123456789",
            title="作品",
        )
