"""Existing-work selection and BOOK WALKER metadata creation UI."""

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core.ebook.works import EbookWorkStore
from extractors.bookwalker import fetch_bookwalker, normalize_bookwalker_url


class EbookWorkPage(ttk.Frame):
    def __init__(self, parent, *, app_directory, on_select=None):
        super().__init__(parent, padding=12)
        self.store = EbookWorkStore(app_directory)
        self.current_work = None
        self.on_select = on_select
        self.loaded_url = None
        self.events = queue.Queue()
        self.loading = False
        self.works = []
        self.selected = tk.StringVar(self)
        self.url = tk.StringVar(self)
        self.provider = tk.StringVar(self, value="bookwalker")
        self.summary = tk.StringVar(self)
        self.status = tk.StringVar(self)
        existing = ttk.LabelFrame(self, text="選現有作品", padding=10)
        existing.pack(fill="x", pady=(0, 12))
        self.existing_combo = ttk.Combobox(existing, textvariable=self.selected, state="readonly")
        self.existing_combo.pack(fill="x")
        self.existing_combo.bind("<<ComboboxSelected>>", self.select_work)
        self.open_button = ttk.Button(existing, text="開啟作品資料夾", command=self.open_folder,
                                      state="disabled")
        self.open_button.pack(anchor="w", pady=(8, 0))
        ttk.Label(existing, textvariable=self.summary, wraplength=620).pack(anchor="w", pady=(8, 0))
        new = ttk.LabelFrame(self, text="新增作品", padding=10)
        new.pack(fill="x")
        ttk.Label(new, text="電子書網站／品牌").pack(anchor="w")
        ttk.Radiobutton(new, text="BOOK☆WALKER 日本", variable=self.provider,
                        value="bookwalker").pack(anchor="w", pady=(4, 10))
        ttk.Label(new, text="單本商品網址").pack(anchor="w")
        self.url_entry = ttk.Entry(new, textvariable=self.url)
        self.url_entry.pack(fill="x", pady=4)
        self.fetch_button = ttk.Button(new, text="讀取作品資料", command=self.fetch)
        self.fetch_button.pack(anchor="w", pady=(0, 8))
        form = ttk.Frame(new)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.fields = {}
        for row, (key, label) in enumerate((
            ("title", "日文書名"), ("author", "作者"), ("illustrator", "插畫"),
            ("publisher", "出版社"), ("label", "文庫／品牌"), ("series", "系列"),
        )):
            value = tk.StringVar(self)
            self.fields[key] = value
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            ttk.Entry(form, textvariable=value).grid(row=row, column=1, sticky="ew", pady=4)
        self.create_button = ttk.Button(new, text="建立作品", command=self.create, state="disabled")
        self.create_button.pack(anchor="w", pady=(10, 0))
        ttk.Label(self, textvariable=self.status, wraplength=620).pack(anchor="w", pady=8)
        self.url.trace_add("write", self.invalidate)
        self.refresh()
        self.poll_id = self.after(100, self.poll)
        self.bind("<Destroy>", self.on_destroy, add="+")

    def refresh(self):
        try:
            self.works = self.store.list_works()
            self.existing_combo.configure(values=[
                f"{work['title']} 〔BOOK☆WALKER · {work['id'][-8:]}〕" for work in self.works
            ])
        except (OSError, ValueError, KeyError) as exc:
            self.status.set(f"讀取作品清單失敗：{exc}")

    def select_work(self, event=None):
        index = self.existing_combo.current()
        if index >= 0:
            chosen = self.works[index]
            if self.on_select and not self.on_select(chosen, self.store.work_directory(chosen)):
                if self.current_work:
                    self.existing_combo.current(next(
                        i for i, work in enumerate(self.works) if work["id"] == self.current_work["id"]
                    ))
                else:
                    self.selected.set("")
                return
            self.current_work = chosen
            self.open_button.configure(state="normal")
            self.summary.set(" · ".join(filter(None, (
                self.current_work.get("author"), self.current_work.get("publisher"),
                self.current_work.get("label"),
            ))))

    def open_folder(self):
        if self.current_work is None:
            return
        try:
            os.startfile(str(self.store.work_directory(self.current_work)))
        except (OSError, ValueError) as exc:
            messagebox.showerror("無法開啟資料夾", str(exc), parent=self)

    def invalidate(self, *_):
        self.loaded_url = None
        self.create_button.configure(state="disabled")

    def fetch(self):
        if self.loading:
            return
        try:
            url, _ = normalize_bookwalker_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror("作品網址", str(exc), parent=self)
            return
        self.loaded_url = None
        self.loading = True
        self.fetch_button.configure(state="disabled")
        self.create_button.configure(state="disabled")
        self.url_entry.configure(state="disabled")
        self.status.set("讀取作品資料…")
        for value in self.fields.values():
            value.set("")

        def run():
            try:
                self.events.put((fetch_bookwalker(url), None))
            except Exception as exc:  # noqa: BLE001 - surface background network/parser errors.
                self.events.put((None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            data, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.loading = False
            self.url_entry.configure(state="normal")
            self.fetch_button.configure(state="normal")
            if error:
                self.status.set(f"讀取失敗：{error}")
            else:
                self.url.set(data["url"])
                self.loaded_url = data["url"]
                for key, value in self.fields.items():
                    value.set(data.get(key, ""))
                self.create_button.configure(state="normal")
                self.status.set("可修改資料後建立作品。")
        self.poll_id = self.after(100, self.poll)

    def create(self):
        if self.loaded_url is None or self.loading:
            return
        try:
            data = {key: value.get() for key, value in self.fields.items()}
            data["url"] = self.loaded_url
            work, created = self.store.create(data)
            self.refresh()
            index = next(i for i, item in enumerate(self.works) if item["id"] == work["id"])
            self.existing_combo.current(index)
            self.select_work()
            self.status.set("作品已建立。" if created else "作品已存在，已選取。")
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            messagebox.showerror("無法建立作品", str(exc), parent=self)

    def on_destroy(self, event):
        if event.widget is self:
            self.after_cancel(self.poll_id)
