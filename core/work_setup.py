"""Create and reuse work-level metadata beside chapter outputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.config import MissingApiKeyError
from core.exceptions import WorkSetupCancelled
from core.models import NovelWork, TranslatedNovelWork
from core.term_memory import TermMemoryStore
from core.work_directories import resolve_work_directory
from core.work_memory import WorkMemory, WorkMemoryStore, text_hash
from formatters.utils import atomic_write_text
from translators.work_base import BaseWorkTranslator

ProgressCallback = Callable[[str], None]
OverwriteConfirmation = Callable[[Path], bool]


@dataclass(frozen=True, slots=True)
class WorkSetupResult:
    """Prepared work metadata and the files used to persist it."""

    work: TranslatedNovelWork
    work_directory: Path
    memory_path: Path
    synopsis_path: Path
    terms_path: Path
    reused_memory: bool


class WorkSetupService:
    """Prepare one work directory without coupling decisions to Tkinter."""

    def __init__(
        self,
        output_directory: Path,
        translator: BaseWorkTranslator | None,
        *,
        memory_store: WorkMemoryStore | None = None,
        term_memory_store: TermMemoryStore | None = None,
    ) -> None:
        if translator is not None and not isinstance(translator, BaseWorkTranslator):
            raise TypeError("translator must implement BaseWorkTranslator")
        self._output_directory = Path(output_directory)
        self._translator = translator
        self._memory_store = memory_store or WorkMemoryStore()
        self._term_memory_store = term_memory_store or TermMemoryStore()

    def prepare(
        self,
        work: NovelWork,
        *,
        progress: ProgressCallback | None = None,
        confirm_overwrite: OverwriteConfirmation | None = None,
    ) -> WorkSetupResult:
        if not isinstance(work, NovelWork):
            raise TypeError("work must be a NovelWork")
        self._emit(progress, "checking_directory")
        work_directory = resolve_work_directory(
            self._output_directory,
            work_id=work.work_id,
            source_url=work.source_url,
            title=work.title,
        )
        work_directory.mkdir(parents=True, exist_ok=True)
        synopsis_path = work_directory / "synopsis.txt"

        self._emit(progress, "checking_memory")
        memory = self._memory_store.load(work_directory, work)
        memory_is_reusable = (
            memory is not None
            and memory.matches_source(work)
            and (
                memory.provider != "fake"
                or (self._translator is not None and self._translator.provider == "fake")
            )
        )
        if memory_is_reusable:
            assert memory is not None
            translated = memory.translated_work(work)
            if not synopsis_path.exists():
                rendered = self._render_synopsis(translated)
                atomic_write_text(synopsis_path, rendered, encoding="utf-8-sig")
            self._emit(progress, "completed")
            return self._result(translated, work_directory, synopsis_path, reused=True)

        if (
            synopsis_path.exists()
            and self._requires_overwrite_confirmation(synopsis_path, memory)
            and (confirm_overwrite is None or not confirm_overwrite(synopsis_path))
        ):
            raise WorkSetupCancelled("已取消覆蓋手動修改的 synopsis.txt。")

        self._emit(progress, "translating_metadata")
        if self._translator is None:
            raise MissingApiKeyError("作品資訊需要 API Key 才能建立翻譯記憶。")
        translated = self._translator.translate(work)
        rendered = self._render_synopsis(translated)

        self._emit(progress, "saving_memory")
        atomic_write_text(synopsis_path, rendered, encoding="utf-8-sig")
        self._memory_store.save(work_directory, translated, text_hash(rendered))
        self._emit(progress, "completed")
        return self._result(translated, work_directory, synopsis_path, reused=False)

    def _result(
        self,
        translated: TranslatedNovelWork,
        work_directory: Path,
        synopsis_path: Path,
        *,
        reused: bool,
    ) -> WorkSetupResult:
        return WorkSetupResult(
            work=translated,
            work_directory=work_directory,
            memory_path=work_directory / self._memory_store.filename,
            synopsis_path=synopsis_path,
            terms_path=self._term_memory_store.ensure(work_directory),
            reused_memory=reused,
        )

    @staticmethod
    def _requires_overwrite_confirmation(path: Path, memory: WorkMemory | None) -> bool:
        if memory is None:
            return True
        generated_hash = memory.synopsis_generated_hash
        try:
            current = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return True
        return text_hash(current) != generated_hash

    @staticmethod
    def _render_synopsis(translated: TranslatedNovelWork) -> str:
        source = translated.source_work
        return (
            f"中文作品名稱：{translated.translated_title}\n"
            f"日文作品名稱：{source.title}\n"
            f"作者：{source.author}\n"
            f"來源：{source.source_url}\n\n"
            f"摘要：\n{translated.translated_synopsis}\n"
        )

    @staticmethod
    def _emit(progress: ProgressCallback | None, stage: str) -> None:
        if progress is not None:
            progress(stage)


__all__ = ["WorkSetupResult", "WorkSetupService"]
