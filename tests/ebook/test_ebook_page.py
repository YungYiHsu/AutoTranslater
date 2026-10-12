"""Embedded OCR layout and main-window ownership checks (no screenshot/model)."""

import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ui.ebook.page import EbookPage


def test_embedded_ocr_tabs_and_close():
    root = tk.Tk()
    root.withdraw()
    root.title("Main GUI")
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    protocol = root.protocol("WM_DELETE_WINDOW")
    page = EbookPage(root, app_directory=Path.cwd())
    page.pack(fill="both", expand=True)
    try:
        assert [page.tabs.tab(t, "text") for t in page.tabs.tabs()] == [
            "作品設定", "OCR", "Codex 提取", "翻譯"
        ]
        assert str(page.work_page.existing_combo.cget("state")) == "readonly"
        assert page.translation_page.action_buttons["翻譯／續傳"].instate(["disabled"])
        assert page.ocr is None
        page.tabs.select(page.ocr_page)
        root.update()
        assert page.ocr is not None
        assert root.title() == "Main GUI"
        assert root.protocol("WM_DELETE_WINDOW") == protocol
        assert page.ocr.window is root
        assert [page.ocr.notebook.tab(t, "text") for t in page.ocr.notebook.tabs()] == [
            "預覽", "辨識與校訂", "版面設定"
        ]
        page.ocr.image = Image.new("RGB", (1920, 1200), "white")
        with patch.object(page.translation_page, "refresh_models") as connect:
            page.tabs.select(page.translation_page)
            root.update()
            assert connect.called
            page.tabs.select(page.ocr_page)
            root.update()
        assert page.ocr.image.size == (1920, 1200)
        page.ocr.busy = True
        with patch("ui.ebook.ocr_page.messagebox.askyesno", return_value=True):
            assert not page.can_close()
        assert page.ocr.cancel.is_set()
        page.ocr.busy = False
        page.ocr.dirty = True
        with patch("ui.ebook.ocr_page.messagebox.askyesnocancel", return_value=None):
            assert not page.can_close()
        page.ocr.dirty = False
        assert page.can_close()
    finally:
        page.shutdown()
        root.destroy()
