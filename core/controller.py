"""Application orchestration for preparing and running one chapter translation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.checkpoint import CheckpointJob, CheckpointStore
from core.exceptions import (
    ApiRequestError,
    GeminiFreeTierQuotaError,
    InvalidLlmResponseError,
    TranslationCancelled,
)
from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk
from core.term_memory import TermMemoryStore, TermMemoryUpdate
from core.text_chunker import TextChunker
from extractors.base import BaseExtractor
from formatters.base import BaseFormatter
from formatters.utils import sanitize_filename_component
from translators.base import BaseTranslator
from translators.term_base import BaseTermAnalyzer


@dataclass(frozen=True, slots=True)
class TranslationPlan:
    """Prepared source, chunks, and reusable progress before any new API request."""

    source_chapter: NovelChapter
    chunks: tuple[TextChunk, ...]
    checkpoint_job: CheckpointJob
    completed_chunks: tuple[TranslatedChunk, ...]
    translated_chapter_title: str | None = None
    completed_term_indexes: frozenset[int] = frozenset()
    matched_terms: tuple[tuple[str, str], ...] = ()
    translated_work_title: str | None = None

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)

    @property
    def completed_count(self) -> int:
        return len(self.completed_chunks)

    @property
    def pending_count(self) -> int:
        return self.total_chunks - self.completed_count

    @property
    def pending_title_count(self) -> int:
        return 0 if self.translated_chapter_title else 1

    @property
    def pending_term_count(self) -> int:
        return self.total_chunks - len(self.completed_term_indexes)


@dataclass(frozen=True, slots=True)
class ChapterExecutionOptions:
    """User-selected chapter components to translate or analyze."""

    translate_title: bool = True
    translate_body: bool = True
    update_terms: bool = True

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (self.translate_title, self.translate_body, self.update_terms)
        ):
            raise TypeError("chapter execution options must be booleans")
        if self.update_terms and not self.translate_body:
            raise ValueError("term-memory analysis requires body translation")

    @property
    def requires_api(self) -> bool:
        return self.translate_title or self.translate_body or self.update_terms


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """One user-facing progress event emitted by the controller."""

    completed: int
    total: int
    source: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Complete translated chapter and every generated output path."""

    chapter: TranslatedChapter
    output_paths: tuple[Path, ...]
    reused_chunks: int
    translated_chunks: int
    term_memory_update: TermMemoryUpdate | None = None
    term_memory_error: str | None = None


class TranslationController:
    """Coordinate interchangeable extraction, translation, checkpoint, and output strategies."""

    def __init__(
        self,
        *,
        extractor: BaseExtractor,
        chunker: TextChunker,
        translator: BaseTranslator,
        checkpoint_store: CheckpointStore,
        formatters: tuple[BaseFormatter, ...],
        output_directory: Path,
        term_memory_store: TermMemoryStore | None = None,
        term_analyzer: BaseTermAnalyzer | None = None,
        translated_work_title: str | None = None,
    ) -> None:
        if not isinstance(extractor, BaseExtractor):
            raise TypeError("extractor must implement BaseExtractor")
        if not isinstance(chunker, TextChunker):
            raise TypeError("chunker must be a TextChunker")
        if not isinstance(translator, BaseTranslator):
            raise TypeError("translator must implement BaseTranslator")
        if not isinstance(checkpoint_store, CheckpointStore):
            raise TypeError("checkpoint_store must be a CheckpointStore")
        if not isinstance(formatters, tuple) or not formatters:
            raise ValueError("formatters must be a non-empty tuple")
        if not all(isinstance(formatter, BaseFormatter) for formatter in formatters):
            raise TypeError("all formatters must implement BaseFormatter")
        if term_analyzer is not None and not isinstance(term_analyzer, BaseTermAnalyzer):
            raise TypeError("term_analyzer must implement BaseTermAnalyzer")

        self._extractor = extractor
        self._chunker = chunker
        self._translator = translator
        self._checkpoint_store = checkpoint_store
        self._formatters = formatters
        self._output_directory = Path(output_directory)
        self._term_memory_store = term_memory_store or TermMemoryStore()
        self._term_analyzer = term_analyzer
        self._translated_work_title = translated_work_title

    @property
    def provider(self) -> str:
        return self._translator.provider

    @property
    def model(self) -> str:
        return self._translator.model

    def clear_checkpoint(self, plan: TranslationPlan) -> bool:
        """Remove this exact plan's checkpoint before an explicit full retranslation."""
        if not isinstance(plan, TranslationPlan):
            raise TypeError("plan must be a TranslationPlan")
        if (
            plan.checkpoint_job.provider != self.provider
            or plan.checkpoint_job.model != self.model
            or plan.checkpoint_job.chunks != plan.chunks
        ):
            raise ValueError("plan is incompatible with the configured controller")
        return self._checkpoint_store.clear(plan.checkpoint_job)

    def clear_checkpoint_components(
        self,
        plan: TranslationPlan,
        options: ChapterExecutionOptions,
    ) -> bool:
        """Clear only components selected for an explicit retranslation."""
        if not isinstance(plan, TranslationPlan):
            raise TypeError("plan must be a TranslationPlan")
        if not isinstance(options, ChapterExecutionOptions):
            raise TypeError("options must be ChapterExecutionOptions")
        return self._checkpoint_store.clear_components(
            plan.checkpoint_job,
            title=options.translate_title,
            chunks=options.translate_body,
            terms=options.update_terms,
        )

    def prepare(self, url: str) -> TranslationPlan:
        """Extract, split, and inspect reusable progress without translating."""
        source_chapter = self._extractor.extract(url)
        chunks = self._chunker.split(source_chapter.original_text)
        return self._prepare_chunks(source_chapter, chunks)

    def resegment(self, plan: TranslationPlan, count: int) -> TranslationPlan:
        """Reanalyze cached source without extraction or any model request."""
        chunks = self._chunker.split_count(plan.source_chapter.original_text, count)
        return self._prepare_chunks(plan.source_chapter, chunks)

    def _prepare_chunks(
        self, source_chapter: NovelChapter, chunks: tuple[TextChunk, ...]
    ) -> TranslationPlan:
        work_directory = self._work_directory(source_chapter)
        terms = self._term_memory_store.load(work_directory)
        matched_terms = self._term_memory_store.match(
            f"{source_chapter.chapter_title}\n{source_chapter.original_text}",
            terms,
        )
        job = CheckpointJob.create(
            source_chapter=source_chapter,
            chunks=chunks,
            provider=self.provider,
            model=self.model,
            system_prompt=self._translator.checkpoint_identity,
        )
        completed = self._checkpoint_store.load(job)
        translated_title = self._checkpoint_store.load_title(job)
        completed_terms = (
            frozenset(
                translated.index
                for translated in completed
                if self._checkpoint_store.term_completed(
                    job,
                    translated,
                    self._term_analyzer.checkpoint_identity,
                )
            )
            if self._term_analyzer is not None
            else frozenset()
        )
        return TranslationPlan(
            source_chapter=source_chapter,
            chunks=chunks,
            checkpoint_job=job,
            completed_chunks=completed,
            translated_chapter_title=translated_title,
            completed_term_indexes=completed_terms,
            matched_terms=tuple(matched_terms.items()),
            translated_work_title=self._translated_work_title,
        )

    def run(
        self,
        plan: TranslationPlan,
        *,
        options: ChapterExecutionOptions | None = None,
        progress: Callable[[ProgressUpdate], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranslationResult:
        """Resume missing chunks, checkpoint each one, then write all outputs."""
        if not isinstance(plan, TranslationPlan):
            raise TypeError("plan must be a TranslationPlan")
        if (
            plan.checkpoint_job.provider != self.provider
            or plan.checkpoint_job.model != self.model
            or plan.checkpoint_job.chunks != plan.chunks
        ):
            raise ValueError("plan is incompatible with the configured controller")

        selected = options or ChapterExecutionOptions()
        if self.provider == "codex":
            from translators.codex_llm import CodexTranslator

            if isinstance(self._translator, CodexTranslator):
                self._translator.chapter_number = plan.source_chapter.chapter_number
                self._translator.context = (
                    f"{plan.source_chapter.source_url}\n{plan.source_chapter.chapter_title}\n"
                    f"分段識別：{plan.checkpoint_job.job_id}"
                )
        if not isinstance(selected, ChapterExecutionOptions):
            raise TypeError("options must be ChapterExecutionOptions")

        checkpoint_chunks = self._checkpoint_store.load(plan.checkpoint_job)
        completed = list(checkpoint_chunks) if selected.translate_body else []
        reused_count = len(completed)
        temporary_terms = dict(plan.matched_terms)
        translated_chapter_title = (
            self._checkpoint_store.load_title(plan.checkpoint_job)
            if selected.translate_title
            else plan.source_chapter.chapter_title
        )
        new_terms: dict[str, str] = {}
        term_errors: list[str] = []
        term_rejected = 0
        term_conflicts = 0
        for index, translated in enumerate(completed):
            if progress:
                progress(ProgressUpdate(index + 1, plan.total_chunks, "checkpoint"))
            if selected.update_terms:
                temporary_terms, update, error = self._analyze_chunk_terms(
                    plan,
                    translated,
                    temporary_terms,
                    translated_chapter_title,
                    progress,
                )
                new_terms.update(update.added)
                term_rejected += update.rejected
                term_conflicts += update.conflicts
                if error:
                    term_errors.append(error)

        if selected.translate_title and translated_chapter_title is None:
            if should_cancel and should_cancel():
                raise TranslationCancelled("Translation was cancelled by the user.")
            if progress:
                progress(ProgressUpdate(0, plan.total_chunks, "translating_title"))
            translated_chapter_title = self._translator.translate_title(
                plan.source_chapter.chapter_title,
                temporary_terms,
            )
            self._checkpoint_store.save_title(
                plan.checkpoint_job,
                translated_chapter_title,
            )

        translated_count = 0
        if selected.translate_body:
            for source_chunk in plan.chunks[reused_count:]:
                if should_cancel and should_cancel():
                    raise TranslationCancelled("Translation was cancelled by the user.")
                if progress:
                    progress(
                        ProgressUpdate(
                            source_chunk.index + 1,
                            plan.total_chunks,
                            "translating",
                        )
                    )
                translated = self._translator.translate(
                    source_chunk,
                    temporary_terms,
                )
                if translated.source_chunk != source_chunk:
                    raise ValueError("translator returned a result for a different source chunk")
                self._checkpoint_store.save_chunk(plan.checkpoint_job, translated)
                completed.append(translated)
                translated_count += 1
                if progress:
                    progress(
                        ProgressUpdate(
                            len(completed),
                            plan.total_chunks,
                            "translated",
                        )
                    )
                if selected.update_terms:
                    temporary_terms, update, error = self._analyze_chunk_terms(
                        plan,
                        translated,
                        temporary_terms,
                        translated_chapter_title,
                        progress,
                    )
                    new_terms.update(update.added)
                    term_rejected += update.rejected
                    term_conflicts += update.conflicts
                    if error:
                        term_errors.append(error)
        else:
            completed = [TranslatedChunk(chunk, chunk.text) for chunk in plan.chunks]

        if should_cancel and should_cancel():
            raise TranslationCancelled("Translation was cancelled by the user.")
        if not translated_chapter_title:
            raise ValueError("first translated chunk did not include a translated chapter title")
        output_provider, output_model = self._output_identity(selected)
        chapter = TranslatedChapter(
            source_chapter=plan.source_chapter,
            chunks=tuple(completed),
            provider=output_provider,
            model=output_model,
            translated_work_title=plan.translated_work_title,
            translated_chapter_title=translated_chapter_title,
        )
        work_directory = self._work_directory(plan.source_chapter)
        output_paths = tuple(
            formatter.save(chapter, work_directory) for formatter in self._formatters
        )
        term_update = (
            TermMemoryUpdate(new_terms, term_rejected, term_conflicts)
            if selected.update_terms and self._term_analyzer is not None
            else None
        )
        return TranslationResult(
            chapter=chapter,
            output_paths=output_paths,
            reused_chunks=reused_count,
            translated_chunks=translated_count,
            term_memory_update=term_update,
            term_memory_error="；".join(dict.fromkeys(term_errors)) or None,
        )

    def _output_identity(self, options: ChapterExecutionOptions) -> tuple[str, str]:
        if options.translate_title and options.translate_body:
            return self.provider, self.model
        if options.translate_title:
            return self.provider, f"{self.model}（僅翻譯標題）"
        if options.translate_body:
            return self.provider, f"{self.model}（僅翻譯內文）"
        return "原文", "未翻譯"

    def _analyze_chunk_terms(
        self,
        plan: TranslationPlan,
        translated: TranslatedChunk,
        temporary_terms: dict[str, str],
        translated_chapter_title: str | None,
        progress: Callable[[ProgressUpdate], None] | None,
    ) -> tuple[dict[str, str], TermMemoryUpdate, str | None]:
        """Add one translated chunk's validated terms to chapter-local memory."""
        empty = TermMemoryUpdate({}, 0, 0)
        if self._term_analyzer is None:
            return temporary_terms, empty, None
        if self._checkpoint_store.term_completed(
            plan.checkpoint_job,
            translated,
            self._term_analyzer.checkpoint_identity,
        ):
            if progress:
                progress(
                    ProgressUpdate(
                        translated.index + 1,
                        plan.total_chunks,
                        "term_checkpoint",
                    )
                )
            return temporary_terms, empty, None
        if progress:
            progress(ProgressUpdate(translated.index + 1, plan.total_chunks, "analyzing_terms"))
        source_chunk = TextChunk(0, translated.source_chunk.text)
        chunk_chapter = TranslatedChapter(
            source_chapter=NovelChapter(
                title=plan.source_chapter.title,
                chapter_title=plan.source_chapter.chapter_title,
                source_url=plan.source_chapter.source_url,
                original_text=source_chunk.text,
            ),
            chunks=(TranslatedChunk(source_chunk, translated.translated_text),),
            provider=self.provider,
            model=self.model,
            translated_work_title=plan.translated_work_title,
            translated_chapter_title=translated_chapter_title,
        )
        try:
            candidates = self._term_analyzer.analyze(chunk_chapter, temporary_terms)
            work_directory = self._work_directory(plan.source_chapter)
            update = self._term_memory_store.update(
                work_directory,
                candidates,
                source_text=source_chunk.text,
                translated_text=translated.translated_text,
            )
            merged = dict(temporary_terms)
            merged.update(update.added)
            self._checkpoint_store.save_term_completed(
                plan.checkpoint_job,
                translated,
                self._term_analyzer.checkpoint_identity,
            )
            return merged, update, None
        except (GeminiFreeTierQuotaError, ApiRequestError, InvalidLlmResponseError):
            raise
        except Exception as exc:  # noqa: BLE001 - a failed memory pass must not lose正文 progress.
            return temporary_terms, empty, str(exc)

    def _work_directory(self, chapter: NovelChapter) -> Path:
        return self._output_directory / sanitize_filename_component(chapter.title, max_length=80)


__all__ = [
    "ChapterExecutionOptions",
    "ProgressUpdate",
    "TranslationController",
    "TranslationPlan",
    "TranslationResult",
]
