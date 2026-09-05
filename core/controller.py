"""Application orchestration for preparing and running one chapter translation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.checkpoint import CheckpointJob, CheckpointStore
from core.exceptions import GeminiFreeTierQuotaError, TranslationCancelled
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

    def prepare(self, url: str) -> TranslationPlan:
        """Extract, split, and inspect reusable progress without translating."""
        source_chapter = self._extractor.extract(url)
        chunks = self._chunker.split(source_chapter.original_text)
        work_directory = self._work_directory(source_chapter)
        terms = self._term_memory_store.load(work_directory)
        matched_terms = self._term_memory_store.match(source_chapter.original_text, terms)
        job = CheckpointJob.create(
            source_chapter=source_chapter,
            chunks=chunks,
            provider=self.provider,
            model=self.model,
            system_prompt=self._translator.checkpoint_identity,
        )
        completed = self._checkpoint_store.load(job)
        return TranslationPlan(
            source_chapter=source_chapter,
            chunks=chunks,
            checkpoint_job=job,
            completed_chunks=completed,
            matched_terms=tuple(matched_terms.items()),
            translated_work_title=self._translated_work_title,
        )

    def run(
        self,
        plan: TranslationPlan,
        *,
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

        completed = list(self._checkpoint_store.load(plan.checkpoint_job))
        reused_count = len(completed)
        temporary_terms = dict(plan.matched_terms)
        new_terms: dict[str, str] = {}
        term_errors: list[str] = []
        term_rejected = 0
        term_conflicts = 0
        for index, translated in enumerate(completed):
            if progress:
                progress(ProgressUpdate(index + 1, plan.total_chunks, "checkpoint"))
            temporary_terms, update, error = self._analyze_chunk_terms(
                plan,
                translated,
                temporary_terms,
                progress,
            )
            new_terms.update(update.added)
            term_rejected += update.rejected
            term_conflicts += update.conflicts
            if error:
                term_errors.append(error)

        translated_count = 0
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
            translated = self._translator.translate(source_chunk, temporary_terms)
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
            temporary_terms, update, error = self._analyze_chunk_terms(
                plan,
                translated,
                temporary_terms,
                progress,
            )
            new_terms.update(update.added)
            term_rejected += update.rejected
            term_conflicts += update.conflicts
            if error:
                term_errors.append(error)

        if should_cancel and should_cancel():
            raise TranslationCancelled("Translation was cancelled by the user.")
        if progress:
            progress(ProgressUpdate(0, 0, "translating_title"))
        translated_chapter_title = self._translator.translate_title(
            plan.source_chapter.chapter_title,
            temporary_terms,
        )
        chapter = TranslatedChapter(
            source_chapter=plan.source_chapter,
            chunks=tuple(completed),
            provider=self.provider,
            model=self.model,
            translated_work_title=plan.translated_work_title,
            translated_chapter_title=translated_chapter_title,
        )
        work_directory = self._work_directory(plan.source_chapter)
        output_paths = tuple(
            formatter.save(chapter, work_directory) for formatter in self._formatters
        )
        term_update: TermMemoryUpdate | None = None
        if self._term_analyzer is not None:
            try:
                if progress:
                    progress(ProgressUpdate(0, 0, "updating_terms"))
                persisted = self._term_memory_store.update(
                    work_directory,
                    new_terms,
                    source_text=plan.source_chapter.original_text,
                    translated_text="".join(item.translated_text for item in chapter.chunks),
                )
                term_update = TermMemoryUpdate(
                    persisted.added,
                    term_rejected + persisted.rejected,
                    term_conflicts + persisted.conflicts,
                )
            except Exception as exc:  # noqa: BLE001 - outputs remain valid when memory fails.
                term_errors.append(str(exc))
        return TranslationResult(
            chapter=chapter,
            output_paths=output_paths,
            reused_chunks=reused_count,
            translated_chunks=translated_count,
            term_memory_update=term_update,
            term_memory_error="；".join(dict.fromkeys(term_errors)) or None,
        )

    def _analyze_chunk_terms(
        self,
        plan: TranslationPlan,
        translated: TranslatedChunk,
        temporary_terms: dict[str, str],
        progress: Callable[[ProgressUpdate], None] | None,
    ) -> tuple[dict[str, str], TermMemoryUpdate, str | None]:
        """Add one translated chunk's validated terms to chapter-local memory."""
        empty = TermMemoryUpdate({}, 0, 0)
        if self._term_analyzer is None:
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
        )
        try:
            candidates = self._term_analyzer.analyze(chunk_chapter, temporary_terms)
            merged, update = self._term_memory_store.merge_candidates(
                temporary_terms,
                candidates,
                source_text=source_chunk.text,
                translated_text=translated.translated_text,
            )
            return merged, update, None
        except GeminiFreeTierQuotaError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed memory pass must not lose正文 progress.
            return temporary_terms, empty, str(exc)

    def _work_directory(self, chapter: NovelChapter) -> Path:
        return self._output_directory / sanitize_filename_component(chapter.title, max_length=80)


__all__ = [
    "ProgressUpdate",
    "TranslationController",
    "TranslationPlan",
    "TranslationResult",
]
