"""Application shell: web novels and ebooks are peer workspaces."""
import sys
from tkinter import ttk

from core.shared.paths import get_app_dir
from core.shared.prompt_templates import PromptTemplateStore
from ui.ebook.page import EbookPage
from ui.shared.prompt_page import PromptPage
from ui.webnovel.page import WebnovelPage


class DesktopApp:
    def __init__(self, root, *, app_directory=None):
        self.root = root
        self.app_directory = app_directory or get_app_dir()
        root.title("日文小說繁體中文翻譯器")
        root.geometry("850x1050")
        root.minsize(720, 700)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.prompt_store = PromptTemplateStore(self.app_directory)
        self.prompt_store.ensure()
        self.webnovel_page = WebnovelPage(
            self.notebook, app_directory=self.app_directory, prompt_store=self.prompt_store,
        )
        self.ebook_page = EbookPage(self.notebook, app_directory=self.app_directory,
                                   config_provider=lambda: self.webnovel_page.config)
        self.prompt_page = PromptPage(
            self.notebook, store=self.prompt_store,
            on_save=self.webnovel_page.on_prompts_saved,
        )
        for page, title in (
            (self.webnovel_page, "網路小說翻譯"),
            (self.ebook_page, "電子書"),
            (self.prompt_page, "Prompt 模板"),
        ):
            self.notebook.add(page, text=title)
        if not getattr(sys, "frozen", False):
            from developer.page import DeveloperPage
            self.developer_page = DeveloperPage(
                self.notebook, is_busy=lambda: self.webnovel_page.worker.is_running
                or self.webnovel_page.state not in {"idle", "work_ready", "chapter_ready"}
                or self.ebook_page.codex_page.busy
                or self.ebook_page.translation_page.busy
                or (self.ebook_page.ocr is not None and self.ebook_page.ocr.busy),
            )
            self.notebook.add(self.developer_page, text="開發者工具")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if not self.webnovel_page.can_close() or not self.ebook_page.can_close():
            return
        if not self.prompt_page.can_close():
            return
        self.ebook_page.shutdown()
        self.root.destroy()
