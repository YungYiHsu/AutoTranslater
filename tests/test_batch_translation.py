"""Batch orchestration is tested without web or model calls."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from core.batch_translation import run_batch, select_batch
from core.controller import ChapterExecutionOptions
from core.exceptions import TranslationCancelled
from core.models import NovelChapterEntry
from tests.test_work_setup import make_work


@pytest.fixture
def work():
    original = make_work()
    return replace(original, chapters=tuple(NovelChapterEntry(n, str(n), f"{original.source_url}{n}/")
                                            for n in range(1, 4)))


def test_selection(work):
    assert [e.number for e in select_batch(work, 1, 3, frozenset({2}), False)] == [1, 3]
    assert len(select_batch(work, 1, 3, frozenset({2}), True)) == 3
    assert select_batch(work, 1, 3, frozenset({1, 2, 3}), False) == ()


@pytest.mark.parametrize(("start", "end"), [(0, 1), (3, 2), (1, 4), (True, 2)])
def test_bad_range(work, start, end):
    with pytest.raises(ValueError):
        select_batch(work, start, end, frozenset(), False)


def test_missing_middle(work):
    work = replace(work, chapters=(work.chapters[0], work.chapters[2]))
    with pytest.raises(ValueError, match="缺少章節"):
        select_batch(work, 1, 3, frozenset(), False)


@dataclass
class Plan:
    completed_chunks: tuple = (1,)
    translated_chapter_title: str | None = "舊譯名"
    completed_term_indexes: frozenset = frozenset({1})
    pending_title_count: int = 0
    pending_count: int = 0
    pending_term_count: int = 0


@pytest.mark.parametrize("failure", [None, "prepare", "run", "save", "terms", "cancel"])
def test_sequential_stop_and_resume(work, failure):
    events, visited, saved = [], [], []
    stop = False

    def factory(entry):
        visited.append(entry.number)

        def prepare(url):
            if entry.number == 2 and failure == "prepare":
                raise ValueError("prepare")
            return Plan()

        def run(plan, **kwargs):
            if entry.number == 2 and failure == "run":
                raise ValueError("run")
            return SimpleNamespace(number=entry.number,
                term_memory_error="terms" if entry.number == 2 and failure == "terms" else None)

        return SimpleNamespace(prepare=prepare, run=run)

    def record(setup, result):
        nonlocal stop
        if result.number == 2 and failure == "save":
            raise OSError("save")
        saved.append(result.number)
        if failure == "cancel":
            stop = True

    def execute():
        run_batch(work.chapters, factory=factory, options=ChapterExecutionOptions(),
            force_numbers=frozenset(), tracker=SimpleNamespace(record_completed=record), setup=None,
            emit=lambda kind, value: events.append((kind, value)), cancelled=lambda: stop, attempts=1)

    if failure:
        with pytest.raises((ValueError, OSError, TranslationCancelled)):
            execute()
        assert 3 not in visited
        assert saved == [1]
    else:
        execute()
        assert visited == saved == [1, 2, 3]
        assert sum(kind == "completed" for kind, _ in events) == 3


def test_force_clears_only_selected_components(work):
    plans, cleared = [], []
    options = ChapterExecutionOptions(True, False, False)
    controller = SimpleNamespace(prepare=lambda _: Plan(),
        clear_checkpoint_components=lambda plan, opts: cleared.append(opts),
        run=lambda plan, **kw: (plans.append(plan) or SimpleNamespace(term_memory_error=None)))
    run_batch(work.chapters[:1], factory=lambda _: controller, options=options,
        force_numbers=frozenset({1}), tracker=SimpleNamespace(record_completed=lambda *args: None),
        setup=None, emit=lambda *args: None, cancelled=lambda: False, attempts=1)
    assert cleared == [options]
    assert plans[0].translated_chapter_title is None
    assert plans[0].completed_chunks == (1,)
