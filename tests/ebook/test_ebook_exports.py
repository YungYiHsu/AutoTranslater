from tests.ebook.ui_helpers import empty_detection, wait_idle

"""Synthetic work output tests: no private images/text and no network."""

import json
import tkinter as tk
from unittest.mock import patch

import pytest
from PIL import Image

from core.ebook.chapters import export_chapter, save_translation
from core.ebook.exports import page_stem, save_page_image
from core.ebook.ocr.session import (
    load_session,
)
from core.ebook.works import EbookWorkStore
from extractors.bookwalker import parse_bookwalker
from tests.ebook.test_ebook_works import HTML, URL
from ui.ebook.ocr_page import EbookOcrPanel
from ui.ebook.page import EbookPage


def make_work(tmp_path):
    store = EbookWorkStore(tmp_path)
    work, _ = store.create(parse_bookwalker(HTML, URL))
    return work, store.work_directory(work)


def test_page_names_and_conflicts(tmp_path):
    assert page_stem(10, 11) == "p010-p011"
    assert page_stem(9) == "p009"
    assert page_stem(1000) == "p1000"
    with pytest.raises(ValueError):
        page_stem(10, 9)
    assert page_stem(0, 0) == "p000"
    assert page_stem(10, 12) == "p010-p012"
    image = Image.new("RGB", (200, 100), "white")
    first = save_page_image(tmp_path, image, 10, 11)
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        save_page_image(tmp_path, image, 10, 11)
    second = save_page_image(tmp_path, image, 10, 11, overwrite=True)
    assert second.name == "p010-p011.png"
    assert first.read_bytes() == original
    with Image.open(second) as saved:
        assert saved.size == (200, 100)


def sample_record(directory, right=10):
    return {
        "work_capture": {"work_id": directory.name, "right": right, "left": right+1},
        "columns": [
            {"id": "right_01_01", "status": "done", "raw": "A", "corrected": "a"},
            {"id": "left_01_01", "status": "done", "raw": "B"},
        ],
    }


def test_chapter_order_ranges_and_translation(tmp_path):
    _, directory = make_work(tmp_path)
    later = sample_record(directory, 12)
    later["columns"][0]["corrected"] = "c"
    later["columns"][1]["raw"] = "D"
    export_chapter(directory, later, "later", 1, 1, 2)
    earlier = sample_record(directory)
    output = export_chapter(directory, earlier, "earlier", 1, 1, 1)
    assert output.name == "001.txt"
    assert output.read_text(encoding="utf-8") == "acD\n"
    next_chapter = export_chapter(directory, earlier, "earlier", 2, 2, 2)
    assert next_chapter.read_text(encoding="utf-8") == "B\n"
    translation = save_translation(directory, 1, "合成譯文")
    assert translation.name == output.name
    earlier["columns"][0]["corrected"] = "changed"
    export_chapter(directory, earlier, "earlier", 1, 1, 1, replace=True)
    chapters = json.loads((directory / "chapters.json").read_text(encoding="utf-8"))
    assert chapters["1"]["source_sha256"] != chapters["1"]["translation_source_sha256"]
    assert translation.read_text(encoding="utf-8") == "合成譯文\n"
    with pytest.raises(FileExistsError):
        export_chapter(directory, earlier, "new-version", 1, 1, 1)


def test_incomplete_and_foreign_records_rejected(tmp_path):
    _, directory = make_work(tmp_path)
    data = sample_record(directory)
    data["columns"][0]["status"] = "pending"
    with pytest.raises(ValueError):
        export_chapter(directory, data, "test", 1, 1, 2)
    data["work_capture"]["work_id"] = "other"
    with pytest.raises(ValueError):
        export_chapter(directory, data, "test", 1, 1, 2)
    assert not (directory / "chapters.json").exists()


def test_work_binding_page_capture_reopen_and_switch(tmp_path):
    work, directory = make_work(tmp_path)
    root = tk.Tk()
    root.withdraw()
    panel = EbookOcrPanel(root, require_work=True)
    try:
        assert panel.buttons["全螢幕截圖"].instate(["disabled"])
        assert panel.bind_work(work, directory)
        with patch("ui.ebook.ocr_page.detect_lines", side_effect=empty_detection):
            panel.accept_image(Image.new("RGB", (1920,1200), "white"), "test")
            wait_idle(panel)
        assert panel.folder.parent == directory / "ocr"
        assert not (directory / "images").exists()
        assert panel.page_suggestion.get() == "沒有"
        _, data, image = load_session(panel.folder / "manifest.json", session_root=directory / "ocr")
        assert data["work_capture"]["work_id"] == work["id"]
        assert image.size == (1920, 1200)
        with pytest.raises(ValueError):
            load_session(panel.folder / "manifest.json", session_root=tmp_path / "other")
        panel.set_busy(True)
        with patch("ui.ebook.ocr_page.messagebox.showwarning"):
            assert not panel.bind_work(dict(work, id="other"), tmp_path)
        assert panel.work == work
    finally:
        root.destroy()

def test_work_folder_button_and_translation_page(tmp_path):
    _, directory = make_work(tmp_path)
    export_chapter(directory, sample_record(directory), "test", 1, 1, 2)
    root = tk.Tk()
    root.withdraw()
    page = EbookPage(root, app_directory=tmp_path)
    try:
        assert page.work_page.open_button.instate(["disabled"])
        page.work_page.existing_combo.current(0)
        page.work_page.select_work()
        with patch("ui.ebook.work_page.os.startfile") as launch:
            page.work_page.open_folder()
            launch.assert_called_once_with(str(directory))
        translated = page.translation_page
        translated.combo.current(0)
        translated.choose()
        assert translated.original.get("1.0", "end-1c") == "aB"
        assert translated.original.cget("state") == "disabled"
        assert not hasattr(translated, "editor")
        assert not (directory / "translations" / "001.txt").exists()
    finally:
        page.shutdown()
        root.destroy()
