"""Developer GUI controls, intentionally not included in EXE builds."""

from collections.abc import Callable
from tkinter import StringVar, messagebox, ttk

from developer.settings import CATEGORIES, THRESHOLDS, get_overrides, set_overrides


class DeveloperPage(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, *, is_busy: Callable[[], bool]) -> None:
        super().__init__(parent, padding=20)
        self._is_busy = is_busy
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Gemini safety_settings",
                  font=("Microsoft JhengHei UI", 16, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            self, justify="left", wraplength=650,
            text=("原始碼版專用。套用後影響作品名稱與摘要、章節標題與內文、"
                  "專有名詞分析及手動整理的 Gemini 請求；不影響 Codex。\n"
                  "設定只保留於本次執行，EXE 使用模型預設。既有 checkpoint 仍會沿用；"
                  "若要測試已完成章節，請從翻譯頁選擇重新翻譯。"),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 18))
        active = get_overrides()
        self.values = {
            category: StringVar(value=active.get(category, "模型預設"))
            for category in CATEGORIES
        }
        self.selectors: list[ttk.Combobox] = []
        for row, (category, label) in enumerate(CATEGORIES.items(), 2):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=6)
            selector = ttk.Combobox(self, textvariable=self.values[category],
                                    values=THRESHOLDS, state="readonly", width=34)
            selector.grid(row=row, column=1, sticky="w", padx=(16, 0), pady=6)
            self.selectors.append(selector)
        ttk.Label(
            self, justify="left", wraplength=650,
            text=("模型預設：不送出該類設定。\nOFF：關閉可調整過濾；"
                  "BLOCK_NONE：不依風險機率封鎖。\nBLOCK_ONLY_HIGH：封鎖高機率；"
                  "BLOCK_MEDIUM_AND_ABOVE：封鎖中、高機率；"
                  "BLOCK_LOW_AND_ABOVE：封鎖低、中、高機率。\n"
                  "以上設定不會關閉服務端的核心保護，也不保證解除 PROHIBITED_CONTENT。"),
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=18)
        actions = ttk.Frame(self)
        actions.grid(row=7, column=0, columnspan=2, sticky="w")
        self.apply_button = ttk.Button(actions, text="套用", command=self.apply)
        self.apply_button.pack(side="left")
        self.reset_button = ttk.Button(actions, text="恢復模型預設", command=self.reset)
        self.reset_button.pack(side="left", padx=10)
        self.status = StringVar()
        ttk.Label(self, textvariable=self.status, wraplength=650, justify="left").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=14)
        self._show_active()

    def _show_active(self) -> None:
        active = get_overrides()
        self.status.set("目前生效：\n" + "\n".join(
            f"{label}：{active.get(category, '模型預設')}"
            for category, label in CATEGORIES.items()
        ))

    def apply(self) -> None:
        if self._is_busy():
            messagebox.showwarning("工作執行中", "請等待目前工作結束後再調整安全設定。", parent=self)
            return
        set_overrides({key: value.get() for key, value in self.values.items()})
        self._show_active()

    def reset(self) -> None:
        if self._is_busy():
            return
        for value in self.values.values():
            value.set("模型預設")
        self.apply()

    def set_busy(self, busy: bool) -> None:
        for selector in self.selectors:
            selector.configure(state="disabled" if busy else "readonly")
        for button in (self.apply_button, self.reset_button):
            button.configure(state="disabled" if busy else "normal")
