"""Tests for end-to-end orchestration without web, API, or browser access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from core.checkpoint import CheckpointStore
from core.controller import ChapterExecutionOptions, ProgressUpdate, TranslationController
from core.exceptions import TranslationCancelled
from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk
from core.term_memory import TermMemoryStore
from core.text_chunker import TextChunker
from extractors.base import BaseExtractor
from formatters.base import BaseFormatter
from translators.base import BaseTranslator
from translators.term_base import BaseTermAnalyzer


class StaticExtractor(BaseExtractor):
    def __init__(self, chapter: NovelChapter) -> None:
        self.chapter = chapter
        self.urls: list[str] = []

    def can_handle(self, url: str) -> bool:
        return bool(url)

    def extract(self, url: str) -> NovelChapter:
        self.urls.append(url)
        return self.chapter


class RecordingTranslator(BaseTranslator):
    def __init__(self, *, fail_at: int | None = None, return_foreign: bool = False) -> None:
        self.calls: list[int] = []
        self.term_calls: list[dict[str, str]] = []
        self.title_calls: list[str] = []
        self.title_term_calls: list[dict[str, str]] = []
        self.fail_at = fail_at
        self.return_foreign = return_foreign

    @property
    def provider(self) -> str:
        return "test"

    @property
    def model(self) -> str:
        return "test-model"

    @property
    def checkpoint_identity(self) -> str:
        return "test-instructions-v1"

    def translate(
        self,
        chunk: TextChunk,
        terms: Mapping[str, str] | None = None,
    ) -> TranslatedChunk:
        self.calls.append(chunk.index)
        self.term_calls.append(dict(terms or {}))
        if chunk.index == self.fail_at:
            raise RuntimeError("simulated interruption")
        source = TextChunk(index=chunk.index, text="foreign") if self.return_foreign else chunk
        return TranslatedChunk(
            source_chunk=source,
            translated_text=f"譯文 {chunk.index}",
        )

    def translate_title(
        self,
        title: str,
        terms: Mapping[str, str] | None = None,
    ) -> str:
        self.title_calls.append(title)
        self.title_term_calls.append(dict(terms or {}))
        return f"譯名：{title}"


class RecordingFormatter(BaseFormatter):
    def __init__(self, suffix: str) -> None:
        self.suffix = suffix
        self.chapters: list[TranslatedChapter] = []

    def save(self, chapter: TranslatedChapter, output_dir: Path) -> Path:
        self.chapters.append(chapter)
        return output_dir / f"result{self.suffix}"


class RecordingTermAnalyzer(BaseTermAnalyzer):
    def __init__(self, result: dict[str, str] | None = None, *, failure: Exception | None = None) -> None:
        self.result = result or {}
        self.failure = failure
        self.used_terms: list[dict[str, str]] = []

    @property
    def checkpoint_identity(self) -> str:
        return "recording-failure" if self.failure is not None else "recording-success"

    def analyze(
        self,
        chapter: TranslatedChapter,
        used_terms: Mapping[str, str],
    ) -> dict[str, str]:
        self.used_terms.append(dict(used_terms))
        if self.failure is not None:
            raise self.failure
        return self.result


class IncrementalTermAnalyzer(BaseTermAnalyzer):
    def __init__(self) -> None:
        self.used_terms: list[dict[str, str]] = []

    def analyze(
        self,
        chapter: TranslatedChapter,
        used_terms: Mapping[str, str],
    ) -> dict[str, str]:
        self.used_terms.append(dict(used_terms))
        if chapter.source_chapter.original_text == "一。\n\n":
            return {"一": "譯文"}
        return {}


def make_chapter() -> NovelChapter:
    return NovelChapter(
        title="測試作品",
        chapter_title="第一章",
        source_url="https://example.test/1",
        original_text="一。\n\n二。\n\n三。",
    )


def make_controller(
    tmp_path: Path,
    *,
    translator: RecordingTranslator | None = None,
    store: CheckpointStore | None = None,
    analyzer: BaseTermAnalyzer | None = None,
) -> tuple[TranslationController, RecordingTranslator, tuple[RecordingFormatter, ...]]:
    active_translator = translator or RecordingTranslator()
    formatters = (RecordingFormatter(".txt"), RecordingFormatter(".html"))
    controller = TranslationController(
        extractor=StaticExtractor(make_chapter()),
        chunker=TextChunker(max_chars=4),
        translator=active_translator,
        checkpoint_store=store or CheckpointStore(tmp_path / "checkpoints"),
        formatters=formatters,
        output_directory=tmp_path / "outputs",
        term_memory_store=TermMemoryStore(),
        term_analyzer=analyzer,
    )
    return controller, active_translator, formatters


def test_prepare_extracts_splits_and_never_translates(tmp_path: Path) -> None:
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("https://example.test/1")

    assert plan.source_chapter.title == "測試作品"
    assert plan.total_chunks == 3
    assert plan.completed_count == 0
    assert plan.pending_count == 3
    assert translator.calls == []
    assert controller.provider == "test"
    assert controller.model == "test-model"


def test_existing_terms_are_matched_once_and_supplied_to_every_chunk(tmp_path: Path) -> None:
    work_directory = tmp_path / "outputs" / "測試作品"
    work_directory.mkdir(parents=True)
    (work_directory / "terms.json").write_text(
        '{"一":"壹","三":"參","未出現":"無"}', encoding="utf-8"
    )
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("url")
    assert dict(plan.matched_terms) == {"一": "壹", "三": "參"}
    controller.run(plan)
    assert translator.term_calls == [{"一": "壹", "三": "參"}] * 3


def test_existing_term_used_only_in_chapter_title_is_supplied_to_first_chunk(
    tmp_path: Path,
) -> None:
    work_directory = tmp_path / "outputs" / "測試作品"
    work_directory.mkdir(parents=True)
    (work_directory / "terms.json").write_text('{"第一章":"序章"}', encoding="utf-8")
    controller, translator, _ = make_controller(tmp_path)

    controller.run(controller.prepare("url"))

    assert translator.term_calls[0] == {"第一章": "序章"}
    assert translator.title_term_calls == [{"第一章": "序章"}]


def test_term_analysis_updates_after_outputs_and_failure_is_nonfatal(tmp_path: Path) -> None:
    analyzer = RecordingTermAnalyzer({"一": "譯文"})
    controller, _, _ = make_controller(tmp_path, analyzer=analyzer)
    result = controller.run(controller.prepare("url"))
    assert result.term_memory_error is None
    assert result.term_memory_update is not None
    assert result.term_memory_update.added == {"一": "譯文"}
    assert TermMemoryStore().load(tmp_path / "outputs" / "測試作品") == {"一": "譯文"}

    failed = RecordingTermAnalyzer(failure=RuntimeError("analysis failed"))
    failing_controller, _, _ = make_controller(tmp_path, analyzer=failed)
    failed_result = failing_controller.run(failing_controller.prepare("url"))
    assert failed_result.output_paths
    assert failed_result.term_memory_error == "analysis failed"


def test_new_chunk_terms_are_supplied_to_every_later_chunk(tmp_path: Path) -> None:
    analyzer = IncrementalTermAnalyzer()
    controller, translator, _ = make_controller(tmp_path, analyzer=analyzer)

    result = controller.run(controller.prepare("url"))

    assert translator.term_calls == [{}, {"一": "譯文"}, {"一": "譯文"}]
    assert analyzer.used_terms == [{}, {"一": "譯文"}, {"一": "譯文"}]
    assert result.term_memory_update is not None
    assert result.term_memory_update.added == {"一": "譯文"}
    assert TermMemoryStore().load(tmp_path / "outputs" / "測試作品") == {"一": "譯文"}


def test_run_translates_checkpoints_and_formats_complete_chapter(tmp_path: Path) -> None:
    controller, translator, formatters = make_controller(tmp_path)
    plan = controller.prepare("https://example.test/1")
    updates: list[ProgressUpdate] = []

    result = controller.run(plan, progress=updates.append)

    assert translator.calls == [0, 1, 2]
    assert result.reused_chunks == 0
    assert result.translated_chunks == 3
    assert result.output_paths == (
        tmp_path / "outputs" / "測試作品" / "result.txt",
        tmp_path / "outputs" / "測試作品" / "result.html",
    )
    assert tuple(chunk.translated_text for chunk in result.chapter.chunks) == (
        "譯文 0",
        "譯文 1",
        "譯文 2",
    )
    assert all(formatter.chapters == [result.chapter] for formatter in formatters)
    assert updates == [
        ProgressUpdate(0, 3, "translating_title"),
        ProgressUpdate(1, 3, "translating"),
        ProgressUpdate(1, 3, "translated"),
        ProgressUpdate(2, 3, "translating"),
        ProgressUpdate(2, 3, "translated"),
        ProgressUpdate(3, 3, "translating"),
        ProgressUpdate(3, 3, "translated"),
    ]


def test_run_can_keep_original_title_without_saving_title_checkpoint(tmp_path: Path) -> None:
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("url")

    result = controller.run(
        plan,
        options=ChapterExecutionOptions(
            translate_title=False,
            translate_body=True,
            update_terms=False,
        ),
    )

    assert translator.title_calls == []
    assert result.chapter.translated_chapter_title == "第一章"
    assert result.chapter.model == "test-model（僅翻譯內文）"
    assert CheckpointStore(tmp_path / "checkpoints").load_title(plan.checkpoint_job) is None


def test_run_can_output_original_body_without_translation_or_checkpoints(
    tmp_path: Path,
) -> None:
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("url")

    result = controller.run(
        plan,
        options=ChapterExecutionOptions(
            translate_title=False,
            translate_body=False,
            update_terms=False,
        ),
    )

    assert translator.calls == []
    assert translator.title_calls == []
    assert "".join(chunk.translated_text for chunk in result.chapter.chunks) == make_chapter().original_text
    assert result.chapter.provider == "原文"
    assert result.chapter.model == "未翻譯"
    assert result.translated_chunks == 0
    assert CheckpointStore(tmp_path / "checkpoints").load(plan.checkpoint_job) == ()


def test_run_can_skip_new_term_analysis_while_translating(tmp_path: Path) -> None:
    analyzer = RecordingTermAnalyzer({"一": "譯文"})
    controller, _, _ = make_controller(tmp_path, analyzer=analyzer)

    result = controller.run(
        controller.prepare("url"),
        options=ChapterExecutionOptions(update_terms=False),
    )

    assert analyzer.used_terms == []
    assert result.term_memory_update is None


def test_term_analysis_requires_body_translation() -> None:
    with pytest.raises(ValueError, match="requires body translation"):
        ChapterExecutionOptions(translate_body=False, update_terms=True)


def test_run_resumes_and_reports_checkpoint_chunks(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    first_controller, first_translator, _ = make_controller(tmp_path, store=store)
    plan = first_controller.prepare("https://example.test/1")
    store.save_chunk(
        plan.checkpoint_job,
        TranslatedChunk(
            plan.chunks[0],
            first_translator.translate(plan.chunks[0]).translated_text,
            first_translator.translate_title(plan.source_chapter.chapter_title),
        ),
    )
    store.save_chunk(plan.checkpoint_job, first_translator.translate(plan.chunks[1]))

    resumed_controller, resumed_translator, _ = make_controller(tmp_path, store=store)
    resumed_plan = resumed_controller.prepare("https://example.test/1")
    updates: list[ProgressUpdate] = []
    result = resumed_controller.run(resumed_plan, progress=updates.append)

    assert resumed_translator.calls == [2]
    assert result.reused_chunks == 2
    assert result.translated_chunks == 1
    assert [update.source for update in updates] == [
        "checkpoint",
        "checkpoint",
        "translating",
        "translated",
    ]


def test_explicit_retranslation_clears_checkpoint(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    controller, translator, _ = make_controller(tmp_path, store=store)
    plan = controller.prepare("https://example.test/1")
    first = translator.translate(plan.chunks[0])
    store.save_chunk(
        plan.checkpoint_job,
        TranslatedChunk(
            plan.chunks[0],
            first.translated_text,
            translator.translate_title(plan.source_chapter.chapter_title),
        ),
    )

    assert len(store.load(plan.checkpoint_job)) == 1
    assert controller.clear_checkpoint(plan) is True
    assert store.load(plan.checkpoint_job) == ()
    assert controller.clear_checkpoint(plan) is False


def test_selective_retranslation_clears_only_selected_checkpoint(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    controller, translator, _ = make_controller(tmp_path, store=store)
    plan = controller.prepare("url")
    store.save_title(plan.checkpoint_job, translator.translate_title("第一章"))
    store.save_chunk(plan.checkpoint_job, translator.translate(plan.chunks[0]))

    controller.clear_checkpoint_components(
        plan,
        ChapterExecutionOptions(
            translate_title=True,
            translate_body=False,
            update_terms=False,
        ),
    )

    assert store.load_title(plan.checkpoint_job) is None
    assert len(store.load(plan.checkpoint_job)) == 1


def test_interruption_leaves_latest_completed_checkpoint(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    controller, _, formatters = make_controller(
        tmp_path,
        translator=RecordingTranslator(fail_at=1),
        store=store,
    )
    plan = controller.prepare("https://example.test/1")
    with pytest.raises(RuntimeError, match="interruption"):
        controller.run(plan)
    assert tuple(chunk.index for chunk in store.load(plan.checkpoint_job)) == (0,)
    assert all(formatter.chapters == [] for formatter in formatters)


def test_title_checkpoint_survives_body_failure_and_is_not_requested_again(
    tmp_path: Path,
) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    first, first_translator, _ = make_controller(
        tmp_path,
        translator=RecordingTranslator(fail_at=0),
        store=store,
    )
    first_plan = first.prepare("url")
    with pytest.raises(RuntimeError, match="interruption"):
        first.run(first_plan)
    assert first_translator.title_calls == ["第一章"]
    assert store.load_title(first_plan.checkpoint_job) == "譯名：第一章"
    assert store.load(first_plan.checkpoint_job) == ()

    resumed, resumed_translator, _ = make_controller(tmp_path, store=store)
    resumed_plan = resumed.prepare("url")
    assert resumed_plan.pending_title_count == 0
    resumed.run(resumed_plan)
    assert resumed_translator.title_calls == []
    assert resumed_translator.calls == [0, 1, 2]


def test_failed_term_stage_retries_without_retranslating_body(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    failed_analyzer = RecordingTermAnalyzer(failure=RuntimeError("analysis failed"))
    first, first_translator, _ = make_controller(
        tmp_path, store=store, analyzer=failed_analyzer
    )
    failed_result = first.run(first.prepare("url"))
    assert failed_result.term_memory_error == "analysis failed"
    assert first_translator.calls == [0, 1, 2]

    successful_analyzer = RecordingTermAnalyzer({"一": "壹"})
    resumed, resumed_translator, _ = make_controller(
        tmp_path, store=store, analyzer=successful_analyzer
    )
    resumed_plan = resumed.prepare("url")
    assert resumed_plan.pending_count == 0
    assert resumed_plan.pending_term_count == 3
    result = resumed.run(resumed_plan)
    assert resumed_translator.calls == []
    assert len(successful_analyzer.used_terms) == 3
    assert result.term_memory_error is None


def test_successful_term_stage_is_skipped_on_resume(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    analyzer = RecordingTermAnalyzer()
    first, _, _ = make_controller(tmp_path, store=store, analyzer=analyzer)
    first.run(first.prepare("url"))

    resumed_analyzer = RecordingTermAnalyzer()
    resumed, resumed_translator, _ = make_controller(
        tmp_path, store=store, analyzer=resumed_analyzer
    )
    plan = resumed.prepare("url")
    assert plan.pending_count == 0
    assert plan.pending_title_count == 0
    assert plan.pending_term_count == 0
    updates: list[ProgressUpdate] = []
    resumed.run(plan, progress=updates.append)
    assert resumed_translator.calls == []
    assert resumed_translator.title_calls == []
    assert resumed_analyzer.used_terms == []
    assert [item.source for item in updates].count("term_checkpoint") == 3


def test_cancellation_stops_between_chunks_and_keeps_progress(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    controller, translator, formatters = make_controller(tmp_path, store=store)
    plan = controller.prepare("https://example.test/1")
    cancelled = False

    def progress(_update: ProgressUpdate) -> None:
        nonlocal cancelled
        if _update.source == "translating":
            cancelled = True

    with pytest.raises(TranslationCancelled):
        controller.run(plan, progress=progress, should_cancel=lambda: cancelled)

    assert translator.calls == [0]
    assert len(store.load(plan.checkpoint_job)) == 1
    assert all(formatter.chapters == [] for formatter in formatters)


def test_cancellation_before_first_chunk_does_not_translate(tmp_path: Path) -> None:
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("https://example.test/1")
    with pytest.raises(TranslationCancelled):
        controller.run(plan, should_cancel=lambda: True)
    assert translator.calls == []


def test_controller_rejects_foreign_translation_and_incompatible_plan(tmp_path: Path) -> None:
    controller, _, _ = make_controller(
        tmp_path,
        translator=RecordingTranslator(return_foreign=True),
    )
    plan = controller.prepare("https://example.test/1")
    with pytest.raises(ValueError, match="different source"):
        controller.run(plan)

    incompatible = replace(
        plan,
        checkpoint_job=replace(plan.checkpoint_job, model="other-model"),
    )
    with pytest.raises(ValueError, match="incompatible"):
        controller.run(incompatible)


def test_controller_rejects_wrong_plan_type(tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path)
    with pytest.raises(TypeError, match="TranslationPlan"):
        controller.run(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "replacement",
    [
        {"extractor": object()},
        {"chunker": object()},
        {"translator": object()},
        {"checkpoint_store": object()},
        {"formatters": ()},
        {"formatters": (object(),)},
    ],
)
def test_controller_validates_dependencies(tmp_path: Path, replacement: dict[str, object]) -> None:
    values = {
        "extractor": StaticExtractor(make_chapter()),
        "chunker": TextChunker(10),
        "translator": RecordingTranslator(),
        "checkpoint_store": CheckpointStore(tmp_path),
        "formatters": (RecordingFormatter(".txt"),),
        "output_directory": tmp_path,
    }
    values.update(replacement)
    with pytest.raises((TypeError, ValueError)):
        TranslationController(**values)  # type: ignore[arg-type]
