import json
from pathlib import Path

from core.local_works import discover_local_works


def write_memory(
    directory: Path,
    *,
    ncode: str = "n1234ab",
    source_title: str = "原文名稱",
    translated_title: str = "中文名稱",
) -> None:
    directory.mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "identity": {
            "ncode": ncode,
            "source_url": f"https://ncode.syosetu.com/{ncode}/",
        },
        "source": {"title": source_title},
        "translation": {"title": translated_title},
    }
    (directory / "work.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_discovers_and_sorts_valid_local_work_memories(tmp_path: Path) -> None:
    write_memory(tmp_path / "b", ncode="n2222bb", translated_title="B作品")
    write_memory(tmp_path / "a", ncode="n1111aa", translated_title="A作品")

    works = discover_local_works(tmp_path)

    assert [work.ncode for work in works] == ["n1111aa", "n2222bb"]
    assert works[0].label == "A作品｜原文名稱（N1111AA）"


def test_ignores_corrupt_and_non_work_files(tmp_path: Path) -> None:
    (tmp_path / "loose.txt").write_text("ignored", encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "work.json").write_text("not-json", encoding="utf-8")
    wrong_url = tmp_path / "wrong"
    write_memory(wrong_url)
    payload = json.loads((wrong_url / "work.json").read_text(encoding="utf-8"))
    payload["identity"]["source_url"] = "https://example.com/n1234ab/"
    (wrong_url / "work.json").write_text(json.dumps(payload), encoding="utf-8")

    assert discover_local_works(tmp_path) == ()


def test_duplicate_ncode_is_listed_once(tmp_path: Path) -> None:
    write_memory(tmp_path / "first")
    write_memory(tmp_path / "second")

    assert len(discover_local_works(tmp_path)) == 1
