"""Chunk count overrides and GUI size persistence without external requests."""

from pathlib import Path

import pytest

from core.config import load_config
from core.exceptions import ChunkingError
from core.text_chunker import TextChunker
from tests.test_app_model import Value, make_app
from tests.test_controller import make_controller


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_exact_count_preserves_source(count: int) -> None:
    source = " 一。\n\n二。三。\n四。 \n"
    chunks = TextChunker(2).split_count(source, count)
    assert len(chunks) == count
    assert "".join(chunk.text for chunk in chunks) == source
    assert all(chunk.text.strip() for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(count))


@pytest.mark.parametrize("count", [0, -1, True, 21, 100])
def test_invalid_count_does_not_force_split(count: int) -> None:
    with pytest.raises(ChunkingError):
        TextChunker(4000).split_count("一。二。", count)


def test_manual_count_upper_boundary() -> None:
    source = "一。\n\n" * 30
    chunks = TextChunker(4000).split_count(source, 20)
    assert len(chunks) == 20
    assert "".join(chunk.text for chunk in chunks) == source
    with pytest.raises(ChunkingError, match="1 到 20"):
        TextChunker(4000).split_count(source, 21)


def test_resegment_uses_cached_source_and_rechecks_checkpoint(tmp_path: Path) -> None:
    controller, translator, _ = make_controller(tmp_path)
    plan = controller.prepare("test")
    controller.run(plan)
    calls = list(translator.calls)
    title_calls = list(translator.title_calls)
    controller._extractor.extract = lambda _url: pytest.fail("must not fetch source again")
    changed = controller.resegment(plan, 1)
    assert changed.total_chunks == 1
    assert changed.completed_count == 0
    assert changed.translated_chapter_title is None
    assert changed.pending_count == 1
    assert changed.checkpoint_job.job_id != plan.checkpoint_job.job_id
    restored = controller.resegment(changed, 3)
    assert restored.checkpoint_job.job_id == plan.checkpoint_job.job_id
    assert restored.completed_count == 3
    assert translator.calls == calls
    assert translator.title_calls == title_calls


def test_size_change_persists_and_invalidates_plan(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.chunk_size_text = Value("2000")
    app.chunk_count_text = Value("3")
    app.plan = object()
    assert app._commit_chunk_size()
    assert load_config(tmp_path / "config.json").chunk_size == 2000
    assert app.plan is None
    assert app.chunk_count_text.get() == ""
    assert app.state == "work_ready"


def test_count_override_does_not_change_global_size(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    controller, _, _ = make_controller(tmp_path)
    app.controller = controller
    app.plan = controller.prepare("test")
    app.state = "chapter_ready"
    app.chunk_size_text = Value(str(app.config.chunk_size))
    app.chunk_count_text = Value("1")
    app._show_prepared_plan = lambda plan: app.chunk_count_text.set(str(plan.total_chunks))
    app.resegment_chapter()
    assert app.plan.total_chunks == 1
    assert app.config.chunk_size == 4000
    assert not (tmp_path / "config.json").exists()
