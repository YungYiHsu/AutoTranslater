"""Ebook workspace: work selection, source recognition and chapter translation."""

from tkinter import messagebox, ttk

from ui.ebook.codex_page import CodexExtractPage
from ui.ebook.translation_page import EbookTranslationPage
from ui.ebook.work_page import EbookWorkPage


class EbookPage(ttk.Frame):
    def __init__(self, parent, *, app_directory, config_provider=None):
        super().__init__(parent)
        self.app_directory = app_directory
        self.ocr = None
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.work_page = EbookWorkPage(self.tabs, app_directory=app_directory,
                                      on_select=self.select_work)
        self.ocr_page = ttk.Frame(self.tabs)
        self.codex_page = CodexExtractPage(self.tabs, app_directory=app_directory,
                                          config_provider=config_provider)
        self.translation_page = EbookTranslationPage(
            self.tabs, app_directory=app_directory, config_provider=config_provider,
        )
        self.codex_page.external_busy = lambda: self.translation_page.busy or (self.ocr is not None and self.ocr.busy)
        self.translation_page.external_busy = lambda: self.codex_page.busy or (self.ocr is not None and self.ocr.busy)
        for page, title in (
            (self.work_page, "作品設定"),
            (self.ocr_page, "OCR"),
            (self.codex_page, "Codex 提取"),
            (self.translation_page, "翻譯"),
        ):
            self.tabs.add(page, text=title)
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, event=None):
        if self.tabs.select() == str(self.codex_page):
            self.codex_page.refresh()
            self.codex_page.auto_load_models()
        if (self.tabs.select() == str(self.ocr_page) and self.ocr is not None
                and not self.ocr.busy and self.ocr.folder is not None
                and not self.ocr.folder.exists()):
            self.ocr._records_cleared((0, []))
        if self.tabs.select() == str(self.translation_page):
            self.translation_page.refresh()
            self.translation_page.refresh_models()
        if self.tabs.select() != str(self.ocr_page) or self.ocr is not None:
            return
        try:
            from core.ebook.ocr.recognizer_process import ProcessRecognizer
            from ui.ebook.ocr_page import EbookOcrPanel

            self.ocr = EbookOcrPanel(
                self.ocr_page,
                engine=ProcessRecognizer(self.app_directory),
                require_work=True,
            )
            self.ocr.external_busy = lambda: self.codex_page.busy or self.translation_page.busy
            if self.work_page.current_work:
                work = self.work_page.current_work
                self.ocr.bind_work(work, self.work_page.store.work_directory(work))
        except Exception as exc:  # noqa: BLE001 - show recognition failures inside this mode.
            for widget in self.ocr_page.winfo_children():
                widget.destroy()
            self.tabs.select(self.work_page)
            messagebox.showerror("無法載入 OCR", str(exc), parent=self)

    def select_work(self, work, directory):
        if self.work_page.current_work and self.work_page.current_work["id"] == work["id"]:
            return True
        if not self.codex_page.can_leave() or not self.translation_page.can_leave():
            return False
        if self.ocr is not None and not self.ocr.bind_work(work, directory):
            return False
        self.translation_page.load_work(directory)
        self.codex_page.bind_work(work, directory)
        return True

    def can_close(self):
        return (self.codex_page.can_leave() and (self.ocr is None or self.ocr.can_close())
                and self.translation_page.can_leave())

    def shutdown(self):
        self.translation_page.shutdown()
        self.codex_page.shutdown()
        if self.ocr is not None:
            self.ocr.shutdown()
