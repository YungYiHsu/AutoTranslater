"""Per-work proper-noun memory tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.exceptions import TermMemoryError
from core.term_memory import TermMemoryStore


def test_ensure_creates_simple_empty_json_and_loads_it(tmp_path: Path) -> None:
    store = TermMemoryStore()
    path = store.ensure(tmp_path)
    assert path.name == "terms.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    assert store.load(tmp_path) == {}


def test_match_normalizes_unicode_and_keeps_overlapping_terms(tmp_path: Path) -> None:
    store = TermMemoryStore()
    terms = {
        "アリス": "愛麗絲",
        "王国": "王國",
        "シンフォニア王国": "辛弗尼亞王國",
        "ＡＢＣ": "ABC",
        "未登場": "未登場",
    }
    matched = store.match("アリス様はシンフォニア王国とABCを見た。", terms)
    assert list(matched) == ["シンフォニア王国", "アリス", "ＡＢＣ", "王国"]


def test_corrupt_memory_is_rejected_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(TermMemoryError, match="損壞"):
        TermMemoryStore().load(tmp_path)
    assert path.read_text(encoding="utf-8") == "not-json"


def test_update_validates_text_and_keeps_existing_translation(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps({"アリス": "愛麗絲", "手動新增": "手動譯名"}, ensure_ascii=False),
        encoding="utf-8",
    )
    update = TermMemoryStore().update(
        tmp_path,
        {
            "アリス": "艾莉絲",
            "クロム": "克羅姆",
            "不存在": "不存在譯名",
            "123": "123",
        },
        source_text="アリス與クロム。",
        translated_text="愛麗絲又被寫成艾莉絲，並與克羅姆同行。",
    )
    assert update.added == {"クロム": "克羅姆"}
    assert update.conflicts == 1
    assert update.rejected == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["アリス"] == "愛麗絲"
    assert saved["手動新增"] == "手動譯名"
    assert saved["クロム"] == "克羅姆"


def test_merge_candidates_builds_temporary_memory_without_writing_file(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text('{"アリス":"愛麗絲"}', encoding="utf-8")

    merged, update = TermMemoryStore().merge_candidates(
        {"アリス": "愛麗絲"},
        {"クロム": "克羅姆", "アリス": "艾莉絲"},
        source_text="アリス與クロム。",
        translated_text="愛麗絲又被寫成艾莉絲，並與克羅姆同行。",
    )

    assert merged == {"アリス": "愛麗絲", "クロム": "克羅姆"}
    assert update.added == {"クロム": "克羅姆"}
    assert update.conflicts == 1
    assert path.read_text(encoding="utf-8") == '{"アリス":"愛麗絲"}'
