"""Tests for static and incremental translated-chapter navigation."""

from __future__ import annotations

from pathlib import Path

from formatters.html_navigation import HtmlNavigationManager


def write_old_html(path: Path, body: str) -> None:
    path.write_text(
        f"<html><body><article class=\"novel-text\">{body}</article><footer>x</footer></body></html>",
        encoding="utf-8",
    )


def test_refresh_all_inserts_non_contiguous_links_and_preserves_body(tmp_path: Path) -> None:
    chapters = {number: tmp_path / f"{number:04d} - 小說.html" for number in (1, 3, 8)}
    for number, path in chapters.items():
        write_old_html(path, f"人工修訂正文 {number}")

    changed = HtmlNavigationManager().refresh_all(chapters)

    assert changed == 3
    first = chapters[1].read_text(encoding="utf-8")
    middle = chapters[3].read_text(encoding="utf-8")
    last = chapters[8].read_text(encoding="utf-8")
    assert "人工修訂正文 1" in first
    assert "沒有上一章" in first
    assert 'href="0003%20-%20%E5%B0%8F%E8%AA%AA.html"' in first
    assert "← 第 1 章" in middle
    assert "第 8 章 →" in middle
    assert "沒有下一章" in last
    assert middle.count("AUTOTRANSLATER_NAV_START") == 1


def test_repeated_refresh_only_replaces_the_marked_navigation_block(tmp_path: Path) -> None:
    chapters = {number: tmp_path / f"{number}.html" for number in (1, 2)}
    for number, path in chapters.items():
        write_old_html(path, f"正文 {number}")
    manager = HtmlNavigationManager()
    manager.refresh_all(chapters)
    first_content = chapters[1].read_text(encoding="utf-8")

    changed = manager.refresh_all(chapters)

    assert changed == 0
    assert chapters[1].read_text(encoding="utf-8") == first_content


def test_malformed_old_html_is_skipped_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "1.html"
    path.write_text("人工保留內容", encoding="utf-8")

    changed = HtmlNavigationManager().refresh_all({1: path})

    assert changed == 0
    assert path.read_text(encoding="utf-8") == "人工保留內容"


class RecordingNavigationManager(HtmlNavigationManager):
    def __init__(self) -> None:
        super().__init__()
        self.updated: list[int] = []

    def _update(
        self,
        chapters: dict[int, Path],
        number: int,
        previous: int | None,
        following: int | None,
    ) -> int:
        self.updated.append(number)
        return 0


def test_incremental_refresh_touches_only_new_chapter_and_neighbors(tmp_path: Path) -> None:
    manager = RecordingNavigationManager()
    chapters = {number: tmp_path / f"{number}.html" for number in (1, 3, 5, 7, 9)}

    manager.refresh_neighbors(chapters, 5)

    assert set(manager.updated) == {3, 5, 7}
    assert len(manager.updated) == 3
