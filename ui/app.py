"""Tkinter desktop application for work selection and chapter translation."""

from __future__ import annotations

import os
import queue
import sys
from dataclasses import replace
from pathlib import Path
from tkinter import (
    END,
    BooleanVar,
    Canvas,
    StringVar,
    Text,
    Tk,
    Toplevel,
    messagebox,
    simpledialog,
    ttk,
)
from typing import Literal
from urllib.parse import urlsplit

from core.api_usage import DailyApiUsage, ObservedApiUsage, quota_date
from core.bootstrap import (
    RunMode,
    build_term_organization_service,
    build_translation_controller,
    build_work_setup_service,
)
from core.chapter_progress import ChapterCompletionTracker, ChapterProgress
from core.codex_usage import context_summary, duration_label, usage_summary
from core.codex_usage import events as codex_usage_events
from core.config import (
    AppConfig,
    get_api_key,
    load_config,
    load_environment,
    save_api_key,
    write_config,
)
from core.controller import (
    ChapterExecutionOptions,
    ProgressUpdate,
    TranslationController,
    TranslationPlan,
    TranslationResult,
)
from core.exceptions import (
    ChapterNotFoundError,
    GeminiFreeTierQuotaError,
    InvalidLlmResponseError,
    TermMemoryError,
    UnsupportedUrlError,
)
from core.local_works import discover_local_works
from core.models import NovelChapter, NovelWork, TextChunk, TranslatedChapter, TranslatedChunk
from core.output_migration import OutputMigrationResult, migrate_legacy_syosetu_outputs
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
from extractors.web_kakuyomu import KakuyomuExtractor
from extractors.web_kakuyomu_work import KakuyomuWorkExtractor
from extractors.web_syosetu_work import SyosetuWorkExtractor
from formatters import HtmlFormatter
from formatters.utils import TXT_BODY_SEPARATOR, atomic_write_text
from translators.codex_llm import CodexWorkTranslator
from ui.batch_dialog import BatchTranslationDialog
from ui.epub_dialog import EpubExportDialog
from ui.messages import WorkerMessage
from ui.widgets import CollapsibleSection, LoadingSpinner, ScrollableFrame
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

_GEMINI_MODEL_CHOICES = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
_LOCAL_MODEL_CHOICES = ("尚未提供本地模型",)
_SITE_LABELS = {
    "syosetu": "成為小說家吧",
    "kakuyomu": "Kakuyomu",
}
_SITE_KEYS = {label: key for key, label in _SITE_LABELS.items()}
_SITE_OUTPUT_FOLDERS = {"syosetu": "Syosetu", "kakuyomu": "Kakuyomu"}
_SITE_URL_HINTS = {
    "syosetu": "例如：https://ncode.syosetu.com/n0000aa/",
    "kakuyomu": "例如：https://kakuyomu.jp/works/12345678901234567890",
}
_SITE_HOSTS = {"syosetu": "ncode.syosetu.com", "kakuyomu": "kakuyomu.jp"}


class DesktopApp:
    """Own widgets and translate worker messages into main-thread UI updates."""

    def __init__(self, root: Tk, *, app_directory: Path | None = None) -> None:
        self.root = root
        self.app_directory = app_directory or get_app_dir()
        self.api_usage = DailyApiUsage(self.app_directory / "logs" / "api_usage.json")
        self._usage_date = quota_date()
        self.config: AppConfig = load_config(self.app_directory / "config.json")
        self.prompt_store = PromptTemplateStore(self.app_directory)
        self.prompt_store.ensure()
        self.output_root_directory, self.checkpoint_directory, _ = ensure_runtime_directories(
            self.config.output_directory, app_dir=self.app_directory
        )
        self.site = StringVar(value=_SITE_LABELS["syosetu"])
        self.site_hint = StringVar(value=_SITE_URL_HINTS["syosetu"])
        self.site_output_directories = {
            key: self.output_root_directory / folder for key, folder in _SITE_OUTPUT_FOLDERS.items()
        }
        for directory in self.site_output_directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        self.output_migration = migrate_legacy_syosetu_outputs(self.output_root_directory)
        self.output_directory = self.site_output_directories["syosetu"]
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
        self.model_name = StringVar(value=self.config.model)
        self.codex_effort = StringVar(value=self.config.codex_reasoning_effort)
        self._codex_models: list[dict] = []
        self._codex_catalog_requested = False
        self.api_usage_text = StringVar(value="本程式今日請求：0 次")
        self.retry_count = StringVar(value=str(self.config.retry_attempts))
        self.chapter_number = StringVar()
        self.chunk_size_text = StringVar(value=str(self.config.chunk_size))
        self.chunk_count_text = StringVar()
        self.mode = StringVar(value="gemini")
        self.japanese_work = StringVar(value="尚未選擇作品")
        self.chinese_work = StringVar(value="")
        self.work_details = StringVar(value="")
        self.summary = StringVar(value="請先選擇作品，再輸入章節數字。")
        self.status = StringVar(value="就緒")
        self.prompt_description = StringVar(value=PROMPT_TEMPLATES[0].description)
        self.translate_title = BooleanVar(value=self.config.translate_title)
        self.translate_body = BooleanVar(value=self.config.translate_body)
        self.update_terms = BooleanVar(value=self.config.update_terms and self.config.translate_body)

        self._configure_window()
        self._build_widgets()
        self.model_name.trace_add("write", lambda *_args: self._refresh_api_usage())
        self.mode.trace_add("write", lambda *_args: self._refresh_api_usage())
        self._refresh_api_usage()
        self.root.after(30000, self._check_usage_date)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.url.trace_add("write", self._on_url_changed)
        self.chapter_number.trace_add("write", self._on_chapter_changed)
        initial_status = (
            f"已將 {len(self.output_migration.moved)} 部舊作品移至 Syosetu 資料夾。"
            if self.output_migration.moved
            else None
        )
        self._apply_state("idle", initial_status)
        self.root.after(0, self._show_output_migration_issues)
        self.root.after(100, self._poll_messages)

    def _configure_window(self) -> None:
        self.root.title("日文小說繁體中文翻譯器")
        self.root.geometry("850x1050")
        self.root.minsize(720, 700)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

    def _show_output_migration_issues(self) -> None:
        """Report migration conflicts without merging or overwriting data."""
        result: OutputMigrationResult = self.output_migration
        messages: list[str] = []
        if result.conflicts:
            folders = "\n".join(f"- {name}" for name in result.conflicts)
            messages.append("下列舊作品與 outputs/Syosetu 內的資料夾同名，未進行搬移：\n" + folders)
        if result.failures:
            failures = "\n".join(f"- {failure}" for failure in result.failures)
            messages.append("下列舊作品搬移失敗：\n" + failures)
        if messages:
            messagebox.showerror("舊作品遷移失敗", "\n\n".join(messages), parent=self.root)

    def _build_widgets(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")
        translation_page = ttk.Frame(notebook)
        prompt_page = ttk.Frame(notebook)
        notebook.add(translation_page, text="翻譯")
        notebook.add(prompt_page, text="Prompt 模板")
        if not getattr(sys, "frozen", False):
            from developer.page import DeveloperPage

            self.developer_page = DeveloperPage(
                notebook,
                is_busy=lambda: self.worker.is_running or self.state not in {
                    "idle", "work_ready", "chapter_ready",
                },
            )
            notebook.add(self.developer_page, text="開發者工具")
        translation_page.columnconfigure(0, weight=1)
        translation_page.rowconfigure(0, weight=1)

        self.translation_scroll = ScrollableFrame(translation_page)
        self.translation_scroll.grid(row=0, column=0, sticky="nsew")
        outer = self.translation_scroll.content
        outer.columnconfigure(0, weight=1)
        ttk.Label(
            outer, text="日文小說繁體中文翻譯器", font=("Microsoft JhengHei UI", 18, "bold")
        ).grid(row=0, column=0, sticky="w", pady=(0, 14))

        mode_section = CollapsibleSection(outer, text="執行模式")
        mode_section.grid(row=1, column=0, sticky="ew")
        mode_frame = mode_section.content
        modes = (
            ("Gemini API", "gemini"),
            ("Codex", "codex"),
            ("本地（待開發）", "local"),
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

        self.codex_login_button = ttk.Button(
            mode_frame, text="登入 Codex", command=lambda: self._codex_auth(True)
        )
        self.codex_login_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.codex_check_button = ttk.Button(
            mode_frame, text="更新模型／檢查 Codex 登入", command=lambda: self._codex_auth(False)
        )
        self.codex_check_button.grid(row=1, column=1, sticky="w", pady=(8, 0))

        site_section = CollapsibleSection(outer, text="小說網站")
        site_section.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        site_frame = site_section.content
        self.site_buttons: list[ttk.Radiobutton] = []
        for column, label in enumerate(_SITE_LABELS.values()):
            button = ttk.Radiobutton(
                site_frame,
                text=label,
                value=label,
                variable=self.site,
                command=self._on_site_changed,
            )
            button.grid(row=0, column=column, sticky="w", padx=(0, 18))
            self.site_buttons.append(button)

        self.model_frame = CollapsibleSection(outer, text="Gemini API 模型")
        self.model_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        model_content = self.model_frame.content
        model_content.columnconfigure(1, weight=1)
        ttk.Label(model_content, text="模型：").grid(row=0, column=0, sticky="w")
        self.model_entry = ttk.Combobox(
            model_content,
            textvariable=self.model_name,
            values=self._available_model_choices(),
            state="normal",
        )
        self.model_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.api_usage_label = ttk.Label(model_content, textvariable=self.api_usage_text)
        self.api_usage_label.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.model_entry.bind("<<ComboboxSelected>>", self._on_model_committed)
        self.model_entry.bind("<Return>", self._on_model_committed)
        self.model_entry.bind("<FocusOut>", self._on_model_committed)
        self.retry_label = ttk.Label(model_content, text="失敗後重試次數：")
        self.retry_label.grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.retry_entry = ttk.Spinbox(
            model_content,
            from_=0,
            to=10,
            textvariable=self.retry_count,
            width=6,
            command=self._on_retry_committed,
        )
        self.retry_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self.retry_entry.bind("<Return>", self._on_model_committed)
        self.retry_entry.bind("<FocusOut>", self._on_model_committed)
        self.model_hint_label = ttk.Label(
            model_content, foreground="#666666", wraplength=650, justify="left"
        )
        self.model_hint_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.codex_effort_frame = ttk.Frame(model_content)
        self.codex_effort_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(self.codex_effort_frame, text="推理強度：").pack(side="left")
        self.codex_effort_entry = ttk.Combobox(
            self.codex_effort_frame, textvariable=self.codex_effort,
            values=(), state="readonly", width=12,
        )
        self.codex_effort_entry.pack(side="left", padx=(8, 12))
        self.codex_effort_entry.bind("<<ComboboxSelected>>", self._on_model_committed)
        ttk.Label(self.codex_effort_frame,
                  text="選項依模型支援程度提供，並分別記住各模型的設定。",
                  wraplength=440, justify="left").pack(side="left")

        url_section = CollapsibleSection(outer, text="作品首頁")
        url_section.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        url_frame = url_section.content
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
            textvariable=self.site_hint,
            wraplength=650,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        work_section = CollapsibleSection(outer, text="作品資訊")
        work_section.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        work_frame = work_section.content
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
        self.export_epub_button = ttk.Button(
            work_frame, text="匯出 EPUB／上傳 Kindle", command=self.export_epub
        )
        self.export_epub_button.pack(anchor="w", pady=(8, 0))
        self.batch_button = ttk.Button(work_frame, text="批次翻譯", command=self.open_batch)
        self.batch_button.pack(anchor="w", pady=(8, 0))
        self._codex_usage_records = []
        self.codex_usage_text = StringVar(value="尚無本次請求紀錄")
        self.codex_limits_text = StringVar(value="帳號額度：尚未讀取")
        self.codex_context_text = StringVar(value=context_summary(None))
        self.codex_usage_frame = ttk.LabelFrame(outer, text="Codex 對話與用量", padding=8)
        ttk.Label(self.codex_usage_frame, textvariable=self.codex_limits_text,
                  wraplength=650, justify="left").pack(anchor="w")
        ttk.Label(self.codex_usage_frame, textvariable=self.codex_context_text,
                  wraplength=650, justify="left").pack(anchor="w", pady=(4, 4))
        ttk.Label(self.codex_usage_frame, textvariable=self.codex_usage_text,
                  wraplength=650, justify="left").pack(anchor="w")
        self.codex_compact_button = ttk.Button(self.codex_usage_frame, text="壓縮對話記憶",
                                               command=lambda: self.codex_maintenance(True))
        self.codex_compact_button.pack(side="left")
        self.codex_usage_refresh = ttk.Button(self.codex_usage_frame, text="重新整理用量",
                                              command=lambda: self.codex_maintenance(False))
        self.codex_usage_refresh.pack(side="left", padx=8)
        ttk.Button(self.codex_usage_frame, text="本次逐步用量",
                   command=self.show_codex_usage).pack(side="left")

        chapter_section = CollapsibleSection(outer, text="章節選擇")
        chapter_section.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        chapter_frame = chapter_section.content
        ttk.Label(chapter_frame, text="章節數字：").grid(row=0, column=0, sticky="w")
        self.chapter_entry = ttk.Entry(chapter_frame, textvariable=self.chapter_number, width=12)
        self.chapter_entry.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.analyze_button = ttk.Button(
            chapter_frame, text="分析章節", command=self.analyze_chapter
        )
        self.analyze_button.grid(row=0, column=2, sticky="w")
        ttk.Label(chapter_frame, text="Chunk 字元上限：").grid(
            row=0, column=3, sticky="w", padx=(12, 4)
        )
        self.chunk_size_entry = ttk.Entry(
            chapter_frame, textvariable=self.chunk_size_text, width=8
        )
        self.chunk_size_entry.grid(row=0, column=4, sticky="w")
        self.chunk_size_entry.bind("<Return>", lambda _event: self._commit_chunk_size())
        self.chunk_size_entry.bind("<FocusOut>", lambda _event: self._commit_chunk_size())

        summary_section = CollapsibleSection(
            outer, text="章節資訊", padding=(12, 12, 12, 18)
        )
        summary_section.grid(row=8, column=0, sticky="ew", pady=(12, 0))
        summary_frame = summary_section.content
        ttk.Label(
            summary_frame,
            textvariable=self.summary,
            justify="left",
            wraplength=750,
            padding=(0, 2, 0, 8),
        ).pack(anchor="nw", fill="both", expand=True)

        resegment_frame = ttk.Frame(summary_frame)
        resegment_frame.pack(anchor="w", pady=(8, 0))
        ttk.Label(resegment_frame, text="Chunk 數量：").pack(side="left")
        self.chunk_count_entry = ttk.Entry(
            resegment_frame, textvariable=self.chunk_count_text, width=8
        )
        self.chunk_count_entry.pack(side="left", padx=(0, 8))
        self.resegment_button = ttk.Button(
            resegment_frame, text="重新分析", command=self.resegment_chapter
        )
        self.resegment_button.pack(side="left")
        ttk.Label(
            summary_frame,
            text="指定數量為 1～20；重新分段不發送模型請求。",
            foreground="#666666",
        ).pack(anchor="w", pady=(6, 0))

        execution_section = CollapsibleSection(outer, text="本章處理項目")
        execution_section.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        execution_frame = execution_section.content
        self.translate_title_check = ttk.Checkbutton(
            execution_frame,
            text="翻譯章節標題",
            variable=self.translate_title,
        )
        self.translate_title_check.grid(row=0, column=0, sticky="w", padx=(0, 18))
        self.translate_body_check = ttk.Checkbutton(
            execution_frame,
            text="翻譯章節內文",
            variable=self.translate_body,
            command=self._on_translation_selection_changed,
        )
        self.translate_body_check.grid(row=0, column=1, sticky="w", padx=(0, 18))
        self.update_terms_check = ttk.Checkbutton(
            execution_frame,
            text="分析並更新專有名詞記憶",
            variable=self.update_terms,
        )
        self.update_terms_check.grid(row=0, column=2, sticky="w")
        self.execution_hint_label = ttk.Label(
            execution_frame, foreground="#666666", wraplength=650, justify="left"
        )
        self.execution_hint_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        footer = ttk.Frame(translation_page, padding=(20, 8, 20, 16))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        progress_frame = ttk.Frame(footer)
        progress_frame.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        progress_frame.columnconfigure(1, weight=1)
        self.spinner = LoadingSpinner(progress_frame)
        self.spinner.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.spinner.grid_remove()
        ttk.Label(progress_frame, textvariable=self.status).grid(row=0, column=1, sticky="w")

        actions = ttk.Frame(footer)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
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
        self.open_terms_button = ttk.Button(actions, text="開啟專有名詞", command=self.open_terms)
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
        self._term_organization_service = None
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

    def _on_close(self) -> None:
        batch = getattr(self, "_batch_dialog", None)
        if batch is not None and batch.busy:
            messagebox.showwarning("工作執行中", "請先取消批次翻譯，再關閉程式。", parent=self.root)
            return
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
        if not self._commit_model_selection():
            return
        entered_url = self.url.get().strip()
        if not entered_url:
            messagebox.showwarning("缺少網址", "請先輸入作品首頁網址。", parent=self.root)
            return
        try:
            self._validate_selected_site_url(entered_url)
            extractor = self._work_extractor()
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
        if not self._commit_model_selection():
            return
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
            self._validate_selected_site_url(entered_url)
            extractor = self._work_extractor()
            work_url, url_chapter = extractor.normalize_selection_url(entered_url)
            requested = self.chapter_number.get().strip()
            requested_chapter = (
                int(requested) if requested.isascii() and requested.isdigit() else url_chapter
            )
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
        if not self._commit_chunk_size():
            return
        if not self._commit_model_selection():
            return
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
        analysis_mode: RunMode = "codex_analysis" if selected == "codex" else "analysis"
        try:
            self._reset_execution_options()
            self._analyzed_mode = selected
            self.controller = build_translation_controller(
                config=self.config,
                output_directory=self.output_directory,
                checkpoint_directory=self.checkpoint_directory,
                mode=analysis_mode,
                work_directory=self.work_result.work_directory if self.work_result else None,
                auto_open=False,
                prompt_path=self.prompt_store.path_for("chapter"),
                translated_work_title=(
                    self.work_result.work.translated_title if self.work_result else None
                ),
                chapter_extractor=self._chapter_extractor(int(raw_number)),
                usage=self.api_usage,
            )
            self.plan = None
            self.result = None
            self.summary.set("正在讀取並分析章節……")
            self._apply_state("analyzing_chapter", "正在擷取並分析章節……")
            self.worker.start_prepare(self.controller, chapter.source_url)
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)

    def start(self) -> None:
        if not self._commit_chunk_size():
            return
        if not self._commit_model_selection():
            return
        if self.plan is None or self.controller is None:
            return
        if self.chunk_count_text.get().strip() != str(self.plan.total_chunks):
            messagebox.showwarning(
                "尚未重新分析", "Chunk 數量已修改，請先按章節資訊中的「重新分析」。", parent=self.root
            )
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
                "本地模型翻譯功能尚待開發，請先切換至 Gemini API 或 Codex。",
                parent=self.root,
            )
            return
        options = self._execution_options()
        overwrite_outputs = False
        if self.work_result is not None and self.work is not None:
            chapter = self.work.get_chapter(int(self.chapter_number.get()))
            output_paths = self.chapter_tracker.output_paths(self.work_result, chapter)
            existing_count = sum(path.exists() for path in output_paths)
            if existing_count:
                description = "完整" if existing_count == 2 else "部分"
                if not messagebox.askyesno(
                    "重新執行章節",
                    f"第 {chapter.number} 章已有{description}輸出。\n\n"
                    "繼續會覆蓋現有 TXT 與 HTML，並只清除本次勾選項目的 "
                    "Checkpoint。是否繼續？",
                    parent=self.root,
                ):
                    return
                overwrite_outputs = True
        if options.requires_api:
            pending_chunks = (
                self.plan.total_chunks
                if overwrite_outputs and options.translate_body
                else self.plan.pending_count if options.translate_body else 0
            )
            title_requests = (
                1
                if overwrite_outputs and options.translate_title
                else self.plan.pending_title_count if options.translate_title else 0
            )
            term_requests = (
                self.plan.total_chunks
                if overwrite_outputs and options.update_terms
                else self.plan.pending_term_count if options.update_terms else 0
            )
            api_jobs = title_requests + pending_chunks + term_requests
            attempts_per_job = 1 if selected == "codex" else self.config.retry_attempts + 1
            request_limit = api_jobs * attempts_per_job
            confirmation = (
                f"模型：{self.config.codex_model}；推理強度：{self.config.codex_reasoning_effort}\n"
                f"Codex：標題 {title_requests} 回合、正文 {pending_chunks} 回合。\n"
                "沿用本機 ChatGPT 登入及本作品對話，使用你的 Codex 額度。\n\n是否繼續？"
            ) if selected == "codex" else (
                f"章節名稱：{title_requests} 個 API 工作\n"
                f"正文翻譯：{pending_chunks} 個 API 工作\n"
                f"專有名詞分析：{term_requests} 個 API 工作\n\n"
                f"合計：{api_jobs} 個 API 工作。\n"
                f"每個工作最多嘗試 {attempts_per_job} 次"
                f"（首次 1 次＋重試 {self.config.retry_attempts} 次）。\n"
                f"程式可能送出的實際請求上限：{request_limit} 次。\n\n"
                "是否繼續？"
            )
            if not messagebox.askyesno(
                "確認使用 Codex" if selected == "codex" else "確認使用 Gemini API",
                confirmation,
                parent=self.root,
            ):
                return
            api_key = None if selected == "codex" else self._get_or_request_api_key()
            if not api_key and selected != "codex":
                return
        else:
            api_key = None
        try:
            self.controller = build_translation_controller(
                config=self.config,
                output_directory=self.output_directory,
                checkpoint_directory=self.checkpoint_directory,
                mode=("codex" if options.requires_api else "codex_analysis")
                if selected == "codex" else ("gemini" if options.requires_api else "analysis"),
                work_directory=self.work_result.work_directory if self.work_result else None,
                auto_open=False,
                overwrite_outputs=overwrite_outputs,
                api_key=api_key,
                prompt_path=self.prompt_store.path_for("chapter"),
                translated_work_title=(
                    self.work_result.work.translated_title if self.work_result else None
                ),
                chapter_extractor=self._chapter_extractor(int(self.chapter_number.get())),
                usage=ObservedApiUsage(
                    self.api_usage,
                    lambda: self.worker.messages.put(
                        WorkerMessage("translation_api_started", options)
                    ),
                    count_requests=selected != "codex",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)
            return
        if overwrite_outputs:
            try:
                self.controller.clear_checkpoint_components(self.plan, options)
                self.plan = replace(
                    self.plan,
                    completed_chunks=(
                        () if options.translate_body else self.plan.completed_chunks
                    ),
                    translated_chapter_title=(
                        None
                        if options.translate_title
                        else self.plan.translated_chapter_title
                    ),
                    completed_term_indexes=(
                        frozenset()
                        if options.update_terms
                        else self.plan.completed_term_indexes
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._show_error(exc)
                return
        self._codex_usage_records = []
        self._apply_state("translating", "正在翻譯……")
        self.worker.start_run(
            self.controller,
            self.plan,
            self.chapter_tracker if self.work_result is not None else None,
            self.work_result,
            options=options,
        )

    def cancel(self) -> None:
        batch = getattr(self, "_batch_dialog", None)
        if batch is not None and batch.busy:
            batch.cancel()
            return
        self.worker.cancel()
        if self.state == "translating":
            self.status.set("正在等待目前請求結束，之後將停止……")
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
        self._offer_chapter_completion_update(local)
        os.startfile(html_path.resolve())

    def _offer_chapter_completion_update(self, local: tuple[Path, Path]) -> None:
        """Optionally reconcile a non-complete chapter after opening its HTML."""
        if self.work_result is None:
            return
        chapter_number = self._output_chapter_number(local)
        if chapter_number is None:
            return
        if (
            self.chapter_progress is not None
            and chapter_number in self.chapter_progress.completed_numbers
        ):
            return
        if not messagebox.askyesno(
            "更新章節狀態",
            f"第 {chapter_number} 章目前不是已完成狀態。\n\n"
            "是否重新檢查輸出並將符合條件的章節更新為已完成？",
            parent=self.root,
        ):
            return
        try:
            progress = self.chapter_tracker.reconcile(self.work_result)
        except Exception as exc:  # noqa: BLE001
            self._show_error(exc)
            return
        self.chapter_progress = progress
        self._update_work_details()
        if chapter_number in progress.completed_numbers:
            self.summary.set(f"第 {chapter_number} 章已更新為已完成。")
            self.status.set(f"第 {chapter_number} 章狀態已更新。")
            return
        messagebox.showwarning(
            "章節仍未完成",
            "TXT 與 HTML 尚未符合完成條件。請確認 TXT 正文不是空白，"
            "並先用 TXT 更新 HTML。",
            parent=self.root,
        )

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

    def export_epub(self) -> None:
        if self.work_result is None or self.state not in {"work_ready", "chapter_ready"}:
            return
        raw = self.chapter_number.get().strip()
        initial = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else 1
        EpubExportDialog(self.root, self.work_result, initial_chapter=initial)

    def open_batch(self) -> None:
        if self.work_result is None or self.state not in {"work_ready", "chapter_ready"}:
            return
        if self.mode.get() not in {"gemini", "codex"}:
            return
        if not self._commit_chunk_size() or not self._commit_model_selection():
            return
        self._batch_dialog = BatchTranslationDialog(self)

    def show_codex_usage(self):
        self._show_llm_output(usage_summary(self._codex_usage_records), self.root)

    def codex_maintenance(self, compact):
        if self.mode.get() != "codex" or self.work_result is None or self.state not in {"work_ready", "chapter_ready"}:
            return
        if self.worker.is_running:
            return
        if compact and not messagebox.askyesno("壓縮對話記憶",
                "壓縮可能消耗額度並遺漏上下文細節。是否壓縮目前作品的 Codex 任務？", parent=self.root):
            return
        if compact:
            self._codex_usage_records = []
        self._apply_state("selecting_work", "正在壓縮……" if compact else "正在查詢用量……")
        self.cancel_button.configure(state="disabled")
        self.worker.start_codex_maintenance(self.work_result.work_directory,
                                            self.config.codex_model, compact=compact)

    def _show_codex_limits(self, data):
        from datetime import UTC, datetime
        if not data:
            self.codex_limits_text.set("帳號額度：無法取得")
            return
        buckets = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits") or {}}
        parts = []
        for name, bucket in buckets.items():
            for key in ("primary", "secondary"):
                window = bucket.get(key) or {}
                if window.get("usedPercent") is not None:
                    reset = window.get("resetsAt")
                    when = datetime.fromtimestamp(reset).astimezone().strftime("%m/%d %H:%M") if reset else "未知"
                    duration = window.get('windowDurationMins')
                    label = duration_label(duration)
                    remaining = max(0, min(100, 100 - window['usedPercent']))
                    parts.append(f"{name} {label}：剩餘 {remaining:g}%，重置 {when}")
        stamp = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
        self.codex_limits_text.set(("\n".join(parts) or "帳號額度：無法取得") + f"\n更新時間：{stamp}")

    def _drain_codex_usage(self):
        while True:
            try:
                entry = codex_usage_events.get_nowait()
            except queue.Empty:
                return
            if "warning" in entry:
                self.codex_usage_text.set(entry["warning"])
                continue
            if self.work_result is not None and entry.get("directory") != str(self.work_result.work_directory.resolve()):
                continue
            if entry.get("status") == "sending":
                self._show_codex_limits(entry.get("before"))
                continue
            self._codex_usage_records = [r for r in self._codex_usage_records if r.get("id") != entry.get("id")]
            self._codex_usage_records.append(entry)
            self._show_codex_limits(entry.get("after"))
            self.codex_context_text.set(context_summary(entry.get("context_estimate")))
            text = "上一步：" + usage_summary([entry])
            self.codex_usage_text.set(text)

    def _chapter_for_html(self, local: tuple[Path, Path]) -> TranslatedChapter:
        base_source: NovelChapter
        default_provider: str
        default_model: str
        if self.result is not None:
            result_paths = {path.resolve() for path in self.result.output_paths}
            if local[1].resolve() in result_paths:
                base_source = self.result.chapter.source_chapter
                default_provider = self.result.chapter.provider
                default_model = self.result.chapter.model
            else:
                base_source, default_provider, default_model = self._loaded_chapter_for_html()
        else:
            base_source, default_provider, default_model = self._loaded_chapter_for_html()

        provider, model = self._read_txt_translation_engine(
            local[0],
            default_provider=default_provider,
            default_model=default_model,
        )
        source = NovelChapter(
            title=self._read_txt_heading(local[0], "日文作品名："),
            chapter_title=self._read_txt_heading(local[0], "日文章節名："),
            source_url=self._read_txt_heading(local[0], "來源："),
            original_text="僅用於重新產生 HTML。",
            chapter_number=base_source.chapter_number,
        )
        placeholder = TextChunk(0, "僅用於重新產生 HTML。")
        return TranslatedChapter(
            source,
            (TranslatedChunk(placeholder, "HTML 正文由 TXT 讀取。"),),
            provider,
            model,
            translated_work_title=self._read_txt_heading(local[0], "作品："),
            translated_chapter_title=self._read_txt_heading(local[0], "章節："),
        )

    def _loaded_chapter_for_html(self) -> tuple[NovelChapter, str, str]:
        if self.work_result is None or self.work is None:
            raise ValueError("缺少章節資料，無法用 TXT 更新 HTML。")
        entry = self.work.get_chapter(int(self.chapter_number.get()))
        source = NovelChapter(
            self.work.title,
            entry.title,
            entry.source_url,
            "僅用於重新產生 HTML。",
            chapter_number=entry.number,
        )
        return source, self.work_result.work.provider, self.work_result.work.model

    def _output_chapter_number(self, local: tuple[Path, Path]) -> int | None:
        """Return the unpadded chapter number represented by an output pair."""
        if self.result is not None:
            result_paths = {path.resolve() for path in self.result.output_paths}
            if local[1].resolve() in result_paths:
                result_number = self.result.chapter.source_chapter.chapter_number
                if result_number is not None:
                    return result_number
        if self.work is not None:
            raw = self.chapter_number.get().strip()
            if raw.isascii() and raw.isdigit():
                return int(raw)
        return None

    @staticmethod
    def _read_txt_heading(path: Path, prefix: str) -> str:
        for line in path.read_text(encoding="utf-8-sig").splitlines()[:8]:
            if line.startswith(prefix) and line[len(prefix) :].strip():
                return line[len(prefix) :].strip()
        raise ValueError(f"TXT 缺少必要欄位：{prefix}")

    @classmethod
    def _read_txt_translation_engine(
        cls,
        path: Path,
        *,
        default_provider: str,
        default_model: str,
    ) -> tuple[str, str]:
        engine = cls._read_txt_heading(path, "翻譯引擎：")
        provider, separator, model = engine.partition(" / ")
        if separator and provider.strip() and model.strip():
            return provider.strip(), model.strip()
        return default_provider, engine or default_model

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
        if not self._commit_model_selection():
            return
        if self.work_result is None:
            return
        api_key = self._get_or_request_api_key()
        if not api_key:
            return
        try:
            service = build_term_organization_service(
                config=self.config,
                api_key=api_key,
                prompt_path=self.prompt_store.path_for("term_organization"),
                usage=self.api_usage,
            )
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
            f"將分批分析完整專有名詞記憶，共 {requests} 個 API 工作。\n"
            f"每個工作最多嘗試 {self.config.retry_attempts + 1} 次"
            f"（首次 1 次＋重試 {self.config.retry_attempts} 次）。\n"
            f"程式可能送出的實際請求上限："
            f"{requests * (self.config.retry_attempts + 1)} 次。\n\n"
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
    ) -> tuple[Literal["apply", "skip", "stop"], TermOrganizationPlan | None]:
        result: list[tuple[Literal["apply", "skip", "stop"], TermOrganizationPlan | None]] = [
            ("stop", None)
        ]
        dialog = Toplevel(self.root)
        dialog.title(f"專有名詞整理預覽（第 {plan.batch_number}/{plan.total_batches} 批）")
        dialog.geometry("760x600")
        dialog.minsize(600, 420)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text=(
                "勾選這一批要新增或刪除的項目；只有勾選的變更會寫入 terms.json。"
                "套用或略過後才會進入下一批。"
            ),
            padding=(14, 14, 14, 8),
            wraplength=720,
        ).grid(row=0, column=0, sticky="ew")

        list_frame = ttk.Frame(dialog)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=14)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        canvas = Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        choices = ttk.Frame(canvas, padding=(8, 6))
        choices.columnconfigure(0, weight=1)
        window = canvas.create_window((0, 0), window=choices, anchor="nw")
        choices.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )

        addition_vars: dict[str, BooleanVar] = {}
        removal_vars: dict[str, BooleanVar] = {}
        row = 0
        ttk.Label(
            choices,
            text=f"新增項目（{len(plan.added)}）",
            font=("Microsoft JhengHei UI", 11, "bold"),
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1
        if plan.added:
            for source, translation in plan.added.items():
                variable = BooleanVar(value=True)
                addition_vars[source] = variable
                ttk.Checkbutton(
                    choices,
                    text=f"{source} → {translation}",
                    variable=variable,
                ).grid(row=row, column=0, sticky="w", pady=2)
                row += 1
        else:
            ttk.Label(choices, text="沒有新增項目。", foreground="#666666").grid(
                row=row, column=0, sticky="w"
            )
            row += 1

        ttk.Separator(choices).grid(row=row, column=0, sticky="ew", pady=10)
        row += 1
        ttk.Label(
            choices,
            text=f"刪除項目（{len(plan.removed)}）",
            font=("Microsoft JhengHei UI", 11, "bold"),
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1
        if plan.removed:
            for source, translation in plan.removed.items():
                variable = BooleanVar(value=True)
                removal_vars[source] = variable
                ttk.Checkbutton(
                    choices,
                    text=f"{source} → {translation}",
                    variable=variable,
                ).grid(row=row, column=0, sticky="w", pady=2)
                row += 1
        else:
            ttk.Label(choices, text="沒有刪除項目。", foreground="#666666").grid(
                row=row, column=0, sticky="w"
            )

        actions = ttk.Frame(dialog, padding=14)
        actions.grid(row=2, column=0, sticky="ew")

        def set_all(selected: bool) -> None:
            for variable in (*addition_vars.values(), *removal_vars.values()):
                variable.set(selected)

        def accept() -> None:
            additions = {source for source, variable in addition_vars.items() if variable.get()}
            removals = {source for source, variable in removal_vars.items() if variable.get()}
            if not additions and not removals:
                messagebox.showwarning(
                    "尚未勾選變更",
                    "請至少勾選一個新增或刪除項目，或選擇「略過此批」。",
                    parent=dialog,
                )
                return
            result[0] = (
                "apply",
                plan.select_changes(additions=additions, removals=removals),
            )
            dialog.destroy()

        def skip() -> None:
            result[0] = ("skip", None)
            dialog.destroy()

        ttk.Button(actions, text="全選", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(actions, text="全部取消", command=lambda: set_all(False)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="停止整理", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="略過此批", command=skip).pack(
            side="right",
            padx=(8, 0),
        )
        ttk.Button(actions, text="套用勾選項目並繼續", command=accept).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()
        return result[0]

    def _initial_work_service(self) -> WorkSetupService:
        selected = self.mode.get()
        if selected == "codex":
            return self._codex_work_service()
        load_environment(self.app_directory / ".env")
        api_key = get_api_key(self.config)
        if selected == "gemini" and api_key:
            return build_work_setup_service(
                config=self.config,
                output_directory=self.output_directory,
                api_key=api_key,
                prompt_path=self.prompt_store.path_for("work_metadata"),
                usage=self.api_usage,
            )
        return WorkSetupService(self.output_directory, None)

    def _codex_work_service(self) -> WorkSetupService:
        return WorkSetupService(
            self.output_directory,
            CodexWorkTranslator(
                self.output_directory, model=self.config.codex_model,
                reasoning_effort=self.config.codex_reasoning_effort,
                prompt_path=self.prompt_store.path_for("work_metadata"),
            ),
        )

    def _codex_auth(self, sign_in: bool) -> None:
        if self.worker.is_running:
            return
        self._apply_state("selecting_work", "請完成瀏覽器登入……" if sign_in else "正在檢查 Codex 登入……")
        self.worker.start_codex_auth(self.app_directory, sign_in=sign_in)

    def _gemini_work_service(self) -> WorkSetupService | None:
        if self.mode.get() == "codex":
            return self._codex_work_service()
        api_key = self._get_or_request_api_key()
        if not api_key:
            return None
        return build_work_setup_service(
            config=self.config,
            output_directory=self.output_directory,
            api_key=api_key,
            prompt_path=self.prompt_store.path_for("work_metadata"),
            usage=self.api_usage,
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
        self._drain_codex_usage()
        try:
            while True:
                self._handle_message(self.worker.messages.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_messages)

    def _handle_message(self, message: WorkerMessage) -> None:
        if message.kind == "codex_maintenance_done":
            compact, limits = message.payload
            self._drain_codex_usage()
            if not compact:
                self._show_codex_limits(limits)
            self._apply_state("work_ready", "壓縮完成。" if compact else "用量已更新。")
            return
        if message.kind == "codex_auth_done":
            account = message.payload
            self._codex_models = account.get("models", [])
            if self.mode.get() == "codex":
                choices = [m["model"] for m in self._codex_models]
                if choices:
                    if self.config.codex_model not in choices:
                        self.model_name.set(choices[0])
                    self._commit_model_selection()
            label = account.get("email") or "ChatGPT"
            state: AppState = "chapter_ready" if self.plan else "work_ready" if self.work_result else "idle"
            self._apply_state(state, f"Codex 已登入：{label}（{account.get('planType', '')}）；已載入 {len(self._codex_models)} 個模型。")
            return
        if message.kind == "translation_api_started":
            self._save_execution_options(message.payload)
            return
        if message.kind in {
            "run_done", "error", "cancelled", "work_setup_done",
            "term_organization_done", "term_organization_applied",
        }:
            self._refresh_api_usage()
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
            if self._selected_site_key() == "kakuyomu" and self._requested_chapter is not None:
                selected_number = KakuyomuWorkExtractor.chapter_number_for_episode(
                    self.work, self._requested_chapter
                )
                if selected_number is not None:
                    self._requested_chapter = selected_number
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
                    "此作品尚無翻譯記憶，而本地模型功能仍待開發。請切換至 Gemini API 或 Codex。",
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
            self.codex_context_text.set(context_summary(None))
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
            if self.mode.get() == "codex":
                self.root.after(100, lambda: self.codex_maintenance(False))
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
            if update.source == "translating_title":
                self.status.set("正在翻譯章節名稱……")
            elif update.source == "translating":
                self.status.set(f"翻譯第{update.completed}/{update.total}個chunk中")
            elif update.source == "checkpoint":
                self.status.set(f"已載入第{update.completed}/{update.total}個chunk的Checkpoint")
            elif update.source == "analyzing_terms":
                self.status.set(f"正在分析第{update.completed}/{update.total}個chunk的新專有名詞……")
            elif update.source == "updating_terms":
                self.status.set("正在驗證並更新專有名詞……")
            elif update.source == "term_checkpoint":
                self.status.set(
                    f"已確認第{update.completed}/{update.total}個chunk的專有名詞Checkpoint"
                )
            else:
                self.status.set(f"第{update.completed}/{update.total}個chunk已翻譯並存檔")
            return
        if message.kind == "run_done":
            self._drain_codex_usage()
            if self.mode.get() == "codex":
                self.codex_usage_text.set(usage_summary(self._codex_usage_records))
            self.result = message.payload
            completed_number = self._output_chapter_number(
                (
                    next(
                        path for path in self.result.output_paths if path.suffix.lower() == ".txt"
                    ),
                    next(
                        path for path in self.result.output_paths if path.suffix.lower() == ".html"
                    ),
                )
            )
            completed_label = (
                f"第 {completed_number} 章" if completed_number is not None else "本章"
            )
            if self.chapter_progress is not None:
                self.chapter_number.set(str(self.chapter_progress.next_number))
            self.plan = None
            self.controller = None
            term_update = self.result.term_memory_update
            term_summary = (
                f"新增專有名詞：{len(term_update.added)} 筆。" if term_update is not None else ""
            )
            self.summary.set(
                f"{completed_label}翻譯與輸出完成。"
                + (f"{term_summary}\n" if term_summary else "")
                + "請分析下一個章節。"
            )
            status = (
                f"{completed_label}翻譯完成；所有章節皆已完成翻譯。"
                if self.chapter_progress is not None and self.chapter_progress.all_completed
                else f"{completed_label}翻譯完成，已填入下一個未完成章節。"
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
                    f"第 {plan.batch_number}/{plan.total_batches} 批無需變更，繼續下一批……"
                )
                self.root.after(10, self._start_next_term_organization_batch)
                return
            decision, selected_plan = self._confirm_term_organization(plan)
            if decision == "stop":
                self._finish_term_organization(stopped=True)
                return
            if decision == "skip":
                self._term_organization_skipped_batches += 1
                self.status.set(
                    f"已略過第 {plan.batch_number}/{plan.total_batches} 批，繼續下一批……"
                )
                self.root.after(10, self._start_next_term_organization_batch)
                return
            if self.work_result is None or self._term_organization_service is None:
                self._show_error(ValueError("缺少作品或整理服務，無法套用整理結果。"))
                return
            if selected_plan is None:
                self._show_error(ValueError("缺少已勾選的整理結果，無法套用。"))
                return
            self.status.set(f"正在套用第 {plan.batch_number}/{plan.total_batches} 批整理結果……")
            self.worker.start_apply_term_organization(
                self._term_organization_service,
                self.work_result.work_directory,
                selected_plan,
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
            messagebox.showwarning("本地模型尚未完成", "請先切換至 Gemini API 或 Codex。", parent=self.root)
            return None
        return self._gemini_work_service()

    def _show_prepared_plan(self, plan: TranslationPlan) -> None:
        self.chunk_count_text.set(str(plan.total_chunks))
        source = plan.source_chapter
        if source.chapter_number is None:
            raise ValueError("章節缺少有效的顯示序號。")
        number = source.chapter_number
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
            f"標題 Checkpoint：{'已完成' if plan.translated_chapter_title else '未完成'}\n"
            f"Checkpoint 已完成：{plan.completed_count}\n"
            f"內文尚未完成區塊：{plan.pending_count}\n"
            + (f"專有名詞 Checkpoint：{len(plan.completed_term_indexes)}/{plan.total_chunks}\n"
               if self.mode.get() != "codex" else "")
            + f"本章套用專有名詞：{len(plan.matched_terms)}\n"
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
        if hasattr(self, "codex_usage_frame"):
            if self.mode.get() == "codex":
                self.codex_usage_frame.grid(row=6, column=0, sticky="ew", pady=(12, 0))
            else:
                self.codex_usage_frame.grid_remove()
            ready = work_ready and self.work_result is not None
            self.codex_usage_refresh.configure(state="normal" if ready else "disabled")
            has_session = ready and (self.work_result.work_directory / "codex_session.json").is_file()
            self.codex_compact_button.configure(state="normal" if has_session else "disabled")
        if hasattr(self, "batch_button"):
            self.batch_button.configure(state="normal" if work_ready and self.work_result is not None
                                        and self.mode.get() in {"gemini", "codex"} else "disabled")
        if hasattr(self, "export_epub_button"):
            self.export_epub_button.configure(
                state="normal" if work_ready and self.work_result is not None else "disabled"
            )
        for button in self.site_buttons:
            button.configure(state="disabled" if busy else "normal")
        self._sync_model_selector(busy=busy)
        if hasattr(self, "developer_page"):
            self.developer_page.set_busy(busy)
        self.url_entry.configure(state="disabled" if busy else "normal")
        self.select_work_button.configure(state="disabled" if busy else "normal")
        can_refresh_catalog = not busy and self.work_result is not None
        self.refresh_catalog_button.configure(state="normal" if can_refresh_catalog else "disabled")
        self.chapter_entry.configure(state="normal" if work_ready else "disabled")
        self.chunk_size_entry.configure(state="disabled" if busy else "normal")
        resegment_state = "normal" if state == "chapter_ready" else "disabled"
        self.chunk_count_entry.configure(state=resegment_state)
        self.resegment_button.configure(state=resegment_state)
        self.analyze_button.configure(state="normal" if work_ready else "disabled")
        selection_state = "normal" if state == "chapter_ready" else "disabled"
        self.translate_title_check.configure(state=selection_state)
        self.translate_body_check.configure(state=selection_state)
        terms_state = (
            "normal"
            if state == "chapter_ready" and self.translate_body.get() and self.mode.get() != "codex"
            else "disabled"
        )
        self.update_terms_check.configure(state=terms_state)
        can_start = state == "chapter_ready" and self.mode.get() in {"gemini", "codex"}
        self.start_button.configure(state="normal" if can_start else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        local_outputs = self._local_output_paths()
        can_open_html = not busy and local_outputs is not None
        open_number = (
            self._output_chapter_number(local_outputs) if local_outputs is not None else None
        )
        self.open_html_button.configure(
            text=f"開啟第 {open_number} 章 HTML" if open_number is not None else "開啟 HTML"
        )
        self.open_html_button.configure(state="normal" if can_open_html else "disabled")
        can_open_terms = not busy and self.work_result is not None
        self.open_terms_button.configure(state="normal" if can_open_terms else "disabled")
        self.organize_terms_button.configure(
            state="normal" if can_open_terms and self.mode.get() != "codex" else "disabled"
        )
        for auth_button in (self.codex_login_button, self.codex_check_button):
            auth_button.configure(state="normal" if not busy and self.mode.get() == "codex" else "disabled")
            if self.mode.get() == "codex":
                auth_button.grid()
            else:
                auth_button.grid_remove()
        if self.mode.get() == "codex":
            self.update_terms_check.grid_remove()
            self.organize_terms_button.pack_forget()
        else:
            self.update_terms_check.grid()
            self.organize_terms_button.pack(side="right", padx=(0, 8))
        if self.mode.get() == "local":
            self.start_button.pack_forget()
        else:
            self.start_button.pack(side="left", before=self.cancel_button)
        for button in self.mode_buttons:
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        if status is not None:
            self.status.set(status)

    def _reset_execution_options(self) -> None:
        self.translate_title.set(self.config.translate_title)
        self.translate_body.set(self.config.translate_body)
        self.update_terms.set(
            self.config.update_terms and self.config.translate_body and self.mode.get() != "codex"
        )

    def _commit_chunk_size(self) -> bool:
        raw = self.chunk_size_text.get().strip()
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 9 or int(raw) <= 0:
            messagebox.showwarning(
                "Chunk 大小錯誤", "字元上限必須是大於 0 的半形整數（最多 9 位）。", parent=self.root
            )
            return False
        size = int(raw)
        if size == self.config.chunk_size:
            return True
        updated = replace(self.config, chunk_size=size)
        try:
            write_config(updated, self.app_directory / "config.json")
        except OSError as exc:
            self._show_error(exc)
            return False
        self.config = updated
        if self.plan is not None:
            self.plan = None
            self.controller = None
            self.result = None
            self.chunk_count_text.set("")
            self.summary.set("Chunk 字元上限已變更，請重新分析章節。")
            self._apply_state("work_ready", "請重新分析章節。")
        return True

    def resegment_chapter(self) -> None:
        if self.state != "chapter_ready" or self.plan is None or self.controller is None:
            return
        if not self._commit_chunk_size() or self.plan is None:
            return
        raw = self.chunk_count_text.get().strip()
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 9 or not 1 <= int(raw) <= 20:
            messagebox.showwarning(
                "Chunk 數量錯誤", "Chunk 數量必須是 1 到 20 的半形整數。", parent=self.root
            )
            return
        try:
            plan = self.controller.resegment(self.plan, int(raw))
        except Exception as exc:  # noqa: BLE001 - keep the previous valid plan.
            messagebox.showerror("重新分段失敗", str(exc), parent=self.root)
            return
        self.plan = plan
        self.result = None
        self._show_prepared_plan(plan)

    def _save_execution_options(self, options: ChapterExecutionOptions) -> None:
        updated = replace(
            self.config,
            translate_title=options.translate_title,
            translate_body=options.translate_body,
            update_terms=options.update_terms,
        )
        try:
            write_config(updated, self.app_directory / "config.json")
        except OSError as exc:
            messagebox.showwarning(
                "無法儲存翻譯選項", f"翻譯會繼續，但無法儲存下次預設：{exc}", parent=self.root
            )
        self.config = updated
        self._reset_execution_options()

    def _on_translation_selection_changed(self) -> None:
        if not self.translate_body.get() or self.mode.get() == "codex":
            self.update_terms.set(False)
        state = "normal" if (self.state == "chapter_ready" and self.translate_body.get()
                             and self.mode.get() != "codex") else "disabled"
        self.update_terms_check.configure(state=state)

    def _execution_options(self) -> ChapterExecutionOptions:
        return ChapterExecutionOptions(
            translate_title=self.translate_title.get(),
            translate_body=self.translate_body.get(),
            update_terms=self.update_terms.get() and self.mode.get() != "codex",
        )

    def _on_url_changed(self, *_args: object) -> None:
        if self.work is not None and self.url.get().strip() != self._selected_input_url:
            self._clear_work_data(keep_url=True)
            self._apply_state("idle", "網址已變更，請重新選擇作品。")

    def _selected_site_key(self) -> str:
        """Return the internal key for the selected website."""
        return _SITE_KEYS.get(self.site.get(), "syosetu")

    def _on_site_changed(self, _event: object | None = None) -> None:
        """Reset work state and switch all work storage to the selected website."""
        if self.worker.is_running:
            return
        site_key = self._selected_site_key()
        self.output_directory = self.site_output_directories[site_key]
        self.site_hint.set(_SITE_URL_HINTS[site_key])
        self._clear_work_data(keep_url=False)
        self._reload_local_work_choices()
        self._apply_state("idle", f"已切換至 {_SITE_LABELS[site_key]}。")

    def _work_extractor(self) -> SyosetuWorkExtractor | KakuyomuWorkExtractor:
        if self._selected_site_key() == "kakuyomu":
            return KakuyomuWorkExtractor(catalog_root=self.output_directory)
        return SyosetuWorkExtractor(catalog_root=self.output_directory)

    def _validate_selected_site_url(self, url: str) -> None:
        """Explain a known cross-site mismatch before format validation."""
        try:
            hostname = urlsplit(url.strip()).hostname
        except ValueError:
            return
        actual_site = next(
            (key for key, expected_host in _SITE_HOSTS.items() if hostname == expected_host),
            None,
        )
        selected_site = self._selected_site_key()
        if actual_site is not None and actual_site != selected_site:
            raise UnsupportedUrlError(
                f"這是 {_SITE_LABELS[actual_site]} 網址，請先切換至"
                f"「{_SITE_LABELS[actual_site]}」網站按鈕。"
            )

    def _chapter_extractor(self, chapter_number: int) -> KakuyomuExtractor | None:
        if self._selected_site_key() == "kakuyomu":
            return KakuyomuExtractor(
                chapter_number=chapter_number,
                expected_work_title=self.work.title if self.work is not None else None,
            )
        return None

    def _on_local_work_selected(self, _event: object) -> None:
        source_url = self._local_work_urls.get(self.url.get())
        if source_url is not None:
            self.url.set(source_url)

    def _on_model_committed(self, _event: object) -> None:
        self._commit_model_selection()

    def _on_retry_committed(self) -> None:
        self._commit_model_selection()

    def _available_model_choices(self) -> tuple[str, ...]:
        if self.mode.get() == "codex":
            models = tuple(m["model"] for m in getattr(self, "_codex_models", []))
            return tuple(dict.fromkeys(m for m in models if m and m != "default"))
        choices = (*_GEMINI_MODEL_CHOICES, *self.config.saved_models, self.config.model)
        return tuple(dict.fromkeys(choice for choice in choices if choice))

    def _available_codex_efforts(self, model: str) -> tuple[str, ...]:
        catalog = getattr(self, "_codex_models", [])
        entries = [m for m in catalog if m["model"] == model]
        supported = [
            {e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])}
            for m in entries
        ]
        common = set.intersection(*supported) if supported else set()
        order = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
        return tuple(effort for effort in order if effort in common)

    def _commit_model_selection(self) -> bool:
        if self.mode.get() == "local":
            return True
        if self.mode.get() == "codex":
            selected = self.model_name.get().strip()
            if selected not in self._available_model_choices():
                messagebox.showwarning("請選擇模型", "請先更新模型清單，再從下拉選單選擇 Codex 模型。", parent=self.root)
                return False
            supported = self._available_codex_efforts(selected)
            if not supported:
                messagebox.showwarning("無法確認推理強度", "請先更新模型清單，並選擇有回報推理強度的模型。", parent=self.root)
                return False
            info: dict = next((m for m in getattr(self, "_codex_models", []) if m["model"] == selected), {})
            fallback = "low" if "low" in supported else info.get("defaultReasoningEffort")
            if fallback not in supported:
                fallback = supported[0]
            effort = (
                self.config.codex_model_efforts.get(selected, fallback)
                if selected != self.config.codex_model else self.codex_effort.get()
            )
            if (selected != self.config.codex_model or getattr(self, "_codex_models", [])) and effort not in self._available_codex_efforts(selected):
                effort = fallback
            efforts = dict(self.config.codex_model_efforts)
            efforts[selected] = effort
            if selected != self.config.codex_model or effort != self.config.codex_reasoning_effort or efforts != self.config.codex_model_efforts:
                updated = replace(self.config, codex_model=selected, codex_reasoning_effort=effort,
                                  codex_model_efforts=efforts)
                try:
                    write_config(updated, self.app_directory / "config.json")
                except OSError as exc:
                    self._show_error(exc)
                    return False
                self.config = updated
                self.codex_effort.set(effort)
                self.model_name.set(selected)
                self.plan = None
                self.controller = None
                self.result = None
                self._analyzed_mode = None
                self.summary.set("Codex 模型或推理強度已變更，請重新分析章節。")
                self._apply_state("work_ready" if self.work_result else "idle", "Codex 模型／推理強度已儲存，請重新分析章節。")
            return True
        selected = self.model_name.get().strip()
        if not selected:
            self.model_name.set(self.config.model)
            messagebox.showwarning(
                "模型不可空白",
                "請選擇或輸入完整的 Gemini Model ID。",
                parent=self.root,
            )
            return False
        retry_text = self.retry_count.get().strip()
        if not retry_text.isascii() or not retry_text.isdigit() or not 0 <= int(retry_text) <= 10:
            self.retry_count.set(str(self.config.retry_attempts))
            messagebox.showwarning(
                "重試次數錯誤",
                "失敗後重試次數必須是 0 到 10 的半形整數。",
                parent=self.root,
            )
            return False
        retry_attempts = int(retry_text)
        model_changed = selected != self.config.model
        retry_changed = retry_attempts != self.config.retry_attempts
        if not model_changed and not retry_changed:
            self.model_name.set(selected)
            self.retry_count.set(str(retry_attempts))
            return True

        saved_models = list(self.config.saved_models)
        for candidate in (self.config.model, selected):
            if (
                candidate
                and candidate not in _GEMINI_MODEL_CHOICES
                and candidate not in saved_models
            ):
                saved_models.append(candidate)
        updated = replace(
            self.config,
            model=selected,
            saved_models=tuple(saved_models),
            retry_attempts=retry_attempts,
        )
        try:
            write_config(updated, self.app_directory / "config.json")
        except OSError as exc:
            self.model_name.set(self.config.model)
            self.retry_count.set(str(self.config.retry_attempts))
            messagebox.showerror(
                "模型儲存失敗",
                f"無法寫入 config.json：\n{exc}",
                parent=self.root,
            )
            return False

        self.config = updated
        self.model_name.set(selected)
        self.retry_count.set(str(retry_attempts))
        self.model_entry.configure(values=self._available_model_choices())
        self._term_organization_service = None
        if model_changed and (self.plan is not None or self.controller is not None):
            self.plan = None
            self.controller = None
            self.result = None
            self._analyzed_mode = None
            if self.work_result is not None:
                self.summary.set("模型已變更，請重新分析章節後再開始翻譯。")
                self._apply_state("work_ready", f"模型已儲存：{selected}；請重新分析章節。")
            else:
                self._apply_state("idle", f"模型已儲存：{selected}")
        else:
            saved = (
                f"模型已儲存：{selected}；重試 {retry_attempts} 次。"
                if model_changed
                else f"重試次數已儲存：{retry_attempts}"
            )
            self._apply_state(self.state, saved)
        return True

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
        if selected == "codex" and not self._codex_catalog_requested:
            self._codex_catalog_requested = True
            self.root.after(0, self._auto_load_codex_models)
        previous = self._analyzed_mode
        self.model_name.set(
            _LOCAL_MODEL_CHOICES[0] if selected == "local" else
            (self.config.codex_model if self.config.codex_model != "default" else "") if selected == "codex" else self.config.model
        )
        if selected == "codex":
            self.codex_effort.set(self.config.codex_model_efforts.get(
                self.config.codex_model, self.config.codex_reasoning_effort))
        if self.plan is not None and (selected == "codex" or previous == "codex"):
            self.plan = None
            self.controller = None
            self.result = None
            self._analyzed_mode = None
            self._reset_execution_options()
            self._apply_state("work_ready" if self.work_result else "idle", "翻譯引擎已變更，請重新分析章節。")
            return
        if self.plan is not None:
            self._analyzed_mode = selected
        if selected == "local":
            status = "已切換至本地模型；翻譯功能尚待開發。"
        elif selected == "codex":
            self.update_terms.set(False)
            status = "已切換至 Codex；請選擇模型與推理強度。"
        else:
            status = "已切換至 Gemini API。"
        self._apply_state(self.state, status)

    def _auto_load_codex_models(self) -> None:
        if self.mode.get() == "codex" and not self.worker.is_running:
            self._codex_auth(False)

    def _sync_model_selector(self, *, busy: bool) -> None:
        """Show model choices belonging to the active execution mode."""
        mode = self.mode.get()
        if hasattr(self, "model_hint_label"):
            hints = {
                "gemini": (
                    "可輸入完整 Model ID；選擇、按 Enter 或離開欄位時自動儲存。"
                    "可重試的失敗最多重試 0～10 次。"
                ),
                "codex": (
                    "請從下拉選單選擇 Codex 模型，選擇後自動儲存；"
                    "可按「更新模型／檢查 Codex 登入」重新取得清單。"
                ),
                "local": "本地模型功能尚待開發。",
            }
            self.model_hint_label.configure(text=hints[mode])
        if hasattr(self, "execution_hint_label"):
            text = "未勾選翻譯的標題或內文，會保留日文原文。"
            self.execution_hint_label.configure(text=text)
        if hasattr(self, "site_hint"):
            hint = (
                _SITE_URL_HINTS[self._selected_site_key()]
                + "\n可輸入作品或章節網址，也可從下拉選單選擇本地作品。"
            )
            if mode in {"gemini", "codex"}:
                engine = "Gemini API" if mode == "gemini" else "Codex"
                hint += f"\n需要建立作品名稱與摘要翻譯時，會使用 {engine} 額度。"
            self.site_hint.set(hint)
        if hasattr(self, "retry_label"):
            for widget in (self.retry_label, self.retry_entry):
                if self.mode.get() == "gemini":
                    widget.grid()
                else:
                    widget.grid_remove()
        if hasattr(self, "codex_effort_frame"):
            if self.mode.get() == "codex":
                self.codex_effort_frame.grid()
                if self.codex_effort.get() == "default":
                    self.codex_effort.set("")
                efforts = self._available_codex_efforts(self.model_name.get())
                self.codex_effort_entry.configure(
                    values=efforts,
                    state="disabled" if busy or not efforts else "readonly",
                )
            else:
                self.codex_effort_frame.grid_remove()
        if self.mode.get() == "codex":
            self.model_frame.set_title("Codex 模型")
            self.model_entry.configure(values=self._available_model_choices(),
                                       state="disabled" if busy else "readonly")
            self.retry_entry.configure(state="disabled")
            return
        if self.mode.get() == "local":
            self.model_frame.set_title("本地模型")
            self.model_entry.configure(
                values=_LOCAL_MODEL_CHOICES,
                state="disabled" if busy else "readonly",
            )
            if self.model_name.get() not in _LOCAL_MODEL_CHOICES:
                self.model_name.set(_LOCAL_MODEL_CHOICES[0])
            self.retry_entry.configure(state="disabled")
            return
        self.model_frame.set_title("Gemini API 模型")
        self.model_entry.configure(
            values=self._available_model_choices(),
            state="disabled" if busy else "normal",
        )
        if self.model_name.get() in _LOCAL_MODEL_CHOICES:
            self.model_name.set(self.config.model)
        self.retry_entry.configure(state="disabled" if busy else "normal")

    def _refresh_api_usage(self) -> None:
        if self.mode.get() in {"local", "codex"}:
            self.api_usage_label.grid_remove()
            return
        self.api_usage_label.grid()
        count = self.api_usage.count(self.model_name.get().strip())
        self.api_usage_text.set(
            "本程式今日請求：無法讀取" if count is None else f"本程式今日請求：{count} 次"
        )

    def _check_usage_date(self) -> None:
        today = quota_date()
        if today != self._usage_date:
            self._usage_date = today
            self._refresh_api_usage()
        self.root.after(30000, self._check_usage_date)

    def _selected_model_label(self) -> str:
        if self.mode.get() == "codex":
            return f"Codex / {self.config.codex_model}（推理：{self.config.codex_reasoning_effort}）"
        if self.mode.get() == "local":
            return "本地模型（待開發）"
        return f"Gemini API / {self.config.model}"

    def _clear_work_data(self, *, keep_url: bool) -> None:
        self.work = None
        self.work_result = None
        self.codex_context_text.set(context_summary(None))
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
        failed_during_chapter_translation = self.state == "translating"
        organizing_terms = self.state == "organizing_terms"
        if organizing_terms:
            self._term_organization_service = None
            self._term_organization_batches = ()
            self._term_organization_batch_index = 0
        next_state: AppState = "work_ready" if self.work_result is not None else "idle"
        self._apply_state(next_state, "發生錯誤。")
        if isinstance(exc, InvalidLlmResponseError):
            self._show_invalid_llm_error(
                exc,
                allow_blank_document=failed_during_chapter_translation,
            )
            return
        if isinstance(exc, GeminiFreeTierQuotaError):
            messagebox.showerror("執行失敗", "額度不足（429）", parent=self.root)
            return
        messagebox.showerror("執行失敗", str(exc), parent=self.root)

    def _show_invalid_llm_error(
        self,
        exc: InvalidLlmResponseError,
        *,
        allow_blank_document: bool = False,
    ) -> None:
        dialog = Toplevel(self.root)
        dialog.title("執行失敗")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=exc.reason, justify="left", wraplength=460).pack(
            anchor="w", fill="x"
        )
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(16, 0))
        ttk.Button(
            actions,
            text="查看 LLM 輸出",
            command=lambda: self._show_llm_output(exc.llm_output, dialog),
        ).pack(side="left")
        if allow_blank_document:
            ttk.Button(
                actions,
                text="建立空白文件",
                command=lambda: self._create_blank_chapter_document(dialog),
            ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="關閉", command=dialog.destroy).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _create_blank_chapter_document(self, dialog: Toplevel) -> None:
        """Create an editable partial TXT without marking the chapter complete."""
        if self.plan is None or self.work_result is None or self.work is None:
            messagebox.showerror(
                "無法建立空白文件",
                "缺少目前章節資料，無法建立空白文件。",
                parent=dialog,
            )
            return
        source = self.plan.source_chapter
        try:
            entry = self.work.get_chapter(source.chapter_number or 0)
            txt_path, html_path = self.chapter_tracker.output_paths(self.work_result, entry)
            if (txt_path.exists() or html_path.exists()) and not messagebox.askyesno(
                "章節文件已存在",
                "本章已有 TXT 或 HTML。\n\n是否將兩者覆蓋成空白文件？",
                parent=dialog,
            ):
                return
            translated_title = self.plan.translated_chapter_title or source.chapter_title
            provider = "codex" if self.mode.get() == "codex" else "gemini"
            model = self.config.codex_model if provider == "codex" else self.config.model
            content = (
                f"作品：{self.work_result.work.translated_title}\n"
                f"章節：{translated_title}\n"
                f"日文作品名：{source.title}\n"
                f"日文章節名：{source.chapter_title}\n"
                f"來源：{source.source_url}\n"
                f"翻譯引擎：{provider} / {model}\n\n"
                f"{TXT_BODY_SEPARATOR}\n\n"
            )
            atomic_write_text(txt_path, content, encoding="utf-8-sig")
            source_chunk = TextChunk(0, source.original_text)
            blank_chapter = TranslatedChapter(
                source_chapter=source,
                chunks=(TranslatedChunk(source_chunk, "空白文件"),),
                provider=provider,
                model=model,
                translated_work_title=self.work_result.work.translated_title,
                translated_chapter_title=translated_title,
            )
            HtmlFormatter(auto_open=False, overwrite=True).save_blank(
                blank_chapter,
                html_path.parent,
                txt_path=txt_path,
                destination_path=html_path,
            )
            self.chapter_progress = self.chapter_tracker.reconcile(self.work_result)
            self._update_work_details()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("無法建立空白文件", str(exc), parent=dialog)
            return
        dialog.destroy()
        self.summary.set(
            f"已建立第 {entry.number} 章的空白 TXT 與 HTML；"
            "本章仍視為未完成，可將其他軟體的譯文貼入 TXT。"
        )
        self._apply_state("work_ready", f"已建立空白文件：{txt_path.name}、{html_path.name}")

    def _show_llm_output(self, output: str, parent: Toplevel) -> None:
        viewer = Toplevel(parent)
        viewer.title("LLM 輸出")
        viewer.geometry("760x520")
        viewer.minsize(520, 320)
        viewer.transient(parent)
        frame = ttk.Frame(viewer, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = Text(frame, wrap="word", padx=10, pady=10)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.insert("1.0", output or "(空回傳)")
        text.configure(state="disabled")
        ttk.Button(frame, text="關閉", command=viewer.destroy).grid(
            row=1, column=0, columnspan=2, sticky="e", pady=(10, 0)
        )


__all__ = ["DesktopApp"]
