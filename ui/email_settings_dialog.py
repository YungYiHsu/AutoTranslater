"""Gmail configuration and authentication-only connection test."""

from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import StringVar, Toplevel, messagebox, ttk

from core.email_settings import (
    EmailCredentials,
    EmailSettings,
    EmailSettingsError,
    load_email_settings,
    normalize_password,
    save_email_settings,
    validate_address,
)
from core.kindle_email import KindleEmailError, test_email_connection


class EmailSettingsDialog(Toplevel):
    def __init__(self, parent, settings_path: Path):
        # Fail before creating a partial dialog on malformed configuration.
        settings = load_email_settings(settings_path)
        super().__init__(parent)
        self.settings_path = settings_path
        self.credentials = EmailCredentials()
        self.busy = False
        self.results = queue.Queue()
        self.title("Kindle Email 設定（Gmail）")
        self.geometry("620x470")
        self.minsize(580, 450)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self.sender = StringVar(value=settings.sender)
        self.recipient = StringVar(value=settings.recipient)
        self.password = StringVar()
        self.status = StringVar(value="密碼欄留白時沿用此寄件帳號已儲存的應用程式密碼。")
        self.controls = []
        for row, (label, var) in enumerate((("寄件 Gmail：", self.sender),
                                           ("Kindle 收件地址：", self.recipient),
                                           ("應用程式密碼：", self.password))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(body, textvariable=var, show="*" if row == 2 else "")
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            self.controls.append(entry)
        ttk.Label(body, text=(
            "1. Google 帳號先啟用兩步驟驗證，再建立 16 位應用程式密碼。\n"
            "2. Amazon 個人文件設定中，將寄件 Gmail 加入核准寄件人清單。\n"
            "3. 收件地址填 Amazon 提供的 Kindle 專用信箱，不是登入信箱。\n\n"
            "密碼僅存於 Windows 認證管理員。測試連線會連至 Gmail 認證，"
            "但不寄信、不檢查收件匣。Email 附件上限：18 MB。"
        ), wraplength=550, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Label(body, textvariable=self.status, wraplength=550, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=8)
        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        for text, command in (("儲存設定／更新密碼", self._save),
                              ("測試連線", self._test), ("移除已儲存密碼", self._delete)):
            button = ttk.Button(actions, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)
        ttk.Button(body, text="開啟 Google 應用程式密碼設定", command=lambda: webbrowser.open(
            "https://myaccount.google.com/apppasswords")).grid(row=6, column=0, columnspan=2, sticky="w")

    def _settings(self):
        return EmailSettings(self.sender.get(), self.recipient.get()).validated()

    def _save(self):
        if self.busy:
            return
        try:
            settings = self._settings()
            secret = self.password.get()
            if secret.strip():
                self.credentials.set(settings.sender, secret)
                self.password.set("")
            save_email_settings(self.settings_path, settings)
            self.status.set("設定已儲存；密碼留白時未變更既有密碼。")
        except EmailSettingsError as exc:
            messagebox.showerror("設定未完成", str(exc), parent=self)

    def _delete(self):
        if self.busy:
            return
        try:
            sender = validate_address(self.sender.get(), sender=True)
            if not messagebox.askyesno("移除密碼", f"移除此程式儲存的 {sender} 應用程式密碼？\n"
                                      "不會刪除 Google 帳號或郵件。", parent=self):
                return
            self.credentials.delete(sender)
            self.password.set("")
            self.status.set("已移除本程式儲存的密碼；需要時可至 Google 撤銷該應用程式密碼。")
        except EmailSettingsError as exc:
            messagebox.showerror("移除失敗", str(exc), parent=self)

    def _test(self):
        if self.busy:
            return
        try:
            settings = self._settings()
            entered = self.password.get()
            password = normalize_password(entered) if entered.strip() else self.credentials.get(settings.sender)
            if not password:
                raise EmailSettingsError("請先輸入或儲存此帳號的應用程式密碼。")
        except EmailSettingsError as exc:
            messagebox.showerror("無法測試", str(exc), parent=self)
            return
        self.busy = True
        for control in self.controls:
            control.configure(state="disabled")
        self.status.set("正在測試 Gmail 安全連線與認證……")

        def run():
            try:
                result = test_email_connection(settings, password)
            except (EmailSettingsError, KindleEmailError) as exc:
                result = exc
            except Exception:  # noqa: BLE001 - return a sanitized failure to the Tk thread
                result = KindleEmailError("測試失敗，原因未知。")
            self.results.put(result)

        threading.Thread(target=run, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.busy = False
        for control in self.controls:
            control.configure(state="normal")
        self.status.set(str(result))

    def _close(self):
        if not self.busy:
            self.password.set("")
            self.destroy()
            if self.master.winfo_exists():
                self.master.grab_set()
