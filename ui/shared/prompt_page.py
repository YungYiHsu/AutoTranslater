"""Shared prompt-template editor."""
from tkinter import END, StringVar, Text, messagebox, ttk

from core.shared.prompt_templates import PROMPT_TEMPLATES


class PromptPage(ttk.Frame):
    def __init__(self, parent, *, store, on_save=lambda: None):
        super().__init__(parent)
        self.root = self.winfo_toplevel()
        self.prompt_store = store
        self.on_save = on_save
        self.prompt_editors = {}
        self._saved_prompts = {}
        self.prompt_description = StringVar(value=PROMPT_TEMPLATES[0].description)
        self._build_prompt_page(self)

    def can_close(self):
        if self._prompts_changed():
            choice = messagebox.askyesnocancel("Prompt 尚未儲存", "關閉前要儲存 Prompt 模板嗎？", parent=self.root)
            if choice is None or (choice and not self._save_prompts()):
                return False
        return True

    def _build_prompt_page(self, page: ttk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        header = ttk.Frame(page, padding=(20, 16, 20, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Prompt 模板", font=("Microsoft JhengHei UI", 16, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            header,
            textvariable=self.prompt_description,
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        editor_tabs = ttk.Notebook(page)
        self.prompt_tabs = editor_tabs
        editor_tabs.grid(row=1, column=0, sticky="nsew", padx=20)
        for template in PROMPT_TEMPLATES:
            frame = ttk.Frame(editor_tabs, padding=8)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            editor = Text(
                frame, wrap="word", undo=True, font=("Microsoft JhengHei UI", 11), padx=10, pady=10
            )
            scrollbar = ttk.Scrollbar(frame, orient="vertical", command=editor.yview)
            editor.configure(yscrollcommand=scrollbar.set)
            editor.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")
            content = self.prompt_store.read(template.key)
            editor.insert("1.0", content)
            editor.edit_reset()
            self.prompt_editors[template.key] = editor
            self._saved_prompts[template.key] = content
            required_frame = ttk.LabelFrame(frame, text="固定格式（不可編輯）", padding=6)
            required_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            required_text = Text(
                required_frame,
                height=min(12, len(template.required_text.splitlines()) + 1),
                wrap="word",
                font=("Microsoft JhengHei UI", 10),
                padx=8,
                pady=6,
                background="#f0f0f0",
            )
            required_text.pack(fill="x")
            required_text.insert("1.0", template.required_text)
            required_text.configure(state="disabled")
            editor_tabs.add(frame, text=template.label)
        editor_tabs.bind("<<NotebookTabChanged>>", self._on_prompt_tab_changed)
        self._on_prompt_tab_changed()

        actions = ttk.Frame(page, padding=(20, 10, 20, 16))
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="儲存全部", command=self._save_prompts).pack(side="left")
        ttk.Button(actions, text="還原內建預設", command=self._restore_default_prompts).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="重新載入", command=self._reload_prompts).pack(side="left")

    def _on_prompt_tab_changed(self, _event: object | None = None) -> None:
        """Show instructions for the currently selected editable prompt."""
        selected = self.prompt_tabs.select()
        if not selected:
            return
        index = self.prompt_tabs.index(selected)
        if 0 <= index < len(PROMPT_TEMPLATES):
            self.prompt_description.set(PROMPT_TEMPLATES[index].description)

    def _editor_text(self, key: str) -> str:
        return self.prompt_editors[key].get("1.0", END).rstrip("\n") + "\n"

    def _prompts_changed(self) -> bool:
        return any(
            self._editor_text(template.key) != self._saved_prompts[template.key]
            for template in PROMPT_TEMPLATES
        )

    def _save_prompts(self) -> bool:
        try:
            values = {
                template.key: self._editor_text(template.key) for template in PROMPT_TEMPLATES
            }
            if any(not value.strip() for value in values.values()):
                raise ValueError("Prompt 模板不可為空白。")
            for key, value in values.items():
                self.prompt_store.save(key, value)
                self._saved_prompts[key] = self.prompt_store.read(key)
        except (OSError, ValueError) as exc:
            messagebox.showerror("儲存失敗", str(exc), parent=self.root)
            return False
        self.on_save()
        messagebox.showinfo(
            "儲存完成", "Prompt 模板已儲存，將從下一次工作開始套用。", parent=self.root
        )
        return True

    def _restore_default_prompts(self) -> None:
        if not messagebox.askyesno(
            "還原內建預設",
            "這會還原所有分頁的模板；按「儲存全部」後才會寫入檔案。是否繼續？",
            parent=self.root,
        ):
            return
        for template in PROMPT_TEMPLATES:
            editor = self.prompt_editors[template.key]
            editor.delete("1.0", END)
            editor.insert("1.0", self.prompt_store.default_text(template.key))

    def _reload_prompts(self) -> None:
        if self._prompts_changed() and not messagebox.askyesno(
            "捨棄未儲存變更",
            "重新載入會捨棄目前尚未儲存的修改，是否繼續？",
            parent=self.root,
        ):
            return
        for template in PROMPT_TEMPLATES:
            content = self.prompt_store.read(template.key)
            editor = self.prompt_editors[template.key]
            editor.delete("1.0", END)
            editor.insert("1.0", content)
            editor.edit_reset()
            self._saved_prompts[template.key] = content
