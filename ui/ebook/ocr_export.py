"""Page/chapter export UI; planning and persistence run through the background queue."""

import copy
import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core.ebook.chapters import commit_export, prepare_export
from core.ebook.exports import export_image, page_stem, suggested_pages
from core.ebook.ocr.session import save_session


class PageRangeDialog(simpledialog.Dialog):
    def __init__(self, parent, first, last=None):
        self.first = first
        self.last = first + 1 if last is None else last
        super().__init__(parent, "匯出章節原文")

    def body(self, master):
        self.head = tk.StringVar(master, str(self.first))
        self.tail = tk.StringVar(master, str(self.last))
        ttk.Label(master, text="頭頁").grid(row=0, column=0, padx=6, pady=6)
        entry = ttk.Entry(master, textvariable=self.head, width=12)
        entry.grid(row=0, column=1, padx=6, pady=6)
        ttk.Label(master, text="尾頁").grid(row=0, column=2, padx=6, pady=6)
        ttk.Entry(master, textvariable=self.tail, width=12).grid(
            row=0, column=3, padx=6, pady=6
        )
        self.head.trace_add("write", self._head_changed)
        return entry

    def _head_changed(self, *_):
        try:
            head = int(self.head.get())
        except ValueError:
            return
        if head >= 0:
            self.tail.set(str(head + 1))

    def validate(self):
        try:
            pages = int(self.head.get()), int(self.tail.get())
            page_stem(*pages)
        except ValueError:
            messagebox.showerror("頁碼", "請輸入非負整數，尾頁不能小於頭頁。", parent=self)
            return False
        self.pages = pages
        return True

    def apply(self):
        self.result = self.pages


class OcrExportMixin:
    def export_original(self):
        if self.busy or not self.work_directory or not self.data or not self.confirm_dirty():
            return
        first_page, _ = suggested_pages(self.work_directory)
        last_page = None
        previous = self.data.get("work_capture", {})
        if previous.get("image"):
            first_page = previous["right"]
            last_page = previous.get("left") if previous.get("left") is not None else first_page
        pages = PageRangeDialog(self.root, first_page, last_page).result
        if pages is None:
            return
        right, left = pages
        page_stem(right, left)
        if previous.get("image") and (
            right != previous["right"]
            or left != (previous.get("left") if previous.get("left") is not None else right)
        ):
            raise ValueError("這張圖片已匯出，請使用原頁碼。")
        index = self.work_directory / "chapters.json"
        chapters = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        maximum = max((int(key) for key in chapters), default=0)
        number = simpledialog.askinteger(
            "匯出章節原文", "章節編號", initialvalue=maximum, minvalue=0, parent=self.root
        )
        if number is None:
            return
        count = len(self.data.get("columns", []))
        span = simpledialog.askstring(
            "匯出章節原文", "起訖行號", initialvalue=f"1,{count}", parent=self.root
        )
        if span is None:
            return
        first, last = (int(value.strip()) for value in span.split(","))
        data = copy.deepcopy(self.data)
        data["work_capture"] = {"work_id": self.work["id"], "right": right, "left": left}
        args = (self.work_directory, data, self.folder.name, number, first, last)
        self.background(
            lambda: prepare_export(*args), lambda plan: self._confirm_export(plan, data, previous)
        )

    def _confirm_export(self, plan, data, previous):
        if plan.conflict and not messagebox.askyesno(
            "更新章節原文",
            "所選頁碼已有內容，是否以本次文字取代該章完整重疊範圍？",
            parent=self.root,
            default="no",
        ):
            return
        right, left = data["work_capture"]["right"], data["work_capture"]["left"]
        path = self.work_directory / 'images' / (page_stem(right, left) + '.png')
        before = path.read_bytes() if path.exists() else None
        if before is not None and not messagebox.askyesno(
            '覆蓋圖片', f'{path.name} 已存在，是否覆蓋？', parent=self.root, default='no'
        ):
            return
        directory, image, folder = self.work_directory, self.image, self.folder

        def work():
            with export_image(directory, image, right, left, before) as saved:
                output = commit_export(plan, replace=plan.conflict)
            data["work_capture"]["image"] = saved.name
            save_session(folder, data)
            return output, data

        self.background(work, self._export_finished)

    def _export_finished(self, result):
        output, self.data = result
        self.update_page_suggestion()
        self.status.set(f"匯出完成：{output.name}")
