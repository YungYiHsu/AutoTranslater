"""Chapter-range EPUB export with manual or user-confirmed browser upload."""

from __future__ import annotations

import os
import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import StringVar, Toplevel, messagebox, ttk

from core.email_settings import EmailCredentials, EmailSettingsError, load_email_settings
from core.epub_export import (
    SEND_TO_KINDLE_URL,
    epub_destination,
    export_epub,
    local_chapter_numbers,
)
from core.kindle_email import (
    EmailCancelled,
    EmailHistory,
    KindleEmailError,
    prepare_email,
    send_email,
)
from core.kindle_upload import UploadCancelled, upload_epub
from core.paths import get_app_dir
from core.work_setup import WorkSetupResult
from ui.email_settings_dialog import EmailSettingsDialog


class EpubExportDialog(Toplevel):
    def __init__(self, parent, setup: WorkSetupResult, *,
                 app_directory: Path | None = None) -> None:
        super().__init__(parent)
        self.setup = setup
        self.app_directory = app_directory or get_app_dir()
        self._busy = False
        self._results: queue.Queue[Path | Exception] = queue.Queue()
        self._progress_updates: queue.Queue[tuple[int, int]] = queue.Queue()
        self.output: Path | None = None
        self._exported_range: tuple[int, int] | None = None
        self._uploading = False
        self._emailing = False
        self._upload_events = queue.Queue()
        self._upload_cancel = threading.Event()
        self._upload_answer = queue.Queue()
        self._uploaded_paths: set[Path] = set()
        self.title("匯出 EPUB／上傳 Kindle")
        self.geometry("620x650")
        self.minsize(540, 630)
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
            "完成後可手動上傳，或按「自動上傳至 Kindle」。\n"
            "自動上傳每次開啟全新的 Chrome／Edge 視窗，需重新登入，不保留登入狀態。\n"
            f"輸出資料夾：{setup.work_directory.resolve()}"
        )).grid(row=3, column=0, columnspan=2, sticky="w", pady=12)
        self.status = StringVar(value="EPUB 會儲存在目前作品的資料夾。")
        ttk.Label(body, textvariable=self.status, wraplength=540, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=8)
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        self.export_button = ttk.Button(actions, text="匯出EPUB", command=self._start)
        self.export_button.pack(side="left")
        if not self.local_numbers:
            self.export_button.configure(state="disabled")
        self.folder_button = ttk.Button(actions, text="開啟資料夾", command=self._open_folder, state="disabled")
        self.folder_button.pack(side="left", padx=8)
        self.web_button = ttk.Button(actions, text="開啟上傳網頁", command=self._open_web, state="disabled")
        self.web_button.pack(side="left")
        upload_actions = ttk.Frame(body)
        upload_actions.grid(row=7, column=0, columnspan=2, sticky="w", pady=8)
        self.upload_button = ttk.Button(upload_actions, text="自動上傳至 Kindle",
                                        command=self._start_upload, state="disabled")
        self.upload_button.pack(side="left")
        self.cancel_upload_button = ttk.Button(upload_actions, text="停止自動上傳",
                                               command=self._cancel_upload, state="disabled")
        self.cancel_upload_button.pack(side="left", padx=8)
        email_actions = ttk.Frame(body)
        email_actions.grid(row=8, column=0, columnspan=2, sticky="w", pady=8)
        self.email_settings_button = ttk.Button(email_actions, text="Email 設定",
                                                command=self._open_email_settings)
        self.email_settings_button.pack(side="left")
        self.email_button = ttk.Button(email_actions, text="Email 寄送至 Kindle",
                                       command=self._start_email, state="disabled")
        self.email_button.pack(side="left", padx=8)

    def _transfer_buttons(self):
        return (self.export_button, self.folder_button, self.web_button, self.upload_button,
                self.email_button, self.email_settings_button)

    def _open_email_settings(self):
        if self._busy:
            return
        try:
            EmailSettingsDialog(self, self.app_directory / "kindle_email.json")
        except EmailSettingsError as exc:
            messagebox.showerror("Email 設定錯誤", str(exc), parent=self)

    def _start_email(self):
        if self._busy or self.output is None or self._exported_range is None:
            return
        try:
            settings = load_email_settings(self.app_directory / "kindle_email.json").validated()
        except EmailSettingsError as exc:
            messagebox.showerror("請先完成 Email 設定", str(exc), parent=self)
            return
        path, (start, end) = self.output, self._exported_range
        title = self.setup.work.translated_title

        def worker(confirm):
            email = prepare_email(path, settings, title, start, end)
            history = EmailHistory(self.app_directory / "logs" / "kindle_email_history.json")
            previous = history.previous(email)
            repeat = (f"此檔案已有寄送紀錄（{previous['status']}，{previous.get('time', '')}）。\n"
                      "請先查看寄件信箱與 Kindle 書庫，確認是否需要重寄。\n\n") if previous else ""
            if not confirm(f"{repeat}作品：{email.title}\n章節：第 {start}～{end} 章\n"
                           f"檔案：{email.filename}\n大小：{email.size / 1_000_000:.2f} MB\n"
                           f"寄件人：{settings.sender}\n收件人：{settings.recipient}\n\n"
                           "請確認已將寄件人加入 Amazon 核准清單。\n是否將此 EPUB 附件寄送至 Kindle？"):
                raise EmailCancelled("已取消寄送，EPUB 已保留。")
            password = EmailCredentials().get(settings.sender)
            if not password:
                raise EmailSettingsError("找不到此 Gmail 的應用程式密碼，請先在 Email 設定中儲存。")
            return send_email(email, password, history, cancel=self._upload_cancel,
                              status=lambda text: self._upload_events.put(("status", text)),
                              expected_previous_id=(previous or {}).get("message_id"))

        self._begin_transfer(worker, email=True)

    def _close(self) -> None:
        if self._uploading:
            self._cancel_upload()
            return
        if not self._busy:
            self.destroy()

    def _cancel_upload(self) -> None:
        self._upload_cancel.set()
        self.cancel_upload_button.configure(state="disabled")
        self.status.set("正在停止操作；若已送出，無法保證撤回，請查詢信箱／Kindle 書庫。")

    def _start_upload(self) -> None:
        if self._busy or self.output is None:
            return
        path = self.output
        if not path.is_file():
            messagebox.showerror("找不到 EPUB", "檔案已移動或刪除，請重新匯出。", parent=self)
            return
        start, end = self._exported_range
        repeat = "此檔案在本視窗已嘗試上傳，請先確認書庫是否已有此書。\n\n" if path in self._uploaded_paths else ""
        if not messagebox.askyesno("自動上傳至 Kindle", (
            f"{repeat}作品：{self.setup.work.translated_title}\n"
            f"章節：第 {start}～{end} 章\n檔案：{path.name}\n\n"
            "將選取此 EPUB 上傳至 Amazon Send to Kindle。\n"
            "每次需自行登入，不沿用或保留登入狀態；送出前會再次確認。\n是否繼續？"
        ), parent=self):
            return
        self._uploaded_paths.add(path)

        def worker(confirm):
            return upload_epub(path, status=lambda text: self._upload_events.put(("status", text)),
                               confirm=confirm, cancel=self._upload_cancel)

        self._begin_transfer(worker)

    def _begin_transfer(self, worker, *, email=False):
        self._busy = self._uploading = True
        self._emailing = email
        self._upload_cancel = threading.Event()
        self._upload_answer = queue.Queue()
        for entry in self.entries:
            entry.configure(state="disabled")
        for button in self._transfer_buttons():
            button.configure(state="disabled")
        self.cancel_upload_button.configure(state="normal")
        self.cancel_upload_button.configure(text="停止寄送" if email else "停止自動上傳")
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)
        self.status.set("正在準備 EPUB 郵件……" if email else "正在啟動上傳瀏覽器……")

        def confirm(message):
            self._upload_events.put(("confirm", message))
            # Bounded wait, cancellation does not require the Tk thread to join us.
            for _ in range(3000):
                if self._upload_cancel.is_set():
                    return False
                try:
                    return self._upload_answer.get(timeout=0.2)
                except queue.Empty:
                    continue
            return False

        def run():
            try:
                result = worker(confirm)
            except Exception as exc:  # noqa: BLE001 - worker exceptions must reach Tk
                result = (exc if not email or isinstance(exc, (EmailSettingsError, KindleEmailError))
                          else KindleEmailError("Email 操作失敗，原因未知；請先查證信箱與書庫再重寄。"))
            self._upload_events.put(("done", result))

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll_upload)

    def _poll_upload(self) -> None:
        while True:
            try:
                kind, value = self._upload_events.get_nowait()
            except queue.Empty:
                self.after(100, self._poll_upload)
                return
            if kind == "status":
                self.status.set(value)
            elif kind == "confirm":
                answer = (not self._upload_cancel.is_set()
                          and self._confirm_upload(value))
                self._upload_answer.put(answer)
            elif kind == "done":
                self._busy = self._uploading = False
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0 if isinstance(value, Exception) else 100)
                self.cancel_upload_button.configure(state="disabled")
                for entry in self.entries:
                    entry.configure(state="normal")
                for button in self._transfer_buttons():
                    button.configure(state="normal")
                self.status.set(str(value))
                if isinstance(value, Exception) and not isinstance(value, (UploadCancelled, EmailCancelled)):
                    messagebox.showwarning("Email 寄送未完成" if self._emailing else "自動上傳未完成",
                                           f"{value}\n\nEPUB 已保留，可使用下方按鈕手動上傳。", parent=self)
                elif not isinstance(value, Exception):
                    messagebox.showinfo("Email 寄送結果" if self._emailing else "Kindle 上傳結果", value, parent=self)
                return

    def _open_folder(self) -> None:
        try:
            os.startfile(self.setup.work_directory.resolve())
        except OSError as exc:
            messagebox.showerror("無法開啟資料夾", str(exc), parent=self)

    def _confirm_upload(self, message: str) -> bool:
        """Bring the native confirmation above the browser, then restore stacking."""
        previous = self.attributes("-topmost")
        try:
            self.deiconify()
            self.attributes("-topmost", True)
            self.lift()
            self.focus_force()
            return messagebox.askyesno("確認寄送 EPUB" if self._emailing else "確認送出 EPUB", message, parent=self)
        finally:
            self.attributes("-topmost", previous)

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
        self.output = None
        self._exported_range = (start, end)
        for button in self._transfer_buttons():
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
        self.email_settings_button.configure(state="normal")
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
        self.upload_button.configure(state="normal")
        self.email_button.configure(state="normal")
        self.status.set(f"已匯出：{result.name}\n位置：{result.parent}\n可按「自動上傳至 Kindle」，或在網頁手動上傳。")
