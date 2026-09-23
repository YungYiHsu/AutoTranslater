"""Manual chapter-range export followed by the official Send to Kindle page."""

from __future__ import annotations

import os
import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import StringVar, Toplevel, messagebox, ttk

from core.epub_export import (
    SEND_TO_KINDLE_URL,
    epub_destination,
    export_epub,
    local_chapter_numbers,
)
from core.work_setup import WorkSetupResult


class EpubExportDialog(Toplevel):
    def __init__(self, parent, setup: WorkSetupResult, *, initial_chapter: int = 1) -> None:
        super().__init__(parent)
        self.setup = setup
        self._busy = False
        self._results: queue.Queue[Path | Exception] = queue.Queue()
        self._progress_updates: queue.Queue[tuple[int, int]] = queue.Queue()
        self.output: Path | None = None
        self.title("匯出 EPUB／手動上傳 Kindle")
        self.geometry("620x520")
        self.minsize(540, 500)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self.local_numbers = local_chapter_numbers(setup)
        self.start = StringVar(value=str(self.local_numbers[0]) if self.local_numbers else "")
        self.end = StringVar(value=str(self.local_numbers[-1]) if self.local_numbers else "")
        range_text = (f"本地章節範圍：{self.local_numbers[0]}～{self.local_numbers[-1]}，包含起始與結束章節。"
                      if self.local_numbers else "尚無可匯出的本地章節")
        ttk.Label(body, text=setup.work.translated_title, wraplength=540).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.entries = []
        for row, (label, value) in enumerate((("起始章節：", self.start), ("結束章節：", self.end)), 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(body, textvariable=value, width=15)
            entry.grid(row=row, column=1, sticky="w", pady=5)
            self.entries.append(entry)
        ttk.Label(body, wraplength=540, justify="left", text=(
            f"{range_text}\n"
            "以本地 TXT 的章節名稱與正文合成 EPUB，包含手動修正內容。"
            "請先補齊範圍內各章 TXT 與正文。\n"
            "完成後開啟 Send to Kindle 網頁，登入 Amazon 並手動選取 EPUB 上傳。\n"
            f"輸出資料夾：{setup.work_directory.resolve()}"
        )).grid(row=3, column=0, columnspan=2, sticky="w", pady=12)
        self.status = StringVar(value="EPUB 會儲存在目前作品的資料夾。")
        ttk.Label(body, textvariable=self.status, wraplength=540, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=8)
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        self.export_button = ttk.Button(actions, text="匯出並開啟網頁", command=self._start)
        self.export_button.pack(side="left")
        if not self.local_numbers:
            self.export_button.configure(state="disabled")
        self.folder_button = ttk.Button(actions, text="開啟資料夾", command=self._open_folder, state="disabled")
        self.folder_button.pack(side="left", padx=8)
        self.web_button = ttk.Button(actions, text="開啟上傳網頁", command=self._open_web, state="disabled")
        self.web_button.pack(side="left")

    def _close(self) -> None:
        if not self._busy:
            self.destroy()

    def _open_folder(self) -> None:
        try:
            os.startfile(self.setup.work_directory.resolve())
        except OSError as exc:
            messagebox.showerror("無法開啟資料夾", str(exc), parent=self)

    def _open_web(self) -> None:
        try:
            if not webbrowser.open(SEND_TO_KINDLE_URL):
                raise OSError("瀏覽器未能開啟。")
        except (OSError, webbrowser.Error) as exc:
            messagebox.showwarning("EPUB 已儲存", f"{exc}\n請手動開啟：{SEND_TO_KINDLE_URL}", parent=self)

    def _start(self) -> None:
        if self._busy or not self.local_numbers:
            return
        values = [self.start.get().strip(), self.end.get().strip()]
        if any(not v.isascii() or not v.isdigit() or len(v) > 9 for v in values):
            messagebox.showerror("章節範圍錯誤", "請輸入正整數章節數字。", parent=self)
            return
        start, end = map(int, values)
        chapters = self.setup.work.source_work.chapters
        if not chapters[0].number <= start <= end <= chapters[-1].number:
            messagebox.showerror("章節範圍錯誤", "請確認章節存在，且起始章節小於或等於結束章節。", parent=self)
            return
        destination = epub_destination(self.setup, start, end)
        overwrite = destination.exists()
        if overwrite and not messagebox.askyesno("EPUB 已存在", f"是否覆蓋？\n{destination}", parent=self):
            return
        self._busy = True
        for entry in self.entries:
            entry.configure(state="disabled")
        for button in (self.export_button, self.folder_button, self.web_button):
            button.configure(state="disabled")
        self.status.set(f"正在匯出第 {start}～{end} 章……")
        self.progress.configure(value=0)

        def run() -> None:
            try:
                self._results.put(export_epub(
                    self.setup, start, end, overwrite=overwrite,
                    on_progress=lambda done, total: self._progress_updates.put((done, total)),
                ))
            except Exception as exc:  # noqa: BLE001 - report worker failures on the Tk thread
                self._results.put(exc)

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        while True:
            try:
                done, total = self._progress_updates.get_nowait()
            except queue.Empty:
                break
            self.progress.configure(value=done * 100 / total)
            self.status.set(f"已合成 {min(done, total - 1)}/{total - 1} 章"
                            + ("，正在儲存 EPUB……" if done == total - 1 else ""))
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self._busy = False
        self.export_button.configure(state="normal")
        for entry in self.entries:
            entry.configure(state="normal")
        if isinstance(result, Exception):
            self.status.set("匯出失敗，請修正後重試。")
            messagebox.showerror("EPUB 匯出失敗", str(result), parent=self)
            return
        self.output = result
        self.progress.configure(value=100)
        self.folder_button.configure(state="normal")
        self.web_button.configure(state="normal")
        self.status.set(f"已匯出：{result.name}\n位置：{result.parent}\n請在網頁手動選取此檔案上傳。")
        self._open_web()
