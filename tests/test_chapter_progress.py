"""Compact chapter completion tracking and legacy output reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.chapter_progress import ChapterCompletionTracker
from core.controller import TranslationResult
from core.exceptions import WorkMemoryError
from core.models import (
    NovelChapter,
    NovelChapterEntry,
    NovelWork,
    TextChunk,
    TranslatedChapter,
    TranslatedChunk,
)
from core.work_memory import text_hash
from core.work_setup import WorkSetupResult, WorkSetupService
from tests.fake_work import FakeWorkTranslator


def make_work() -> NovelWork:
    ncode = "n1234ab"
    return NovelWork(
        ncode,
        f"https://ncode.syosetu.com/{ncode}/",
        "測試作品",
        "作者",
        "摘要",
        tuple(
            NovelChapterEntry(
                number,
                f"第{number}章",
                f"https://ncode.syosetu.com/{ncode}/{number}/",
            )
            for number in (1, 3, 5)
        ),
    )


def make_setup(tmp_path: Path) -> WorkSetupResult:
    return WorkSetupService(tmp_path, FakeWorkTranslator()).prepare(make_work())


def output_paths(setup: WorkSetupResult, number: int) -> tuple[Path, Path]:
    chapter = setup.work.source_work.get_chapter(number)
    return ChapterCompletionTracker.output_paths(setup, chapter)


def touch_outputs(setup: WorkSetupResult, number: int, *, txt: bool, html: bool) -> None:
    txt_path, html_path = output_paths(setup, number)
    if txt:
        txt_path.write_text("txt", encoding="utf-8")
    if html:
        html_path.write_text("html", encoding="utf-8")


def make_result(setup: WorkSetupResult, number: int) -> TranslationResult:
    work = setup.work.source_work
    entry = work.get_chapter(number)
    source = NovelChapter(
        title=work.title,
        chapter_title=entry.title,
        source_url=entry.source_url,
        original_text="原始正文。",
    )
    chunk = TextChunk(0, source.original_text)
    chapter = TranslatedChapter(
        source,
        (TranslatedChunk(chunk, "翻譯正文。"),),
        "fake",
        "model",
    )
    return TranslationResult(chapter, output_paths(setup, number), 0, 1)


def test_reconcile_imports_complete_legacy_outputs_once(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    touch_outputs(setup, 1, txt=True, html=True)
    tracker = ChapterCompletionTracker()

    progress = tracker.reconcile(setup)

    assert progress.completed_numbers == {1}
    assert progress.partial_numbers == set()
    assert progress.next_number == 3
    memory = json.loads(setup.memory_path.read_text(encoding="utf-8"))
    assert memory["schema_version"] == 2
    assert memory["completed_chapters"]["1"]["source_hash"] == ""
    first_content = setup.memory_path.read_bytes()
    tracker.reconcile(setup)
    assert setup.memory_path.read_bytes() == first_content


def test_reconcile_still_recognizes_previous_filename_order(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    (setup.work_directory / "測試作品 - 1.txt").write_text("txt", encoding="utf-8")
    (setup.work_directory / "測試作品 - 1.html").write_text("html", encoding="utf-8")

    progress = ChapterCompletionTracker().reconcile(setup)

    assert progress.completed_numbers == {1}
    assert output_paths(setup, 1)[0].name == "0001 - 測試作品.txt"


@pytest.mark.parametrize(("txt", "html"), [(True, False), (False, True)])
def test_reconcile_treats_one_output_as_partial(tmp_path: Path, txt: bool, html: bool) -> None:
    setup = make_setup(tmp_path)
    touch_outputs(setup, 1, txt=txt, html=html)
    progress = ChapterCompletionTracker().reconcile(setup)
    assert progress.completed_numbers == set()
    assert progress.partial_numbers == {1}
    assert progress.next_number == 1


def test_reconcile_supports_non_contiguous_chapters_and_all_complete(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    for number in (1, 3, 5):
        touch_outputs(setup, number, txt=True, html=True)
    progress = ChapterCompletionTracker().reconcile(setup)
    assert progress.completed_numbers == {1, 3, 5}
    assert progress.next_number == 5
    assert progress.all_completed


def test_record_completed_saves_source_hash_and_updates_same_entry(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    touch_outputs(setup, 3, txt=True, html=True)
    tracker = ChapterCompletionTracker()
    result = make_result(setup, 3)

    progress = tracker.record_completed(setup, result)

    assert 3 in progress.completed_numbers
    completion = tracker.completion_for(setup, 3)
    assert completion is not None
    assert completion.source_hash == text_hash("原始正文。")
    memory = json.loads(setup.memory_path.read_text(encoding="utf-8"))
    assert list(memory["completed_chapters"]) == ["3"]


def test_record_completed_requires_both_fixed_outputs(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    touch_outputs(setup, 1, txt=True, html=False)
    with pytest.raises(WorkMemoryError, match="尚未完整建立"):
        ChapterCompletionTracker().record_completed(setup, make_result(setup, 1))


def test_missing_recorded_output_is_not_treated_as_complete(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    touch_outputs(setup, 1, txt=True, html=True)
    tracker = ChapterCompletionTracker()
    tracker.record_completed(setup, make_result(setup, 1))
    txt_path, _html_path = output_paths(setup, 1)
    txt_path.unlink()

    progress = tracker.reconcile(setup)

    assert 1 not in progress.completed_numbers
    assert 1 in progress.partial_numbers
    assert progress.next_number == 1
