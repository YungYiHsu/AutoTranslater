"""Isolated local reading-layout experiment; no model requests or file writes."""

import tkinter as tk
from tkinter import ttk

from core.shared.reading_lines import split_for_reading


class ReadingLinesPage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        ttk.Label(self, text="只新增閱讀換行，不還原原書段落；不修改提取紀錄或匯出檔案。",
                  wraplength=650).pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(self)
        actions.pack(fill="x")
        ttk.Button(actions, text="閱讀分行", command=self.run).pack(side="left")
        self.copy_button = ttk.Button(actions, text="複製結果", command=self.copy, state="disabled")
        self.copy_button.pack(side="left", padx=8)
        ttk.Button(actions, text="清空", command=self.clear).pack(side="left")
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, pady=8)
        self.source = self.text_pane(panes, "原始文字（貼上合併後的內文）")
        self.result = self.text_pane(panes, "閱讀分行結果")
        self.result.configure(state="disabled")
        self.status = tk.StringVar(self)
        ttk.Label(self, textvariable=self.status, wraplength=650).pack(anchor="w")
        self.source.bind("<<Modified>>", self.source_changed)
        self.source.edit_modified(False)

    @staticmethod
    def text_pane(panes, title):
        frame = ttk.LabelFrame(panes, text=title, padding=5)
        panes.add(frame, weight=1)
        text = tk.Text(frame, wrap="word", height=10, undo=True)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    def set_result(self, text):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.configure(state="disabled")

    def source_changed(self, event=None):
        if self.source.edit_modified():
            self.source.edit_modified(False)
            self.set_result("")
            self.copy_button.configure(state="disabled")
            self.status.set("")

    def run(self):
        self.source.edit_modified(False)
        source = self.source.get("1.0", "end-1c")
        result = split_for_reading(source)
        self.set_result(result.text)
        self.copy_button.configure(state="normal" if source else "disabled")
        self.status.set(f"新增 {len(result.inserted)} 處換行；文字一致性檢查通過。" if source else "")

    def copy(self):
        text = self.result.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status.set("已複製分行結果。")

    def clear(self):
        self.source.delete("1.0", "end")
        self.set_result("")
        self.copy_button.configure(state="disabled")
        self.status.set("")
