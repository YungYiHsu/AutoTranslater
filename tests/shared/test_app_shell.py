"""Peer-mode GUI ownership and storage roots; synthetic data only."""
import tkinter as tk
from unittest.mock import patch

from core.ebook.works import EbookWorkStore
from core.shared.config import AppConfig, write_config
from ui.app import DesktopApp


def test_peer_modes_and_shared_prompts(tmp_path):
    root = tk.Tk()
    root.withdraw()
    app = DesktopApp(root, app_directory=tmp_path)
    try:
        assert [app.notebook.tab(t, "text") for t in app.notebook.tabs()][:3] == [
            "網路小說翻譯", "電子書", "Prompt 模板",
        ]
        assert app.webnovel_page.master is app.ebook_page.master is app.notebook
        assert not hasattr(app.webnovel_page, 'ebook_page')
        assert app.prompt_page.prompt_store is app.webnovel_page.prompt_store
        assert app.webnovel_page.output_root_directory == tmp_path / 'outputs/webnovel'
        assert app.ebook_page.work_page.store.directory == tmp_path / 'outputs/ebook/bookwalker'
        app.ebook_page.tabs.select(app.ebook_page.ocr_page)
        root.update()
        assert app.ebook_page.ocr is not None
        assert '匯入舊紀錄' not in app.ebook_page.ocr.buttons
        assert '清空 OCR 紀錄' in app.ebook_page.ocr.buttons
        with patch.object(app.webnovel_page, 'can_close', return_value=False):
            app._on_close()
        assert root.winfo_exists()
        with patch.object(app.ebook_page, 'can_close', return_value=False):
            app._on_close()
        assert root.winfo_exists()
        with patch.object(app.prompt_page, 'can_close', return_value=False):
            app._on_close()
        assert root.winfo_exists()
    finally:
        app.ebook_page.shutdown()
        root.destroy()


def test_ebook_respects_shared_custom_output_root(tmp_path):
    write_config(AppConfig(output_directory='custom-output'), tmp_path / 'config.json')
    assert EbookWorkStore(tmp_path).directory == tmp_path / 'custom-output/ebook/bookwalker'
