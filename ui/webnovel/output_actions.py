"""Web-novel local output actions and translation-error dialogs."""

from __future__ import annotations

import os
from itertools import islice
from pathlib import Path
from tkinter import Text, Toplevel, messagebox, ttk

from core.shared.exceptions import ChapterNotFoundError, InvalidLlmResponseError
from core.shared.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk
from formatters import HtmlFormatter
from formatters.utils import TXT_BODY_SEPARATOR, atomic_write_text


class WebOutputMixin:
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
        self._begin_reconcile(
            lambda progress: self._chapter_completion_updated(chapter_number, progress)
        )

    def _chapter_completion_updated(self, chapter_number, progress):
        self.chapter_progress = progress
        self._update_work_details()
        if chapter_number in progress.completed_numbers:
            self.summary.set(f"第 {chapter_number} 章已更新為已完成。")
            self.status.set(f"第 {chapter_number} 章狀態已更新。")
            return
        messagebox.showwarning(
            "章節仍未完成",
            "TXT 與 HTML 尚未符合完成條件。請確認 TXT 正文不是空白，並先用 TXT 更新 HTML。",
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
                    paths = self.chapter_tracker.output_paths(self.work_result, chapter)
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
        with path.open(encoding="utf-8-sig") as stream:
            for line in islice(stream, 8):
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
        ttk.Label(body, text=exc.reason, justify="left", wraplength=460).pack(anchor="w", fill="x")
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
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("無法建立空白文件", str(exc), parent=dialog)
            return
        dialog.destroy()
        self.summary.set(
            f"已建立第 {entry.number} 章的空白 TXT 與 HTML；"
            "本章仍視為未完成，可將其他軟體的譯文貼入 TXT。"
        )
        self._apply_state("work_ready", f"已建立空白文件：{txt_path.name}、{html_path.name}")
        self._begin_reconcile(lambda _progress: self._update_work_details())

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
