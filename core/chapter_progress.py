"""Compact chapter completion tracking backed by work.json."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.controller import TranslationResult
from core.exceptions import WorkMemoryError
from core.models import NovelChapterEntry
from core.work_memory import ChapterCompletion, WorkMemoryStore, text_hash
from core.work_setup import WorkSetupResult
from formatters.html_navigation import HtmlNavigationManager
from formatters.utils import sanitize_filename_component


@dataclass(frozen=True, slots=True)
class ChapterProgress:
    """Reconciled completion state for the currently listed chapters."""

    completed_numbers: frozenset[int]
    partial_numbers: frozenset[int]
    next_number: int
    all_completed: bool


class ChapterCompletionTracker:
    """Reconcile fixed output files and update compact completion records."""

    def __init__(
        self,
        memory_store: WorkMemoryStore | None = None,
        navigation_manager: HtmlNavigationManager | None = None,
    ) -> None:
        self._memory_store = memory_store or WorkMemoryStore()
        self._navigation_manager = navigation_manager or HtmlNavigationManager()
        self._output_indexes: dict[Path, dict[int, tuple[Path, Path]]] = {}
        self._progress_cache: dict[Path, ChapterProgress] = {}

    def reconcile(self, setup: WorkSetupResult) -> ChapterProgress:
        work = setup.work.source_work
        memory = self._memory_store.load(setup.work_directory, work)
        if memory is None:
            raise WorkMemoryError("缺少 work.json，無法讀取章節進度。")
        completions = dict(memory.completed_chapters)
        completed: set[int] = set()
        partial: set[int] = set()
        output_index: dict[int, tuple[Path, Path]] = {}
        changed = False
        for chapter in work.chapters:
            txt_path, html_path = self.output_paths(setup, chapter)
            exists = (txt_path.exists(), html_path.exists())
            if not any(exists):
                legacy_txt, legacy_html = self._legacy_output_paths(setup, chapter)
                exists = (legacy_txt.exists(), legacy_html.exists())
                if any(exists):
                    txt_path, html_path = legacy_txt, legacy_html
            is_blank_draft = all(exists) and self._is_blank_draft(html_path)
            if all(exists):
                output_index[chapter.number] = (txt_path, html_path)
            if all(exists) and not is_blank_draft:
                completed.add(chapter.number)
                if chapter.number not in completions:
                    completions[chapter.number] = ChapterCompletion(
                        source_hash="",
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                    changed = True
            elif any(exists):
                partial.add(chapter.number)
        if changed:
            self._memory_store.save_completions(setup.work_directory, work, completions)

        unfinished = [
            chapter.number for chapter in work.chapters if chapter.number not in completed
        ]
        all_completed = not unfinished
        next_number = work.chapters[-1].number if all_completed else unfinished[0]
        progress = ChapterProgress(
            completed_numbers=frozenset(completed),
            partial_numbers=frozenset(partial),
            next_number=next_number,
            all_completed=all_completed,
        )
        key = setup.work_directory.resolve()
        self._output_indexes[key] = output_index
        self._progress_cache[key] = progress
        self._navigation_manager.refresh_all(
            {number: paths[1] for number, paths in output_index.items()}
        )
        return progress

    @staticmethod
    def _is_blank_draft(html_path: Path) -> bool:
        """Identify the explicit marker used by manually completed blank drafts."""
        try:
            html = html_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return False
        return 'name="translation-status" content="draft"' in html

    def record_completed(
        self,
        setup: WorkSetupResult,
        result: TranslationResult,
    ) -> ChapterProgress:
        work = setup.work.source_work
        source = result.chapter.source_chapter
        if source.chapter_number is None:
            raise WorkMemoryError("翻譯結果缺少有效的章節數字。")
        number = source.chapter_number
        chapter = work.get_chapter(number)
        txt_path, html_path = self.output_paths(setup, chapter)
        actual_paths = {path.resolve() for path in result.output_paths}
        if (
            txt_path.resolve() not in actual_paths
            or html_path.resolve() not in actual_paths
            or not txt_path.is_file()
            or not html_path.is_file()
        ):
            raise WorkMemoryError("TXT 與 HTML 尚未完整建立，未記錄章節完成狀態。")
        memory = self._memory_store.load(setup.work_directory, work)
        if memory is None:
            raise WorkMemoryError("缺少 work.json，無法記錄章節完成狀態。")
        completions = dict(memory.completed_chapters)
        completions[number] = ChapterCompletion(
            source_hash=text_hash(source.original_text),
            completed_at=datetime.now(UTC).isoformat(),
        )
        self._memory_store.save_completions(setup.work_directory, work, completions)
        key = setup.work_directory.resolve()
        if key not in self._output_indexes:
            return self.reconcile(setup)

        output_index = self._output_indexes[key]
        output_index[number] = (txt_path, html_path)
        previous = self._progress_cache[key]
        completed = set(previous.completed_numbers)
        completed.add(number)
        partial = set(previous.partial_numbers)
        partial.discard(number)
        progress = self._build_progress(setup, completed, partial)
        self._progress_cache[key] = progress
        self._navigation_manager.refresh_neighbors(
            {item: paths[1] for item, paths in output_index.items()},
            number,
        )
        return progress

    @staticmethod
    def _build_progress(
        setup: WorkSetupResult,
        completed: set[int],
        partial: set[int],
    ) -> ChapterProgress:
        unfinished = [
            chapter.number
            for chapter in setup.work.source_work.chapters
            if chapter.number not in completed
        ]
        all_completed = not unfinished
        next_number = setup.work.source_work.chapters[-1].number if all_completed else unfinished[0]
        return ChapterProgress(
            completed_numbers=frozenset(completed),
            partial_numbers=frozenset(partial),
            next_number=next_number,
            all_completed=all_completed,
        )

    def completion_for(
        self, setup: WorkSetupResult, chapter_number: int
    ) -> ChapterCompletion | None:
        memory = self._memory_store.load(setup.work_directory, setup.work.source_work)
        return None if memory is None else memory.completed_chapters.get(chapter_number)

    @staticmethod
    def output_paths(setup: WorkSetupResult, chapter: NovelChapterEntry) -> tuple[Path, Path]:
        safe_title = sanitize_filename_component(
            setup.work.source_work.title,
            max_length=80,
        )
        stem = f"{chapter.number:04d} - {safe_title}"
        return (
            setup.work_directory / f"{stem}.txt",
            setup.work_directory / f"{stem}.html",
        )

    @classmethod
    def existing_output_paths(
        cls, setup: WorkSetupResult, chapter: NovelChapterEntry
    ) -> tuple[Path, Path]:
        """Return current paths, or a previous-format pair when only that pair exists."""
        current = cls.output_paths(setup, chapter)
        legacy = cls._legacy_output_paths(setup, chapter)
        if current[1].exists():
            return current
        if legacy[1].exists():
            return legacy
        if any(path.exists() for path in current):
            return current
        return legacy if any(path.exists() for path in legacy) else current

    @staticmethod
    def _legacy_output_paths(
        setup: WorkSetupResult, chapter: NovelChapterEntry
    ) -> tuple[Path, Path]:
        safe_title = sanitize_filename_component(setup.work.source_work.title, max_length=80)
        stem = f"{safe_title} - {chapter.number}"
        return (
            setup.work_directory / f"{stem}.txt",
            setup.work_directory / f"{stem}.html",
        )


__all__ = ["ChapterCompletionTracker", "ChapterProgress"]
