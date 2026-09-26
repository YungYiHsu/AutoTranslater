"""Modal batch setup and sequential translation progress."""

import queue
import threading
from dataclasses import replace
from tkinter import BooleanVar, StringVar, Toplevel, messagebox, ttk

from core.api_usage import ObservedApiUsage
from core.batch_translation import run_batch, select_batch
from core.bootstrap import build_translation_controller
from core.codex_usage import batch_total_summary, usage_summary
from core.controller import ChapterExecutionOptions
from core.exceptions import TranslationCancelled
from extractors.web_kakuyomu import KakuyomuExtractor
from translators.codex_session import CodexSession
from ui.messages import WorkerMessage
from ui.widgets import ScrollableFrame


class BatchTranslationDialog(Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.setup = app.work_result
        self.busy = False
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.title("批次翻譯")
        self.geometry("650x546")
        self.transient(app.root)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._minimized_app = False
        self.bind("<Map>", self._restore_grab, add="+")
        actions = ttk.Frame(self, padding=(20, 10))
        actions.pack(side="bottom", fill="x")
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.content
        self.start = StringVar(value=str(app.chapter_progress.next_number))
        self.end = StringVar(value=str(app.chapter_progress.next_number))
        self.redo = BooleanVar(value=False)
        self.title_option = BooleanVar(value=app.translate_title.get())
        self.body_option = BooleanVar(value=app.translate_body.get())
        self.terms_option = BooleanVar(value=app.update_terms.get())
        self.selected_mode = app.mode.get()
        self._over_limit = False
        config = app.config
        details = [f"執行模式：{'Codex' if self.selected_mode == 'codex' else 'Gemini API'}",
                   f"模型名稱：{config.codex_model if self.selected_mode == 'codex' else config.model}"]
        if self.selected_mode == "codex":
            details.append(f"推理強度：{config.codex_reasoning_effort}")
        details.append(f"Chunk 字元上限：{config.chunk_size}")
        ttk.Label(body, text="\n".join(details), wraplength=590, justify="left").pack(
            fill="x", pady=(0, 10))
        self.estimate = StringVar(value="預計請求次數：請設定章節範圍")
        if self.selected_mode == "gemini":
            ttk.Label(body, textvariable=self.estimate, wraplength=590).pack(fill="x", pady=(0, 10))
        self.controls = []
        for label, var in (("起始章節", self.start), ("結束章節", self.end)):
            ttk.Label(body, text=label).pack(anchor="w")
            entry = ttk.Entry(body, textvariable=var)
            entry.pack(fill="x", pady=4)
            self.controls.append(entry)
        for label, var in (("翻譯標題", self.title_option), ("翻譯正文", self.body_option),
                           ("更新專有名詞", self.terms_option),
                           ("重新翻譯已完成章節", self.redo)):
            if var is self.terms_option and app.mode.get() == "codex":
                continue
            control = ttk.Checkbutton(body, text=label, variable=var)
            control.pack(anchor="w", pady=3)
            self.controls.append(control)
            if var is self.terms_option:
                self.terms_control = control
        def sync_terms(*_args):
            if not self.body_option.get():
                self.terms_option.set(False)
            if hasattr(self, "terms_control"):
                self.terms_control.configure(state="normal" if self.body_option.get() else "disabled")
        self.body_option.trace_add("write", sync_terms)
        self.sync_terms = sync_terms
        sync_terms()
        self.status = StringVar(value="逐章分析並翻譯；未解決的錯誤會停止整批。")
        self.limit = StringVar(value="各章分析後顯示該章請求上限。")
        ttk.Label(body, textvariable=self.status, wraplength=590).pack(fill="x", pady=15)
        ttk.Label(body, textvariable=self.limit, wraplength=590).pack(fill="x")
        self.progress = ttk.Progressbar(body, mode="determinate")
        self.codex_step_text = StringVar(value="尚無本次請求紀錄")
        if self.selected_mode == "codex":
            ttk.Label(body, textvariable=self.codex_step_text, wraplength=590).pack(fill="x")
        self.progress.pack(fill="x", pady=10)
        self.begin = ttk.Button(actions, text="開始批次翻譯", command=self.begin_batch)
        self.begin.pack(side="left")
        self.stop = ttk.Button(actions, text="取消", command=self.cancel, state="disabled")
        self.stop.pack(side="left", padx=10)
        ttk.Button(actions, text="開啟資料夾", command=app.open_folder).pack(side="right")
        ttk.Button(actions, text="最小化程式", command=self.minimize_app).pack(side="right", padx=8)
        for variable in (self.start, self.end, self.title_option, self.body_option,
                         self.terms_option, self.redo):
            variable.trace_add("write", self._schedule_estimate)
        self._estimate_job = None
        self._schedule_estimate()

    def minimize_app(self):
        self._minimized_app = True
        self.grab_release()
        self.app.root.iconify()

    def _restore_grab(self, event):
        if event.widget is self and self._minimized_app:
            self._minimized_app = False
            self.grab_set()

    def _schedule_estimate(self, *_args):
        if self.selected_mode != "gemini":
            return
        if self._estimate_job is not None:
            self.after_cancel(self._estimate_job)
        self._estimate_job = self.after_idle(self._update_estimate)

    def _update_estimate(self):
        self._estimate_job = None
        if self.busy:
            return
        try:
            values = (self.start.get().strip(), self.end.get().strip())
            if any(not v.isascii() or not v.isdecimal() or len(v) > 9 for v in values):
                raise ValueError("invalid range")
            entries = select_batch(self.setup.work.source_work, *map(int, values),
                                   self.app.chapter_progress.completed_numbers, self.redo.get())
        except (ValueError, AttributeError):
            self.estimate.set("預計請求次數：請設定有效章節範圍")
            self._over_limit = False
            return
        count = (int(self.title_option.get()) + int(self.body_option.get())
                 + int(self.terms_option.get() and self.body_option.get())) * len(entries)
        self.estimate.set(f"預計請求次數：至少 {count} 次（{len(entries)} 章；正文每章暫算 1 次）")
        exceeded = count > 20
        if exceeded and not self._over_limit:
            self._over_limit = True
            messagebox.showwarning("超出免費 Flash 每日額度提醒",
                f"本批次預計至少 {count} 次，超過設定的每日 20 次提醒門檻。\n"
                "此為簡易估算，未計多個 chunk、重試或今日已用次數；實際額度依帳號與模型而異。",
                parent=self)
        self._over_limit = exceeded

    def cancel(self):
        self.cancelled.set()
        self.status.set("等待目前工作安全停止……")

    def close(self):
        if self.busy:
            self.cancel()
            return
        self.app._batch_dialog = None
        if self._estimate_job is not None:
            self.after_cancel(self._estimate_job)
        self.destroy()

    def begin_batch(self):
        if self.busy:
            return
        app = self.app
        try:
            values = (self.start.get().strip(), self.end.get().strip())
            if any(not v.isascii() or not v.isdecimal() or len(v) > 9 for v in values):
                raise ValueError("章節必須是正整數。")
            start, end = map(int, values)
            progress = app.chapter_tracker.reconcile(self.setup)
            entries = select_batch(self.setup.work.source_work, start, end,
                                   progress.completed_numbers, self.redo.get())
            if not entries:
                messagebox.showinfo("批次翻譯", "範圍內沒有待翻譯章節。", parent=self)
                return
            existing = [e.number for e in entries if any(
                p.exists() for p in app.chapter_tracker.output_paths(self.setup, e))]
            skipped = end - start + 1 - len(entries)
            if not messagebox.askyesno("確認批次翻譯",
                    f"範圍 {end-start+1} 章，跳過 {skipped} 章，處理 {len(entries)} 章。\n"
                    f"允許覆蓋既有輸出的章節：{existing or '無'}\n"
                    "將使用目前模型與設定逐章執行，是否繼續？", parent=self):
                return
            selected = app.mode.get()
            options = ChapterExecutionOptions(self.title_option.get(), self.body_option.get(),
                self.terms_option.get() and self.body_option.get() and selected != "codex")
            key = app._get_or_request_api_key() if options.requires_api and selected != "codex" else None
            if options.requires_api and selected != "codex" and not key:
                return
            config = replace(app.config)
            site = app._selected_site_key()
            prompt = app.prompt_store.path_for("chapter")
            usage = ObservedApiUsage(app.api_usage,
                lambda: self.events.put(("api_started", options)), count_requests=selected != "codex")

            def factory(entry):
                return build_translation_controller(config=config,
                    output_directory=app.output_directory, checkpoint_directory=app.checkpoint_directory,
                    mode=("codex" if options.requires_api else "codex_analysis") if selected == "codex"
                         else ("gemini" if options.requires_api else "analysis"),
                    auto_open=False, overwrite_outputs=entry.number in existing, api_key=key,
                    prompt_path=prompt, work_directory=self.setup.work_directory,
                    translated_work_title=self.setup.work.translated_title,
                    chapter_extractor=KakuyomuExtractor(chapter_number=entry.number,
                        expected_work_title=self.setup.work.source_work.title) if site == "kakuyomu" else None,
                    usage=usage)

            self.busy = True
            app._codex_usage_records = []
            self.cancelled.clear()
            self.done = 0
            self.current = None
            self.total = len(entries)
            self.skipped = skipped
            self.begin.configure(state="disabled")
            self.stop.configure(state="normal")
            for control in self.controls:
                control.configure(state="disabled")
            self.progress.configure(value=0, maximum=self.total)
            app._apply_state("translating", "批次翻譯中……")
            attempts = 1 if selected == "codex" else config.retry_attempts + 1

            # Capture Tk values before the background thread starts.
            force = progress.completed_numbers if self.redo.get() else frozenset()
            def execute():
                try:
                    run_batch(entries, factory=factory, options=options, force_numbers=force,
                        tracker=app.chapter_tracker, setup=self.setup,
                        emit=lambda kind, value: self.events.put((kind, value)),
                        cancelled=self.cancelled.is_set, attempts=attempts)
                except Exception as exc:  # noqa: BLE001 - marshal worker failures to Tk
                    self.events.put(("finish", exc))
                else:
                    self.events.put(("finish", None))
            threading.Thread(target=execute, daemon=True).start()
            self.after(100, self.poll)
        except Exception as exc:  # noqa: BLE001 - GUI boundary
            messagebox.showerror("無法開始批次翻譯", str(exc), parent=self)

    def poll(self):
        self.app._drain_codex_usage()
        if self.selected_mode == "codex":
            self.codex_step_text.set(self.app.codex_usage_text.get())
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            app = self.app
            if kind == "chapter":
                index, entry = value
                self.current = entry.number
                app.chapter_number.set(str(entry.number))
                app.plan = None
                self.prefix = f"第 {index}/{self.total} 章（作品第 {entry.number} 章）"
                self.status.set(self.prefix + "：正在分析……")
            elif kind == "prepared":
                app.controller, app.plan, limit = value
                background = (CodexSession(self.setup.work_directory, app.config.codex_model)
                              .background_request_count() if app.mode.get() == "codex" and limit else 0)
                self.limit.set(f"本章請求上限：{limit} 次" if app.mode.get() != "codex"
                               else f"本章 Codex 回合數：{limit + background}（摘要初始化 {background} 回合）")
            elif kind == "api_started":
                app._save_execution_options(value)
            elif kind == "progress":
                app._handle_message(WorkerMessage("progress", value))
                self.status.set(self.prefix + "：" + app.status.get())
            elif kind == "completed":
                app.result, app.chapter_progress = value
                self.done += 1
                self.progress.configure(value=self.done)
                app._update_work_details()
                app._refresh_api_usage()
            elif kind == "finish":
                self.app._drain_codex_usage()
                if self.selected_mode == "codex":
                    records = self.app._codex_usage_records
                    self.codex_step_text.set(usage_summary(records, average=True)
                                             + "\n\n" + batch_total_summary(records))
                self.busy = False
                self.stop.configure(state="disabled")
                self.begin.configure(state="normal")
                for control in self.controls:
                    control.configure(state="normal")
                self.sync_terms()
                failed = int(value is not None and not isinstance(value, TranslationCancelled))
                summary = (f"完成 {self.done} 章，跳過 {self.skipped} 章，失敗 {failed} 章，"
                           f"尚未完成 {self.total-self.done-failed} 章。")
                if value is not None:
                    summary += f" 停在第 {self.current} 章。"
                self.status.set(summary)
                app._refresh_api_usage()
                if failed:
                    self.grab_release()
                    app._batch_dialog = None
                    self.destroy()
                    app._show_error(value)
                else:
                    app._apply_state("work_ready", summary)
                    self._schedule_estimate()
                app.summary.set(summary)
                return
        if self.busy:
            self.after(100, self.poll)
