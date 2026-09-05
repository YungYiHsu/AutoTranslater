"""Tkinter desktop application for work selection and chapter translation."""

from __future__ import annotations

import os
import queue
from pathlib import Path
from tkinter import END, StringVar, Text, Tk, Toplevel, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Literal

from core.bootstrap import (
    RunMode,
    build_term_organization_service,
    build_translation_controller,
    build_work_setup_service,
)
from core.chapter_progress import ChapterCompletionTracker, ChapterProgress
from core.config import AppConfig, get_api_key, load_config, load_environment, save_api_key
from core.controller import (
    ProgressUpdate,
    TranslationController,
    TranslationPlan,
    TranslationResult,
)
from core.exceptions import (
    ChapterNotFoundError,
    GeminiFreeTierQuotaError,
    TermMemoryError,
)
from core.local_works import discover_local_works
from core.models import NovelChapter, NovelWork, TextChunk, TranslatedChapter, TranslatedChunk
from core.paths import ensure_runtime_directories, get_app_dir
from core.prompt_templates import PROMPT_TEMPLATES, PromptTemplateStore
from core.term_memory import TermMemoryStore
from core.term_organizer import (
    TermOrganizationBatch,
    TermOrganizationPlan,
    TermOrganizationService,
)
from core.work_memory import text_hash
from core.work_setup import WorkSetupResult, WorkSetupService
from extractors.web_syosetu_work import SyosetuWorkExtractor
from formatters import HtmlFormatter
from ui.messages import WorkerMessage
from ui.widgets import LoadingSpinner
from ui.worker import TranslationWorker

AppState = Literal[
    "idle",
    "selecting_work",
    "work_ready",
    "analyzing_chapter",
    "chapter_ready",
    "translating",
    "organizing_terms",
]

_WORK_PROGRESS_TEXT = {
    "fetching_work": "正在讀取作品資料……",
    "checking_directory": "正在檢查作品資料夾……",
    "checking_memory": "正在檢查作品記憶……",
    "translating_metadata": "正在翻譯作品名稱與摘要……",
    "saving_memory": "正在儲存作品資料……",
    "completed": "作品資料準備完成。",
}


class DesktopApp:
    """Own widgets and translate worker messages into main-thread UI updates."""

    def __init__(self, root: Tk, *, app_directory: Path | None = None) -> None:
        self.root = root
        self.app_directory = app_directory or get_app_dir()
        self.config: AppConfig = load_config(self.app_directory / "config.json")
        self.prompt_store = PromptTemplateStore(self.app_directory)
        self.prompt_store.ensure()
        self.output_directory, self.checkpoint_directory, _ = ensure_runtime_directories(
            self.config.output_directory, app_dir=self.app_directory
        )
        self.worker = TranslationWorker()
        self.controller: TranslationController | None = None
        self.plan: TranslationPlan | None = None
        self.result: TranslationResult | None = None
        self.work: NovelWork | None = None
        self.work_result: WorkSetupResult | None = None
        self.chapter_tracker = ChapterCompletionTracker()
        self.term_memory_store = TermMemoryStore()
        self._term_organization_service: TermOrganizationService | None = None
        self._term_organization_batches: tuple[TermOrganizationBatch, ...] = ()
        self._term_organization_batch_index = 0
        self._term_organization_applied_batches = 0
        self._term_organization_skipped_batches = 0
        self._state_before_term_organization: AppState = "work_ready"
        self.chapter_progress: ChapterProgress | None = None
        self._analyzed_mode: str | None = None
        self._selected_input_url = ""
        self._requested_chapter: int | None = None
        self.state: AppState = "idle"
        self.prompt_editors: dict[str, Text] = {}
        self._saved_prompts: dict[str, str] = {}
        self._local_work_urls = {
            option.label: option.source_url
            for option in discover_local_works(self.output_directory)
        }

        self.url = StringVar()
        self.chapter_number = StringVar()
        self.mode = StringVar(value="gemini")
        self.japanese_work = StringVar(value="尚未選擇作品")
        self.chinese_work = StringVar(value="")
        self.work_details = StringVar(value="")
        self.summary = StringVar(value="請先選擇作品，再輸入章節數字。")
        self.status = StringVar(value="就緒")

        self._configure_window()
        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.url.trace_add("write", self._on_url_changed)
        self.chapter_number.trace_add("write", self._on_chapter_changed)
        self._apply_state("idle")
        self.root.after(100, self._poll_messages)

    def _configure_window(self) -> None:
        self.root.title("日文小說繁體中文翻譯器")
        self.root.geometry("860x760")
        self.root.minsize(720, 660)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _build_widgets(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")
        translation_page = ttk.Frame(notebook)
        prompt_page = ttk.Frame(notebook)
        notebook.add(translation_page, text="翻譯")
        notebook.add(prompt_page, text="Prompt 模板")
        translation_page.columnconfigure(0, weight=1)
        translation_page.rowconfigure(0, weight=1)

        outer = ttk.Frame(translation_page, padding=20)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        ttk.Label(
            outer, text="日文小說繁體中文翻譯器", font=("Microsoft JhengHei UI", 18, "bold")
        ).grid(row=0, column=0, sticky="w", pady=(0, 14))

        url_frame = ttk.LabelFrame(outer, text="作品首頁", padding=12)
        url_frame.grid(row=1, column=0, sticky="ew")
        url_frame.columnconfigure(0, weight=1)
        self.url_entry = ttk.Combobox(
            url_frame,
            textvariable=self.url,
            values=tuple(self._local_work_urls),
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<<ComboboxSelected>>", self._on_local_work_selected)
        self.select_work_button = ttk.Button(url_frame, text="選擇作品", command=self.select_work)
        self.select_work_button.grid(row=0, column=1)
        self.refresh_catalog_button = ttk.Button(
            url_frame, text="重新整理完整目錄", command=self.refresh_catalog
        )
        self.refresh_catalog_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            url_frame,
            text="例如：https://ncode.syosetu.com/n1234ab/（首次選擇可能使用 1 次 API）",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        work_frame = ttk.LabelFrame(outer, text="作品資訊", padding=12)
        work_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(
            work_frame,
            textvariable=self.japanese_work,
            justify="left",
            wraplength=750,
            font=("Microsoft JhengHei UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(work_frame, textvariable=self.chinese_work, justify="left", wraplength=750).pack(
            anchor="w", pady=(4, 0)
        )
        ttk.Label(work_frame, textvariable=self.work_details, justify="left").pack(
            anchor="w", pady=(4, 0)
        )

        chapter_frame = ttk.LabelFrame(outer, text="章節選擇", padding=12)
        chapter_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(chapter_frame, text="章節數字：").grid(row=0, column=0, sticky="w")
        self.chapter_entry = ttk.Entry(chapter_frame, textvariable=self.chapter_number, width=12)
        self.chapter_entry.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.analyze_button = ttk.Button(
            chapter_frame, text="分析章節", command=self.analyze_chapter
        )
        self.analyze_button.grid(row=0, column=2, sticky="w")
        ttk.Label(chapter_frame, text="請輸入大於 0 的半形整數。", foreground="#666666").grid(
            row=0, column=3, sticky="w", padx=(12, 0)
        )

        mode_frame = ttk.LabelFrame(outer, text="執行模式", padding=12)
        mode_frame.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        modes = (
            ("Gemini 真實翻譯", "gemini"),
            ("本地模型（待開發）", "local"),
        )
        self.mode_buttons: list[ttk.Radiobutton] = []
        for column, (label, value) in enumerate(modes):
            button = ttk.Radiobutton(
                mode_frame,
                text=label,
                value=value,
                variable=self.mode,
                command=self._on_mode_changed,
            )
            button.grid(row=0, column=column, sticky="w", padx=(0, 18))
            self.mode_buttons.append(button)

        summary_frame = ttk.LabelFrame(outer, text="章節資訊", padding=(12, 12, 12, 18))
        summary_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        outer.rowconfigure(5, weight=1)
        ttk.Label(
            summary_frame,
            textvariable=self.summary,
            justify="left",
            wraplength=750,
            padding=(0, 2, 0, 8),
        ).pack(anchor="nw", fill="both", expand=True)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=6, column=0, sticky="ew", pady=(12, 4))
        progress_frame.columnconfigure(1, weight=1)
        self.spinner = LoadingSpinner(progress_frame)
        self.spinner.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.spinner.grid_remove()
        ttk.Label(progress_frame, textvariable=self.status).grid(row=0, column=1, sticky="w")

        actions = ttk.Frame(outer)
        actions.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        self.start_button = ttk.Button(actions, text="開始翻譯", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel)
        self.cancel_button.pack(side="left", padx=8)
        self.open_html_button = ttk.Button(actions, text="開啟 HTML", command=self.open_html)
        self.open_html_button.pack(side="right")
        self.open_folder_button = ttk.Button(
            actions, text="開啟輸出資料夾", command=self.open_folder
        )
        self.open_folder_button.pack(side="right", padx=8)
        self.open_terms_button = ttk.Button(
            actions, text="開啟專有名詞", command=self.open_terms
        )
        self.open_terms_button.pack(side="right")
        self.organize_terms_button = ttk.Button(
            actions, text="整理專有名詞", command=self.organize_terms
        )
        self.organize_terms_button.pack(side="right", padx=(0, 8))
        self.url_entry.focus_set()
        self._build_prompt_page(prompt_page)

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
            text=(
                "章節模板可使用 {Novel_Content} 與 {Term_Memory}；"
                "缺少時會自動附加，重複時無法儲存。"
            ),
        ).pack(anchor="w", pady=(6, 0))

        editor_tabs = ttk.Notebook(page)
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
            editor_tabs.add(frame, text=template.label)

        actions = ttk.Frame(page, padding=(20, 10, 20, 16))
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="儲存全部", command=self._save_prompts).pack(side="left")
        ttk.Button(actions, text="還原內建預設", command=self._restore_default_prompts).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="重新載入", command=self._reload_prompts).pack(side="left")

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
        if self.plan is not None and self.state == "chapter_ready":
            self.plan = None
            self.controller = None
            self.result = None
            self.summary.set("Prompt 已變更，請重新分析章節後再開始翻譯。")
            self._apply_state("work_ready", "Prompt 已儲存；章節需要重新分析。")
        messagebox.showinfo(
            "儲存完成", "Prompt 模板已儲存，將從下一次工作開始套用。", parent=self.root
        )
        return True

    def _restore_default_prompts(self) -> None:
        if not messagebox.askyesno(
            "還原內建預設",
            "這會以內建預設取代編輯框中的內容；按「儲存全部」後才會寫入檔案。是否繼續？",
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

    def _on_close(self) -> None:
        if self.worker.is_running:
            messagebox.showwarning("工作執行中", "請先取消目前工作，再關閉程式。", parent=self.root)
            return
        if self._prompts_changed():
            choice = messagebox.askyesnocancel(
                "Prompt 尚未儲存",
                "Prompt 模板有尚未儲存的修改。關閉前要儲存嗎？",
                parent=self.root,
            )
            if choice is None:
                return
            if choice and not self._save_prompts():
                return
        self.root.destroy()

    def select_work(self) -> None:
        entered_url = self.url.get().strip()
        if not entered_url:
            messagebox.showwarning("缺少網址", "請先輸入作品首頁網址。", parent=self.root)
            return
        try:
            extractor = SyosetuWorkExtractor(catalog_root=self.output_directory)
            work_url, requested_chapter = extractor.normalize_selection_url(entered_url)
            self._clear_work_data(keep_url=True)
            self.url.set(work_url)
            self._selected_input_url = work_url
            self._requested_chapter = requested_chapter
            service = self._initial_work_service()
            self._apply_state("selecting_work", "正在讀取作品資料……")
            self.worker.start_select_work(
                extractor,
                service,
                work_url,
                requested_chapter,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)

    def refresh_catalog(self) -> None:
        entered_url = self.work.source_url if self.work is not None else self.url.get().strip()
        if not entered_url:
            messagebox.showwarning("缺少網址", "請先輸入作品首頁或章節網址。", parent=self.root)
            return
        if not messagebox.askyesno(
            "重新整理完整目錄",
            "這會重新讀取作品的所有目錄頁，長篇作品可能需要一段時間。是否繼續？",
            parent=self.root,
        ):
            return
        try:
            work_url, url_chapter = SyosetuWorkExtractor.normalize_selection_url(entered_url)
            requested = self.chapter_number.get().strip()
            requested_chapter = (
                int(requested) if requested.isascii() and requested.isdigit() else url_chapter
            )
            extractor = SyosetuWorkExtractor(catalog_root=self.output_directory)
            service = self._initial_work_service()
            self._requested_chapter = requested_chapter
            self.url.set(work_url)
            self._selected_input_url = work_url
            self._apply_state("selecting_work", "正在重新整理完整章節目錄……")
            self.worker.start_select_work(
                extractor,
                service,
                work_url,
                requested_chapter,
                force_refresh=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)

    def analyze_chapter(self) -> None:
        if self.work is None:
            messagebox.showwarning(
                "尚未選擇作品", "請先輸入作品首頁網址並選擇作品。", parent=self.root
            )
            return
        raw_number = self.chapter_number.get().strip()
        if not raw_number:
            messagebox.showwarning("缺少章節", "請輸入章節數字。", parent=self.root)
            return
        if not raw_number.isascii() or not raw_number.isdecimal() or int(raw_number) <= 0:
            messagebox.showwarning(
                "章節格式錯誤", "章節必須是大於 0 的半形整數。", parent=self.root
            )
            return
        try:
            chapter = self.work.get_chapter(int(raw_number))
        except ChapterNotFoundError:
            messagebox.showerror(
                "章節不存在",
                f"這部作品不存在第 {raw_number} 章，請輸入其他章節。",
                parent=self.root,
            )
            return

        selected = self.mode.get()
        analysis_mode: RunMode = "analysis"
        try:
            self._analyzed_mode = selected
            self.controller = build_translation_controller(
                config=self.config,
                output_directory=self.output_directory,
                checkpoint_directory=self.checkpoint_directory,
                mode=analysis_mode,
                auto_open=False,
                prompt_path=self.prompt_store.path_for("chapter"),
                translated_work_title=(
                    self.work_result.work.translated_title if self.work_result else None
                ),
            )
            self.plan = None
            self.result = None
            self.summary.set("正在讀取並分析章節……")
            self._apply_state("analyzing_chapter", "正在擷取並分析章節……")
            self.worker.start_prepare(self.controller, chapter.source_url)
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)

    def start(self) -> None:
        if self.plan is None or self.controller is None:
            return
        selected = self.mode.get()
        if selected != self._analyzed_mode:
            messagebox.showwarning(
                "模式已變更", "執行模式已變更，請重新分析章節。", parent=self.root
            )
            return
        if selected == "local":
            messagebox.showinfo(
                "本地模型尚未完成",
                "本地模型翻譯功能尚待開發，請先切換至 Gemini。",
                parent=self.root,
            )
            return
        overwrite_outputs = False
        if self.work_result is not None and self.work is not None:
            chapter = self.work.get_chapter(int(self.chapter_number.get()))
            output_paths = self.chapter_tracker.output_paths(self.work_result, chapter)
            existing_count = sum(path.exists() for path in output_paths)
            if existing_count:
                description = "完整" if existing_count == 2 else "部分"
                if not messagebox.askyesno(
                    "章節輸出已存在",
                    f"第 {chapter.number} 章已有{description}輸出。\n\n"
                    "繼續會覆蓋現有 TXT 與 HTML，是否繼續？",
                    parent=self.root,
                ):
                    return
                overwrite_outputs = True
        if selected == "gemini":
            if not messagebox.askyesno(
                "確認使用 Gemini",
                f"本次正文、逐 chunk 專有名詞分析與章節名稱最多呼叫 API "
                f"{self.plan.pending_count + self.plan.total_chunks + 1} 次。"
                "確定開始翻譯嗎？",
                parent=self.root,
            ):
                return
            api_key = self._get_or_request_api_key()
            if not api_key:
                return
            try:
                self.controller = build_translation_controller(
                    config=self.config,
                    output_directory=self.output_directory,
                    checkpoint_directory=self.checkpoint_directory,
                    mode="gemini",
                    auto_open=False,
                    overwrite_outputs=overwrite_outputs,
                    api_key=api_key,
                    prompt_path=self.prompt_store.path_for("chapter"),
                    translated_work_title=(
                        self.work_result.work.translated_title if self.work_result else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._show_error(exc)
                return
        self._apply_state("translating", "正在翻譯……")
        self.worker.start_run(
            self.controller,
            self.plan,
            self.chapter_tracker if self.work_result is not None else None,
            self.work_result,
        )

    def cancel(self) -> None:
        self.worker.cancel()
        if self.state == "translating":
            self.status.set("正在等待目前的 API 請求完成，之後將停止……")
        else:
            self.status.set("正在等待目前工作停止……")

    def open_html(self) -> None:
        local = self._local_output_paths()
        if local is None:
            return
        txt_path, html_path = local
        if txt_path.exists():
            try:
                matches = HtmlFormatter.txt_matches_html(txt_path, html_path)
            except (OSError, UnicodeError, ValueError) as exc:
                if not messagebox.askyesno(
                    "無法比較 TXT 與 HTML",
                    f"無法確認兩個檔案的正文是否相同：\n{exc}\n\n仍要直接開啟現有 HTML 嗎？",
                    parent=self.root,
                ):
                    return
                matches = True
            if not matches and messagebox.askyesno(
                "TXT 與 HTML 內容不同",
                "本機 TXT 與 HTML 的正文不同。是否用 TXT 內容覆蓋 HTML？\n\n"
                "選擇「否」會直接開啟目前的 HTML。",
                parent=self.root,
            ):
                try:
                    HtmlFormatter(auto_open=False, overwrite=True).save(
                        self._chapter_for_html(local),
                        html_path.parent,
                        txt_path=txt_path,
                        destination_path=html_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._show_error(exc)
                    return
        os.startfile(html_path.resolve())

    def _local_output_paths(self) -> tuple[Path, Path] | None:
        if self.work_result is not None and self.work is not None:
            raw_number = self.chapter_number.get().strip()
            if raw_number.isascii() and raw_number.isdigit():
                try:
                    chapter = self.work.get_chapter(int(raw_number))
                except ChapterNotFoundError:
                    pass
                else:
                    paths = self.chapter_tracker.existing_output_paths(self.work_result, chapter)
                    if paths[1].exists():
                        return paths
        if self.result is None:
            return None
        txt_path = next(
            (path for path in self.result.output_paths if path.suffix.lower() == ".txt"), None
        )
        html_path = next(
            (path for path in self.result.output_paths if path.suffix.lower() == ".html"), None
        )
        if txt_path is None or html_path is None or not html_path.exists():
            return None
        return txt_path, html_path

    def _chapter_for_html(self, local: tuple[Path, Path]) -> TranslatedChapter:
        if self.result is not None:
            result_paths = {path.resolve() for path in self.result.output_paths}
            if local[1].resolve() in result_paths:
                return self.result.chapter
        if self.work_result is None or self.work is None:
            raise ValueError("缺少章節資料，無法用 TXT 更新 HTML。")
        entry = self.work.get_chapter(int(self.chapter_number.get()))
        source = NovelChapter(
            self.work.title, entry.title, entry.source_url, "僅用於重新產生 HTML。"
        )
        placeholder = TextChunk(0, "僅用於重新產生 HTML。")
        return TranslatedChapter(
            source,
            (TranslatedChunk(placeholder, "HTML 正文由 TXT 讀取。"),),
            self.work_result.work.provider,
            self.work_result.work.model,
            translated_work_title=self._read_txt_heading(local[0], "作品："),
            translated_chapter_title=self._read_txt_heading(local[0], "章節："),
        )

    @staticmethod
    def _read_txt_heading(path: Path, prefix: str) -> str:
        for line in path.read_text(encoding="utf-8-sig").splitlines()[:8]:
            if line.startswith(prefix) and line[len(prefix) :].strip():
                return line[len(prefix) :].strip()
        raise ValueError(f"TXT 缺少必要欄位：{prefix}")

    def open_folder(self) -> None:
        if self.result is not None and self.result.output_paths:
            directory = self.result.output_paths[0].parent
        elif self.work_result is not None:
            directory = self.work_result.work_directory
        else:
            directory = self.output_directory
        os.startfile(directory.resolve())

    def open_terms(self) -> None:
        if self.work_result is None:
            return
        try:
            path = self.term_memory_store.ensure(self.work_result.work_directory)
            os.startfile(path.resolve())
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)

    def organize_terms(self) -> None:
        if self.work_result is None:
            return
        api_key = self._get_or_request_api_key()
        if not api_key:
            return
        try:
            service = build_term_organization_service(config=self.config, api_key=api_key)
            batches = service.prepare_batches(self.work_result.work_directory)
            requests = len(batches)
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)
            return
        if requests == 0:
            messagebox.showinfo(
                "無需整理",
                "目前專有名詞不足兩筆，無需整理。",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "整理專有名詞",
            f"將分批分析完整專有名詞記憶，預計呼叫 API {requests} 次。\n\n"
            "每一批分析完成後都會先顯示預覽，由你決定套用或略過。是否繼續？",
            parent=self.root,
        ):
            return
        self._term_organization_service = service
        self._term_organization_batches = batches
        self._term_organization_batch_index = 0
        self._term_organization_applied_batches = 0
        self._term_organization_skipped_batches = 0
        self._state_before_term_organization = self.state
        self._apply_state("organizing_terms", "正在讀取完整專有名詞記憶……")
        self._start_next_term_organization_batch()

    def _start_next_term_organization_batch(self) -> None:
        if self.work_result is None or self._term_organization_service is None:
            self._show_error(ValueError("缺少作品或整理服務，無法繼續整理。"))
            return
        if self._term_organization_batch_index >= len(self._term_organization_batches):
            self._finish_term_organization()
            return
        batch = self._term_organization_batches[self._term_organization_batch_index]
        self._term_organization_batch_index += 1
        self.status.set(f"正在分析第 {batch.number}/{batch.total} 批專有名詞……")
        self.worker.start_organize_terms(
            self._term_organization_service,
            self.work_result.work_directory,
            batch,
        )

    def _finish_term_organization(self, *, stopped: bool = False) -> None:
        applied = self._term_organization_applied_batches
        skipped = self._term_organization_skipped_batches
        self._term_organization_service = None
        self._term_organization_batches = ()
        self._term_organization_batch_index = 0
        self._update_work_details()
        status = "已停止專有名詞整理。" if stopped else "專有名詞整理完成。"
        self._apply_state(self._state_before_term_organization, status)
        title = "已停止整理" if stopped else "整理完成"
        messagebox.showinfo(
            title,
            f"已套用 {applied} 批，略過 {skipped} 批。"
            + ("\n先前已套用的變更會保留。" if stopped and applied else ""),
            parent=self.root,
        )

    def _confirm_term_organization(
        self,
        plan: TermOrganizationPlan,
    ) -> Literal["apply", "skip", "stop"]:
        decision: list[Literal["apply", "skip", "stop"]] = ["stop"]
        dialog = Toplevel(self.root)
        dialog.title(f"專有名詞整理預覽（第 {plan.batch_number}/{plan.total_batches} 批）")
        dialog.geometry("680x520")
        dialog.minsize(520, 360)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text="這次只處理目前批次；套用或略過後才會進入下一批。",
            padding=(14, 14, 14, 8),
        ).grid(row=0, column=0, sticky="ew")
        preview = ScrolledText(dialog, wrap="word", font=("Microsoft JhengHei UI", 10))
        preview.grid(row=1, column=0, sticky="nsew", padx=14)
        preview.insert("1.0", plan.preview_text())
        preview.configure(state="disabled")
        actions = ttk.Frame(dialog, padding=14)
        actions.grid(row=2, column=0, sticky="e")

        def accept() -> None:
            decision[0] = "apply"
            dialog.destroy()

        def skip() -> None:
            decision[0] = "skip"
            dialog.destroy()

        ttk.Button(actions, text="套用並繼續", command=accept).pack(side="left")
        ttk.Button(actions, text="略過此批", command=skip).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="停止整理", command=dialog.destroy).pack(
            side="left",
            padx=(8, 0),
        )
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()
        return decision[0]

    def _initial_work_service(self) -> WorkSetupService:
        selected = self.mode.get()
        load_environment(self.app_directory / ".env")
        api_key = get_api_key(self.config)
        if selected == "gemini" and api_key:
            return build_work_setup_service(
                config=self.config,
                output_directory=self.output_directory,
                api_key=api_key,
                prompt_path=self.prompt_store.path_for("work_metadata"),
            )
        return WorkSetupService(self.output_directory, None)

    def _gemini_work_service(self) -> WorkSetupService | None:
        api_key = self._get_or_request_api_key()
        if not api_key:
            return None
        return build_work_setup_service(
            config=self.config,
            output_directory=self.output_directory,
            api_key=api_key,
            prompt_path=self.prompt_store.path_for("work_metadata"),
        )

    def _get_or_request_api_key(self) -> str | None:
        load_environment(self.app_directory / ".env")
        existing = get_api_key(self.config)
        if existing:
            return existing
        entered = simpledialog.askstring(
            "Gemini API Key",
            "請輸入 API Key（內容會儲存在本機 .env）：",
            show="*",
            parent=self.root,
        )
        if entered is None or not entered.strip():
            return None
        return save_api_key(self.config, entered, env_path=self.app_directory / ".env")

    def _poll_messages(self) -> None:
        try:
            while True:
                self._handle_message(self.worker.messages.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def _handle_message(self, message: WorkerMessage) -> None:
        if message.kind == "work_progress":
            self.status.set(_WORK_PROGRESS_TEXT.get(message.payload, "正在準備作品資料……"))
            return
        if message.kind == "catalog_progress":
            completed, total, source = message.payload
            if source == "cache":
                self.status.set("正在使用快取檢查最新章節……")
            else:
                self.status.set(f"正在同步第 {completed}/{total} 頁章節目錄……")
            return
        if message.kind == "work_found":
            self.work = message.payload
            self.japanese_work.set(f"日文作品名：{self.work.title}")
            self.chinese_work.set("中文作品名：正在準備……")
            self.work_details.set(f"作者：{self.work.author}　章節數：{len(self.work.chapters):,}")
            return
        if message.kind == "api_key_required":
            self.work = message.payload
            if self.mode.get() == "local":
                self._apply_state("idle", "本地模型模式找不到既有作品記憶。")
                messagebox.showwarning(
                    "本地模型尚未完成",
                    "此作品尚無翻譯記憶，而本地模型功能仍待開發。請切換至 Gemini。",
                    parent=self.root,
                )
                return
            service = self._gemini_work_service()
            if service is None:
                self._apply_state("idle", "已取消建立作品翻譯記憶。")
                return
            self._apply_state("selecting_work", "正在翻譯作品名稱與摘要……")
            self.worker.start_setup_work(service, self.work)
            return
        if message.kind == "work_overwrite_required":
            self.work = message.payload
            if not messagebox.askyesno(
                "摘要已被修改",
                "作品摘要已有更新，但 synopsis.txt 曾被手動修改。\n\n"
                "繼續會以新的翻譯覆蓋現有內容，是否覆蓋？",
                parent=self.root,
            ):
                self._apply_state("idle", "已取消覆蓋 synopsis.txt。")
                return
            service = self._service_for_resume()
            if service is None:
                self._apply_state("idle", "已取消建立作品翻譯記憶。")
                return
            self._apply_state("selecting_work", "正在翻譯作品名稱與摘要……")
            self.worker.start_setup_work(service, self.work, allow_overwrite=True)
            return
        if message.kind == "work_setup_done":
            self.work_result = message.payload
            self.work = self.work_result.work.source_work
            self._reload_local_work_choices()
            self.chinese_work.set(f"中文作品名：{self.work_result.work.translated_title}")
            try:
                self.chapter_progress = self.chapter_tracker.reconcile(self.work_result)
            except Exception as exc:  # noqa: BLE001
                self._show_error(exc)
                return
            all_translated = self._set_default_chapter()
            if self._requested_chapter is not None:
                self.chapter_number.set(str(self._requested_chapter))
            self._update_work_details()
            self.summary.set("請確認章節數字，再按「分析章節」。")
            if all_translated:
                source = "此作品目前所有章節皆已有翻譯輸出。"
            else:
                source = (
                    "已載入作品記憶。" if self.work_result.reused_memory else "作品資料已建立。"
                )
            self._apply_state("work_ready", source)
            return
        if message.kind == "prepare_done":
            self.plan = message.payload
            self._show_prepared_plan(self.plan)
            return
        if message.kind == "completion_progress":
            self.status.set("正在驗證輸出並記錄章節完成狀態……")
            return
        if message.kind == "chapter_progress":
            self.chapter_progress = message.payload
            self._update_work_details()
            return
        if message.kind == "progress":
            update: ProgressUpdate = message.payload
            if update.source == "translating":
                self.status.set(f"翻譯第{update.completed}/{update.total}個chunk中")
            elif update.source == "checkpoint":
                self.status.set(f"已載入第{update.completed}/{update.total}個chunk的Checkpoint")
            elif update.source == "analyzing_terms":
                self.status.set(
                    f"正在分析第{update.completed}/{update.total}個chunk的新專有名詞……"
                )
            elif update.source == "updating_terms":
                self.status.set("正在驗證並更新專有名詞……")
            elif update.source == "translating_title":
                self.status.set("正在翻譯章節名稱……")
            else:
                self.status.set(f"第{update.completed}/{update.total}個chunk已翻譯並存檔")
            return
        if message.kind == "run_done":
            self.result = message.payload
            if self.chapter_progress is not None:
                self.chapter_number.set(str(self.chapter_progress.next_number))
            self.plan = None
            self.controller = None
            term_update = self.result.term_memory_update
            term_summary = (
                f"新增專有名詞：{len(term_update.added)} 筆。"
                if term_update is not None
                else ""
            )
            self.summary.set(
                "翻譯與輸出完成。"
                + (f"{term_summary}\n" if term_summary else "")
                + "請分析下一個章節。"
            )
            status = (
                "所有章節皆已完成翻譯。"
                if self.chapter_progress is not None and self.chapter_progress.all_completed
                else "翻譯完成，已填入下一個未完成章節。"
            )
            self._apply_state("work_ready", status)
            if self.result.term_memory_error:
                messagebox.showwarning(
                    "專有名詞記憶未更新",
                    "正文翻譯與輸出已完成，但專有名詞記憶更新失敗：\n\n"
                    f"{self.result.term_memory_error}",
                    parent=self.root,
                )
            return
        if message.kind == "term_organization_progress":
            current = self._term_organization_batch_index
            total = len(self._term_organization_batches)
            prefix = f"第 {current}/{total} 批：" if total else ""
            labels = {
                "scanning": f"{prefix}正在讀取專有名詞……",
                "requesting": f"{prefix}正在請 Gemini 判斷整理方式……",
                "validating": f"{prefix}正在驗證整理建議……",
            }
            self.status.set(labels.get(message.payload, "正在整理專有名詞……"))
            return
        if message.kind == "term_organization_done":
            plan: TermOrganizationPlan = message.payload
            if not plan.changed:
                self.status.set(
                    f"第 {plan.batch_number}/{plan.total_batches} 批無需變更，"
                    "繼續下一批……"
                )
                self.root.after(10, self._start_next_term_organization_batch)
                return
            decision = self._confirm_term_organization(plan)
            if decision == "stop":
                self._finish_term_organization(stopped=True)
                return
            if decision == "skip":
                self._term_organization_skipped_batches += 1
                self.status.set(
                    f"已略過第 {plan.batch_number}/{plan.total_batches} 批，"
                    "繼續下一批……"
                )
                self.root.after(10, self._start_next_term_organization_batch)
                return
            if self.work_result is None or self._term_organization_service is None:
                self._show_error(ValueError("缺少作品或整理服務，無法套用整理結果。"))
                return
            self.status.set(
                f"正在套用第 {plan.batch_number}/{plan.total_batches} 批整理結果……"
            )
            self.worker.start_apply_term_organization(
                self._term_organization_service,
                self.work_result.work_directory,
                plan,
            )
            return
        if message.kind == "term_organization_applied":
            self._term_organization_applied_batches += 1
            self._update_work_details()
            self.status.set("本批已更新 terms.json，繼續下一批……")
            self.root.after(10, self._start_next_term_organization_batch)
            return
        if message.kind == "cancelled":
            if self.state == "translating":
                status = "已取消；完成的區塊已保留在 Checkpoint。"
                next_state: AppState = "chapter_ready"
            elif self.state == "organizing_terms":
                status = "已停止整理；先前已套用的批次會保留。"
                next_state = self._state_before_term_organization
                self._term_organization_service = None
                self._term_organization_batches = ()
                self._term_organization_batch_index = 0
            elif self.work_result is not None:
                status = "已取消目前工作。"
                next_state = "work_ready"
            else:
                status = "已取消選擇作品。"
                next_state = "idle"
            self._apply_state(next_state, status)
            return
        if message.kind == "error":
            self._show_error(message.payload)

    def _service_for_resume(self) -> WorkSetupService | None:
        if self.mode.get() == "local":
            messagebox.showwarning("本地模型尚未完成", "請先切換至 Gemini。", parent=self.root)
            return None
        return self._gemini_work_service()

    def _show_prepared_plan(self, plan: TranslationPlan) -> None:
        source = plan.source_chapter
        number = int(source.source_url.rstrip("/").rsplit("/", 1)[-1])
        completion_status = "尚未翻譯"
        if self.chapter_progress is not None:
            if number in self.chapter_progress.partial_numbers:
                completion_status = "部分輸出"
            elif number in self.chapter_progress.completed_numbers:
                completion_status = "已完成"
                if self.work_result is not None:
                    completion = self.chapter_tracker.completion_for(self.work_result, number)
                    if (
                        completion is not None
                        and completion.source_hash
                        and completion.source_hash != text_hash(source.original_text)
                    ):
                        completion_status = "已完成，但網站原文已有變更"
        self.summary.set(
            f"章節：{source.chapter_title}\n"
            f"完成狀態：{completion_status}\n"
            f"原文字數：{len(source.original_text):,}\n"
            f"總區塊：{plan.total_chunks}\n"
            f"Checkpoint 已完成：{plan.completed_count}\n"
            f"本次待翻譯：{plan.pending_count}\n"
            f"本章套用專有名詞：{len(plan.matched_terms)}\n"
            f"模型：{self._selected_model_label()}"
        )
        self._apply_state("chapter_ready", "分析完成。")

    def _set_default_chapter(self) -> bool:
        if self.work is None or self.chapter_progress is None:
            return False
        self.chapter_number.set(str(self.chapter_progress.next_number))
        return self.chapter_progress.all_completed

    def _update_work_details(self) -> None:
        if self.work is None:
            return
        details = f"作者：{self.work.author}　章節數：{len(self.work.chapters):,}"
        if self.work_result is not None:
            try:
                term_count = len(self.term_memory_store.load(self.work_result.work_directory))
            except (OSError, UnicodeError, TermMemoryError):
                term_count = 0
            details += f"　專有名詞：{term_count:,} 筆"
        if self.chapter_progress is not None:
            next_chapter = (
                "無"
                if self.chapter_progress.all_completed
                else str(self.chapter_progress.next_number)
            )
            details += f"　下一個待翻譯章節：{next_chapter}"
        self.work_details.set(details)

    def _apply_state(self, state: AppState, status: str | None = None) -> None:
        self.state = state
        busy = state in {
            "selecting_work",
            "analyzing_chapter",
            "translating",
            "organizing_terms",
        }
        work_ready = state in {"work_ready", "chapter_ready"}
        self.url_entry.configure(state="disabled" if busy else "normal")
        self.select_work_button.configure(state="disabled" if busy else "normal")
        can_refresh_catalog = not busy and self.work_result is not None
        self.refresh_catalog_button.configure(state="normal" if can_refresh_catalog else "disabled")
        self.chapter_entry.configure(state="normal" if work_ready else "disabled")
        self.analyze_button.configure(state="normal" if work_ready else "disabled")
        can_start = state == "chapter_ready" and self.mode.get() == "gemini"
        self.start_button.configure(state="normal" if can_start else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        can_open_html = not busy and self._local_output_paths() is not None
        self.open_html_button.configure(state="normal" if can_open_html else "disabled")
        can_open_terms = not busy and self.work_result is not None
        self.open_terms_button.configure(state="normal" if can_open_terms else "disabled")
        self.organize_terms_button.configure(state="normal" if can_open_terms else "disabled")
        for button in self.mode_buttons:
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        if status is not None:
            self.status.set(status)

    def _on_url_changed(self, *_args: object) -> None:
        if self.work is not None and self.url.get().strip() != self._selected_input_url:
            self._clear_work_data(keep_url=True)
            self._apply_state("idle", "網址已變更，請重新選擇作品。")

    def _on_local_work_selected(self, _event: object) -> None:
        source_url = self._local_work_urls.get(self.url.get())
        if source_url is not None:
            self.url.set(source_url)

    def _reload_local_work_choices(self) -> None:
        self._local_work_urls = {
            option.label: option.source_url
            for option in discover_local_works(self.output_directory)
        }
        self.url_entry.configure(values=tuple(self._local_work_urls))

    def _on_chapter_changed(self, *_args: object) -> None:
        if self.state == "chapter_ready":
            self.plan = None
            self.result = None
            self.controller = None
            self.summary.set("章節數字已變更，請重新分析章節。")
            self.open_html_button.configure(state="disabled")
            self._apply_state("work_ready", "請分析新的章節。")
        elif self.state == "work_ready":
            self.result = None
            self._apply_state("work_ready")

    def _on_mode_changed(self) -> None:
        selected = self.mode.get()
        if self.plan is not None:
            self._analyzed_mode = selected
        if selected == "local":
            status = "已切換至本地模型；翻譯功能尚待開發。"
        else:
            status = "已切換至 Gemini。"
        self._apply_state(self.state, status)

    def _selected_model_label(self) -> str:
        if self.mode.get() == "local":
            return "本地模型（待開發）"
        return f"{self.config.provider} / {self.config.model}"

    def _clear_work_data(self, *, keep_url: bool) -> None:
        self.work = None
        self.work_result = None
        self.chapter_progress = None
        self.controller = None
        self.plan = None
        self.result = None
        self._analyzed_mode = None
        self._selected_input_url = ""
        self._requested_chapter = None
        self.chapter_number.set("")
        self.japanese_work.set("尚未選擇作品")
        self.chinese_work.set("")
        self.work_details.set("")
        self.summary.set("請先選擇作品，再輸入章節數字。")
        self.open_html_button.configure(state="disabled")
        if not keep_url:
            self.url.set("")

    def _show_error(self, exc: Exception) -> None:
        organizing_terms = self.state == "organizing_terms"
        if organizing_terms:
            self._term_organization_service = None
            self._term_organization_batches = ()
            self._term_organization_batch_index = 0
        next_state: AppState = "work_ready" if self.work_result is not None else "idle"
        self._apply_state(next_state, "發生錯誤。")
        if isinstance(exc, GeminiFreeTierQuotaError):
            lines = [
                "Gemini API 已達免費方案的使用上限，因此目前無法繼續整理。"
                if organizing_terms
                else "Gemini API 已達免費方案的使用上限，因此目前無法繼續。",
                "",
            ]
            if organizing_terms:
                lines.extend(("專有名詞檔案尚未修改，",))
            lines.append(
                "請等待額度重置後再試，或至 Google AI Studio "
                "檢查用量與付費方案。"
            )
            if exc.retry_after_seconds is not None:
                lines.extend(
                    ("", f"建議等待約 {exc.retry_after_seconds} 秒後再試。")
                )
            messagebox.showerror(
                "Gemini 免費額度已用完",
                "\n".join(lines),
                parent=self.root,
            )
            return
        messagebox.showerror("執行失敗", str(exc), parent=self.root)


__all__ = ["DesktopApp"]
