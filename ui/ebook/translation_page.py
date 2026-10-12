"""Single-chapter ebook translation, using the shared webnovel translation engine."""

import json
import os
import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk

from core.ebook import usage
from core.ebook.translation import EbookTranslation, load_source
from core.shared.codex_models import model_choices, supported_efforts
from core.shared.codex_usage import usage_summary
from core.shared.config import AppConfig, load_config, write_config
from core.shared.exceptions import TranslationCancelled
from core.shared.prompt_templates import PromptTemplateStore
from translators.codex_client import check_login


class EbookTranslationPage(ttk.Frame):
    def __init__(self, parent, *, app_directory=None, config_provider=None):
        super().__init__(parent, padding=10)
        self.app_directory = Path(app_directory or ".")
        self.settings_path = self.app_directory / "runtime" / "ebook" / "translation.json"
        self.config = (load_config(self.settings_path) if self.settings_path.exists() else
                       config_provider() if config_provider else AppConfig())
        self.directory = None
        self.chapters, self.keys, self.catalog = {}, [], []
        self.current_key = None
        self.has_title = False
        self.analysis = None
        self.busy = False
        self.external_busy = lambda: False
        self.messages = queue.Queue()
        self.cancel_event = threading.Event()
        self.closed = False
        self.status = tk.StringVar(self)
        self.usage_text = tk.StringVar(self)
        self.model = tk.StringVar(self, self.config.codex_model)
        self.effort = tk.StringVar(self, self.config.codex_reasoning_effort)
        self.chunk_count = tk.StringVar(self, "1")
        self.translate_title = tk.BooleanVar(self, self.config.translate_title)
        self.controls = []

        self.combo = ttk.Combobox(self, state="readonly")
        self.combo.pack(fill="x", pady=(0, 8))
        self.combo.bind("<<ComboboxSelected>>", self.choose)
        self.controls.append(self.combo)
        settings = ttk.LabelFrame(self, text="翻譯設定", padding=8)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Codex").grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="模型").grid(row=1, column=0, sticky="w")
        self.model_combo = ttk.Combobox(settings, textvariable=self.model, state="readonly")
        self.model_combo.grid(row=1, column=1, sticky="ew", pady=4)
        self.model_combo.bind("<<ComboboxSelected>>", self.model_changed)
        self.effort_combo = ttk.Combobox(settings, textvariable=self.effort, state="readonly", width=10)
        self.effort_combo.grid(row=1, column=2, padx=6)
        self.effort_combo.bind("<<ComboboxSelected>>", self.remember_effort)
        self.models_button = ttk.Button(settings, text="更新模型／檢查登入", command=self.refresh_models)
        self.models_button.grid(row=0, column=2)
        ttk.Label(settings, text="分段數").grid(row=2, column=0, sticky="w")
        self.count_entry = ttk.Spinbox(settings, textvariable=self.chunk_count, from_=1, to=20, width=8)
        self.count_entry.grid(row=2, column=1, sticky="w")
        self.title_check = ttk.Checkbutton(settings, text="翻譯章名", variable=self.translate_title)
        self.title_check.grid(row=4, column=0, sticky="w")
        ttk.Label(settings, text="使用「Prompt 模板 → 章節翻譯」", foreground="#666666").grid(row=5, column=0, columnspan=3, sticky="w")
        self.controls += [self.model_combo, self.effort_combo, self.models_button,
                          self.count_entry, self.title_check]
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=8)
        self.action_buttons = {}
        for label, command in (("分析章節", self.prepare), ("翻譯／續傳", self.run),
                               ("重新翻譯", lambda: self.run(force=True)), ("開啟 HTML", self.open_html)):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self.controls.append(button)
            self.action_buttons[label] = button
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Label(self, text="章節原文").pack(anchor="w")
        source_frame = ttk.Frame(self)
        source_frame.pack(fill="both", expand=True)
        self.original = tk.Text(source_frame, height=5, wrap="word", state="disabled")
        source_scroll = ttk.Scrollbar(source_frame, command=self.original.yview)
        self.original.configure(yscrollcommand=source_scroll.set)
        source_scroll.pack(side="right", fill="y")
        self.original.pack(side="left", fill="both", expand=True)
        result_frame = ttk.LabelFrame(self, text="章節分析結果", padding=5)
        result_frame.pack(fill="x", pady=(8, 0))
        self.analysis_summary = tk.StringVar(self)
        self.analysis_label = ttk.Label(
            result_frame, textvariable=self.analysis_summary, justify="left",
            anchor="nw", wraplength=650, padding=(0, 2, 0, 8),
        )
        self.analysis_label.pack(fill="x")
        result_frame.bind("<Configure>", lambda event: self.analysis_label.configure(
            wraplength=max(1, event.width - 24),
        ))
        for variable in (self.model, self.effort, self.chunk_count, self.translate_title):
            variable.trace_add("write", lambda *_: self.clear_analysis())
        self.progress = ttk.Progressbar(self)
        self.progress.pack(fill="x", pady=8)
        ttk.Label(self, textvariable=self.status, wraplength=650).pack(anchor="w")
        quota = ttk.Frame(self)
        quota.pack(fill="x", pady=8)
        self.quota_button = ttk.Button(quota, text="更新額度", command=self.refresh_limits)
        self.quota_button.pack(side="left")
        self.history_button = ttk.Button(quota, text="Codex 翻譯紀錄", command=self.show_history)
        self.history_button.pack(side="left", padx=5)
        self.controls += [self.quota_button, self.history_button]
        ttk.Label(self, textvariable=self.usage_text, wraplength=650).pack(anchor="w")
        self.after_id = self.after(80, self.poll)
        self.update_model_choices()
        self.update_actions()

    def update_actions(self):
        enabled = not self.busy and self.current_key is not None
        for label, button in self.action_buttons.items():
            available = enabled
            if label == "開啟 HTML" and available:
                available = (self.directory / "translations" / f"{int(self.current_key):03d}.html").is_file()
            button.configure(state="normal" if available else "disabled")
        self.title_check.configure(state="normal" if enabled and self.has_title else "disabled")

    def can_leave(self):
        if self.busy:
            self.status.set("請先取消並等待目前工作結束。")
        return not self.busy

    def load_work(self, directory):
        self.directory = Path(directory)
        self.current_key = self.analysis = None
        self.clear_analysis()
        self.fill_source("")
        self.combo.set("")
        self.refresh()

    def refresh(self):
        if self.busy:
            return
        try:
            path = self.directory / "chapters.json" if self.directory else None
            self.chapters = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
            self.keys = sorted(self.chapters, key=int)
            self.combo.configure(values=[f"{int(k):03d}" for k in self.keys])
            if self.keys:
                key = self.current_key if self.current_key in self.keys else self.keys[0]
                self.combo.current(self.keys.index(key))
                self.choose()
            else:
                self.current_key = self.analysis = None
                self.clear_analysis()
                self.combo.set("")
                self.fill_source("")
                self.status.set("")
            self.load_usage()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.set(f"讀取章節失敗：{exc}")
        self.update_actions()

    def fill_source(self, text):
        self.original.configure(state="normal")
        self.original.delete("1.0", "end")
        self.original.insert("1.0", text)
        self.original.configure(state="disabled")

    def clear_analysis(self):
        self.analysis = None
        self.analysis_summary.set("")

    def choose(self, event=None):
        if self.busy or self.combo.current() < 0:
            return
        try:
            key = self.keys[self.combo.current()]
            source = load_source(self.directory, key)
            if self.analysis and (self.analysis.source.key != key or self.analysis.source.chapter != source.chapter):
                self.clear_analysis()
            if key != self.current_key:
                self.analysis = None
                self.chunk_count.set(str(min(20, max(1, (len(source.chapter.original_text) + self.config.chunk_size - 1) // self.config.chunk_size))))
            self.current_key = key
            self.has_title = source.has_title
            self.fill_source(source.chapter.original_text)
            old = self.chapters[key].get("translation_source_sha256")
            self.status.set("原文已更新，現有譯文可能需要重譯。" if old and old != source.digest else "")
            self.title_check.configure(state="normal" if source.has_title else "disabled")
        except (OSError, ValueError, KeyError) as exc:
            self.current_key = self.analysis = None
            self.clear_analysis()
            self.status.set(str(exc))
        self.update_actions()

    def remember_effort(self, event=None):
        if self.effort.get() in supported_efforts(self.catalog, self.model.get()):
            self.config = replace(self.config, codex_model_efforts={
                **self.config.codex_model_efforts, self.model.get(): self.effort.get(),
            })

    def model_changed(self, event=None):
        self.analysis = None
        self.effort.set(self.config.codex_model_efforts.get(self.model.get(), "low"))
        self.update_model_choices()

    def update_model_choices(self):
        self.model_combo.configure(values=model_choices(self.catalog),
                                   state="disabled" if self.busy else "readonly")
        efforts = supported_efforts(self.catalog, self.model.get())
        self.effort_combo.configure(values=efforts, state="readonly" if efforts and not self.busy else "disabled")
        if efforts and self.effort.get() not in efforts:
            info = next(m for m in self.catalog if m["model"] == self.model.get())
            fallback = "low" if "low" in efforts else info.get("defaultReasoningEffort")
            self.effort.set(fallback if fallback in efforts else efforts[0])
        self.models_button.configure(state="normal" if not self.busy else "disabled")
        self.history_button.configure(state="normal" if not self.busy and self.directory else "disabled")

    def refresh_models(self):
        if not self.busy:
            self.background(lambda: check_login(self.app_directory, include_models=True), self.models_loaded)

    def models_loaded(self, result):
        self.catalog = result["models"]
        choices = model_choices(self.catalog)
        if self.model.get() not in choices:
            self.model.set(choices[0] if choices else "")
        self.update_model_choices()
        self.status.set("模型清單已更新。")

    def settings(self):
        if not self.model.get().strip():
            raise ValueError("請選擇模型。")
        count = int(self.chunk_count.get())
        if not 1 <= count <= 20:
            raise ValueError("分段數須為 1 到 20。")
        changes = {"translate_title": self.translate_title.get(), "update_terms": False}
        if self.model.get() not in model_choices(self.catalog) or self.effort.get() not in supported_efforts(self.catalog, self.model.get()):
            raise ValueError("請先更新模型清單，並選擇支援的模型與強度。")
        changes.update(codex_model=self.model.get(), codex_reasoning_effort=self.effort.get(),
                       codex_model_efforts={**self.config.codex_model_efforts, self.model.get(): self.effort.get()})
        config = replace(self.config, **changes)
        # Validate numbers using the same constraints as webnovel settings.
        from dataclasses import asdict
        config = AppConfig.from_mapping(asdict(config))
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        write_config(config, self.settings_path)
        self.config = config
        store = PromptTemplateStore(self.app_directory)
        store.ensure()
        return config, count, store.path_for("chapter")

    def prepare(self):
        if self.busy or self.current_key is None or self.external_busy():
            return
        try:
            config, count, prompt = self.settings()
            mode = "codex_analysis"
            directory, key = self.directory, self.current_key
            def task():
                job = EbookTranslation(directory, key, config=config, mode=mode, prompt_path=prompt)
                job.resegment(count)
                return job
            self.background(task, self.prepared)
        except Exception as exc:  # noqa: BLE001 - Tk callback error boundary.
            self.error(exc)

    def prepared(self, job):
        self.chunk_count.set(str(job.plan.total_chunks))
        self.analysis = job
        title, body, _, total = job.request_estimate(translate_title=self.translate_title.get(), update_terms=False)
        lines = [f"{job.source.chapter.chapter_title} · {len(job.source.chapter.original_text):,} 字元",
                 f"模型：{job.config.codex_model} · 強度：{job.config.codex_reasoning_effort}",
                 f"共 {job.plan.total_chunks} 段；可續用 {job.plan.completed_count} 段，待翻譯 {body} 段。",
                 f"章名 {title} 回合、內文 {body} 回合；合計最多 {total} 回合。", ""]
        completed = {chunk.index for chunk in job.plan.completed_chunks}
        lines.extend(f"第 {chunk.index + 1} 段：{chunk.char_count:,} 字元 · {'已完成' if chunk.index in completed else '待翻譯'}"
                     for chunk in job.plan.chunks)
        self.analysis_summary.set("\n".join(lines))
        self.status.set("章節分析完成。")

    def run(self, force=False):
        if self.busy or self.current_key is None or self.external_busy():
            return
        try:
            config, count, prompt = self.settings()
            directory, key, mode = self.directory, self.current_key, "codex"
            paths = [directory / "translations" / f"{int(key):03d}.{ext}" for ext in ("txt", "html")]
            overwrite = any(p.exists() for p in paths)
            # Freeze source and prompt before starting the worker; no Tk access from it.
            job = EbookTranslation(directory, key, config=config, mode=mode, prompt_path=prompt,
                                   overwrite=overwrite or force)
            job.resegment(count)
            title, body, _, total = job.request_estimate(force=force, translate_title=config.translate_title, update_terms=False)
            confirmation = f"第 {int(key):03d} 章 · {self.model.get()}\n章名 {title}、內文 {body} 個工作。\n"
            confirmation += f"推理強度：{self.effort.get()}；最多 {total} 回合。"
            if overwrite:
                confirmation += "\n完成後覆蓋此章 TXT 與 HTML。"
            if force:
                confirmation += "\n重新翻譯會清除本次設定的分段進度。"
            if not messagebox.askyesno("確認翻譯", confirmation + "\n是否繼續？", parent=self):
                return
            self.progress.configure(value=0, maximum=job.plan.total_chunks)
            self.background(lambda: job.run(force=force, translate_title=config.translate_title,
                                           update_terms=False,
                                           should_cancel=self.cancel_event.is_set,
                                           progress=lambda update: self.messages.put(("progress", update))), self.completed)
        except Exception as exc:  # noqa: BLE001 - Tk callback error boundary.
            self.error(exc)

    def completed(self, result):
        self.clear_analysis()
        self.refresh()
        self.status.set(f"翻譯完成：{', '.join(p.name for p in result.output_paths)}")

    def open_html(self):
        if self.directory and self.current_key is not None:
            path = self.directory / "translations" / f"{int(self.current_key):03d}.html"
            if path.exists():
                try:
                    os.startfile(str(path))
                except OSError as exc:
                    self.error(exc)

    def load_usage(self):
        if self.directory:
            records = [r for r in usage.history(self.directory) if r.get("component") != "ebook-extract"]
            self.usage_text.set(usage_summary(records[-1:]) if records else "")

    def refresh_limits(self):
        if not self.busy and self.directory:
            directory = self.directory
            self.background(lambda: usage.query(directory), lambda data: self.usage_text.set(usage.limits_text(data)))

    def show_history(self):
        if not self.directory:
            return
        window = tk.Toplevel(self)
        window.title("作品翻譯紀錄")
        text = tk.Text(window, wrap="word")
        text.pack(fill="both", expand=True)
        for entry in usage.history(self.directory):
            if entry.get("component") != "ebook-extract":
                text.insert("end", f"{entry.get('time', '')}\n{usage.model_summary(entry)}\n{usage_summary([entry])}\n\n")
        text.configure(state="disabled")

    def set_busy(self, value):
        self.busy = value
        for widget in self.controls:
            widget.configure(state="disabled" if value else "normal")
        self.combo.configure(state="disabled" if value else "readonly")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.update_model_choices()
        self.update_actions()

    def background(self, action, completed):
        self.cancel_event.clear()
        self.set_busy(True)
        self.status.set("處理中…")
        def worker():
            try:
                self.messages.put(("done", (completed, action())))
            except Exception as exc:  # noqa: BLE001 - deliver worker failures to Tk.
                self.messages.put(("error", exc))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == "progress":
                    self.progress.configure(maximum=data.total, value=data.completed)
                    label = {"translating": "翻譯中", "translated": "已完成", "translating_title": "翻譯章名",
                             "checkpoint": "沿用分段"}.get(data.source, "處理中")
                    self.status.set(f"{data.completed}/{data.total} 段 · {label}")
                else:
                    self.set_busy(False)
                    if kind == "done":
                        callback, result = data
                        callback(result)
                    elif isinstance(data, TranslationCancelled):
                        self.status.set("已取消，分段進度已保留。")
                    else:
                        self.error(data)
                    self.load_usage()
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - keep the Tk event pump alive.
            self.set_busy(False)
            self.error(exc)
        if not self.closed:
            self.after_id = self.after(80, self.poll)

    def error(self, exc):
        self.status.set(str(exc))
        messagebox.showerror("電子書翻譯", str(exc), parent=self)

    def cancel(self):
        self.cancel_event.set()
        self.status.set("目前請求結束後停止，保留已完成分段。")

    def shutdown(self):
        self.closed = True
        self.cancel_event.set()
        self.after_cancel(self.after_id)
