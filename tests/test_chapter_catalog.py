import json
from pathlib import Path

import pytest

from core.chapter_catalog import ChapterCatalog, ChapterCatalogStore
from core.exceptions import ExtractorError, WorkDirectoryConflictError
from core.models import NovelChapterEntry


def make_catalog() -> ChapterCatalog:
    return ChapterCatalog(
        "n1234ab",
        "https://ncode.syosetu.com/n1234ab/",
        2,
        (
            NovelChapterEntry(1, "第一章", "https://ncode.syosetu.com/n1234ab/1/"),
            NovelChapterEntry(3, "第三章", "https://ncode.syosetu.com/n1234ab/3/"),
        ),
    )


def test_catalog_round_trip_uses_one_fixed_file(tmp_path: Path) -> None:
    store = ChapterCatalogStore()
    store.save(tmp_path, make_catalog())

    assert store.load(tmp_path, "n1234ab") == make_catalog()
    assert [path.name for path in tmp_path.iterdir()] == ["chapters.json"]


def test_corrupt_catalog_requires_full_refresh(tmp_path: Path) -> None:
    (tmp_path / "chapters.json").write_text("broken", encoding="utf-8")

    with pytest.raises(ExtractorError, match="重新整理完整目錄"):
        ChapterCatalogStore().load(tmp_path, "n1234ab")


def test_catalog_for_another_work_is_not_overwritten(tmp_path: Path) -> None:
    store = ChapterCatalogStore()
    store.save(tmp_path, make_catalog())

    with pytest.raises(WorkDirectoryConflictError, match="另一部作品"):
        store.load(tmp_path, "n9999zz")


def test_existing_work_memory_owner_is_checked_before_cache_save(tmp_path: Path) -> None:
    (tmp_path / "work.json").write_text(
        json.dumps({"identity": {"ncode": "n9999zz"}}), encoding="utf-8"
    )

    with pytest.raises(WorkDirectoryConflictError, match="另一部作品"):
        ChapterCatalogStore().save(tmp_path, make_catalog())
