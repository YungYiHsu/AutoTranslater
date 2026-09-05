"""Threaded worker tests using only local controller strategies."""

from __future__ import annotations

import queue
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from core.checkpoint import CheckpointStore
from core.models import NovelChapterEntry, NovelWork, TextChunk, TranslatedChunk
from core.work_setup import WorkSetupService
from extractors.work_base import BaseWorkExtractor
from tests.fake_work import FakeWorkTranslator
from tests.test_controller import RecordingTranslator, make_controller
from ui.messages import WorkerMessage
from ui.worker import TranslationWorker


def next_message(worker: TranslationWorker) -> WorkerMessage:
    return worker.messages.get(timeout=2)


class StaticWorkExtractor(BaseWorkExtractor):
    def __init__(self, work: NovelWork) -> None:
        self.work = work

    def can_handle(self, url: str) -> bool:
        return True

    def extract(self, url: str) -> NovelWork:
        return self.work


def make_work() -> NovelWork:
    return NovelWork(
        "n1234ab",
        "https://ncode.syosetu.com/n1234ab/",
        "測試作品",
        "作者",
        "摘要",
        (
            NovelChapterEntry(
                1,
                "第一章",
                "https://ncode.syosetu.com/n1234ab/1/",
            ),
        ),
    )


def test_worker_selects_and_sets_up_work_without_touching_gui(tmp_path: Path) -> None:
    work = make_work()
    worker = TranslationWorker()
    worker.start_select_work(
        StaticWorkExtractor(work),
        WorkSetupService(tmp_path, FakeWorkTranslator()),
        work.source_url,
    )

    messages: list[WorkerMessage] = []
    while not messages or messages[-1].kind != "work_setup_done":
        messages.append(next_message(worker))

    assert [message.kind for message in messages] == [
        "work_progress",
        "work_found",
        "work_progress",
        "work_progress",
        "work_progress",
        "work_progress",
        "work_progress",
        "work_setup_done",
    ]
    assert messages[1].payload is work
    assert messages[-1].payload.work.source_work is work


def test_worker_requests_api_key_only_when_memory_needs_translation(tmp_path: Path) -> None:
    work = make_work()
    worker = TranslationWorker()
    worker.start_select_work(
        StaticWorkExtractor(work),
        WorkSetupService(tmp_path, None),
        work.source_url,
    )

    messages: list[WorkerMessage] = []
    while not messages or messages[-1].kind != "api_key_required":
        messages.append(next_message(worker))
    assert messages[-1].payload is work


def test_worker_rejects_requested_chapter_missing_from_work_before_setup(
    tmp_path: Path,
) -> None:
    work = make_work()
    worker = TranslationWorker()
    worker.start_select_work(
        StaticWorkExtractor(work),
        WorkSetupService(tmp_path, FakeWorkTranslator()),
        work.source_url,
        requested_chapter=2,
    )

    assert next_message(worker).kind == "work_progress"
    error = next_message(worker)
    assert error.kind == "error"
    assert "第 2 章" in str(error.payload)
    assert not (tmp_path / "測試作品").exists()


def test_worker_prepares_and_runs_without_touching_gui(tmp_path: Path) -> None:
    controller, translator, _ = make_controller(tmp_path)
    worker = TranslationWorker()

    worker.start_prepare(controller, "https://example.test/1")
    prepared = next_message(worker)
    assert prepared.kind == "prepare_done"
    assert translator.calls == []

    worker.start_run(controller, prepared.payload)
    kinds = []
    while "run_done" not in kinds:
        kinds.append(next_message(worker).kind)
    assert kinds == [
        "progress",
        "progress",
        "progress",
        "progress",
        "progress",
        "progress",
        "progress",
        "run_done",
    ]
    assert translator.calls == [0, 1, 2]


def test_worker_forwards_run_errors(tmp_path: Path) -> None:
    controller, _, _ = make_controller(
        tmp_path,
        translator=RecordingTranslator(fail_at=0),
    )
    worker = TranslationWorker()
    worker.start_prepare(controller, "url")
    plan = next_message(worker).payload
    worker.start_run(controller, plan)
    title_progress = next_message(worker)
    chunk_progress = next_message(worker)
    assert title_progress.kind == "progress"
    assert chunk_progress.kind == "progress"
    message = next_message(worker)
    assert message.kind == "error"
    assert isinstance(message.payload, RuntimeError)


class BlockingTranslator(RecordingTranslator):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    def translate(
        self,
        chunk: TextChunk,
        terms: Mapping[str, str] | None = None,
    ) -> TranslatedChunk:
        self.started.set()
        self.release.wait(timeout=2)
        return super().translate(chunk, terms)


def test_worker_cancel_waits_for_current_chunk_then_stops(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    controller, translator, _ = make_controller(
        tmp_path,
        translator=BlockingTranslator(started, release),
        store=CheckpointStore(tmp_path / "checkpoints"),
    )
    plan = controller.prepare("url")
    worker = TranslationWorker()

    worker.start_run(controller, plan)
    assert started.wait(timeout=2)
    worker.cancel()
    release.set()

    messages: list[WorkerMessage] = []
    while not messages or messages[-1].kind not in {"cancelled", "run_done", "error"}:
        messages.append(next_message(worker))
    assert [message.kind for message in messages] == [
        "progress",
        "progress",
        "progress",
        "cancelled",
    ]
    assert translator.calls == [0]


def test_worker_rejects_a_second_concurrent_task(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    controller, _, _ = make_controller(
        tmp_path,
        translator=BlockingTranslator(started, release),
    )
    worker = TranslationWorker()
    plan = controller.prepare("url")
    worker.start_run(controller, plan)
    assert started.wait(timeout=2)
    with pytest.raises(RuntimeError, match="already running"):
        worker.start_prepare(controller, "url")
    worker.cancel()
    release.set()
    while True:
        try:
            if next_message(worker).kind == "cancelled":
                break
        except queue.Empty:
            pytest.fail("worker did not finish cancellation")
