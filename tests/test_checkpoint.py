"""Tests for resumable, content-addressed translation checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.checkpoint import CheckpointJob, CheckpointStore
from core.exceptions import CheckpointError
from core.models import NovelChapter, TextChunk, TranslatedChunk


def make_job(
    *,
    texts: tuple[str, ...] = ("一。\n\n", "二。\n\n", "三。"),
    url: str = "https://example.test/novel/1",
    provider: str = "gemini",
    model: str = "test-model",
    prompt: str = "翻譯成繁體中文",
) -> CheckpointJob:
    chapter = NovelChapter(
        title="測試小說",
        chapter_title="第一章",
        source_url=url,
        original_text="".join(texts),
    )
    chunks = tuple(TextChunk(index=index, text=text) for index, text in enumerate(texts))
    return CheckpointJob.create(
        source_chapter=chapter,
        chunks=chunks,
        provider=provider,
        model=model,
        system_prompt=prompt,
    )


def translated(job: CheckpointJob, index: int, text: str | None = None) -> TranslatedChunk:
    return TranslatedChunk(
        source_chunk=job.chunks[index],
        translated_text=text or f"翻譯 {index}",
        translated_chapter_title="中文第一章" if index == 0 else None,
    )


def read_state(store: CheckpointStore, job: CheckpointJob) -> dict[str, Any]:
    return json.loads((store.path_for(job) / "metadata.json").read_text(encoding="utf-8"))


def write_state(store: CheckpointStore, job: CheckpointJob, state: Any) -> None:
    store.path_for(job).mkdir(parents=True, exist_ok=True)
    (store.path_for(job) / "metadata.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def test_job_identity_is_deterministic_and_uses_safe_hash_filename(tmp_path: Path) -> None:
    first = make_job()
    second = make_job()
    path = CheckpointStore(tmp_path).path_for(first)
    assert first.job_id == second.job_id
    assert len(first.job_id) == 64
    assert path.name == first.job_id
    assert set(path.name) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "changed",
    [
        {"prompt": "新的翻譯規則"},
        {"model": "other-model"},
        {"provider": "other-provider"},
        {"url": "https://example.test/novel/2"},
        {"texts": ("一。", "\n\n二。\n\n", "三。")},
        {"texts": ("不同原文。",)},
    ],
)
def test_job_identity_changes_for_incompatible_work(changed: dict[str, Any]) -> None:
    assert make_job().job_id != make_job(**changed).job_id


def test_new_job_loads_as_empty_without_creating_directory(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "missing")
    assert store.load(make_job()) == ()
    assert not (tmp_path / "missing").exists()


def test_save_and_load_consecutive_translation_prefix(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    first_path = store.save_chunk(job, translated(job, 0))
    second_path = store.save_chunk(job, translated(job, 1, "　第二段。\n"))

    loaded = store.load(job)
    assert first_path.name == "0000.json"
    assert second_path.name == "0001.json"
    assert tuple(item.index for item in loaded) == (0, 1)
    assert loaded[0].source_chunk is job.chunks[0]
    assert loaded[1].translated_text == "　第二段。\n"
    state = read_state(store, job)
    assert state["schema_version"] == 3
    assert state["job"] == job.metadata()
    assert "api_key" not in json.dumps(state).lower()
    assert job.source_chapter.original_text not in json.dumps(state, ensure_ascii=False)


def test_repeated_identical_save_is_idempotent(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    path = store.save_chunk(job, translated(job, 0))
    original = path.read_bytes()
    assert store.save_chunk(job, translated(job, 0)) == path
    assert path.read_bytes() == original
    assert len(store.load(job)) == 1


def test_save_rejects_gap_foreign_chunk_and_conflicting_translation(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    with pytest.raises(CheckpointError, match="without gaps"):
        store.save_chunk(job, translated(job, 1))

    store.save_chunk(job, translated(job, 0))
    with pytest.raises(CheckpointError, match="different translation"):
        store.save_chunk(job, translated(job, 0, "另一個結果"))

    other = make_job(texts=("外部原文。",))
    with pytest.raises(CheckpointError, match="does not belong"):
        store.save_chunk(job, translated(other, 0))


def test_resume_translates_only_missing_chunks(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    initial_calls: list[int] = []
    for chunk in job.chunks[:2]:
        initial_calls.append(chunk.index)
        store.save_chunk(job, translated(job, chunk.index))

    resumed_calls: list[int] = []
    completed = list(store.load(job))
    for chunk in job.chunks[len(completed) :]:
        resumed_calls.append(chunk.index)
        result = translated(job, chunk.index)
        store.save_chunk(job, result)

    assert initial_calls == [0, 1]
    assert resumed_calls == [2]
    assert len(store.load(job)) == 3


def test_atomic_failure_preserves_previous_checkpoint(tmp_path: Path) -> None:
    normal_store = CheckpointStore(tmp_path)
    job = make_job()
    normal_store.save_chunk(job, translated(job, 0))
    first_path = normal_store.path_for(job) / "chunks" / "0000.json"
    original = first_path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replacement failure")

    failing_store = CheckpointStore(tmp_path, replace_file=fail_replace)
    with pytest.raises(CheckpointError, match="Unable to save"):
        failing_store.save_chunk(job, translated(job, 1))

    assert first_path.read_bytes() == original
    assert len(normal_store.load(job)) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_clear_is_explicit_and_idempotent(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    store.save_chunk(job, translated(job, 0))
    assert store.clear(job)
    assert not store.path_for(job).exists()
    assert not store.clear(job)


def test_title_chunk_and_term_checkpoints_are_independent(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    chunk = translated(job, 0)

    title_path = store.save_title(job, "中文第一章")
    chunk_path = store.save_chunk(job, chunk)
    term_path = store.save_term_completed(job, chunk, "terms-v1")

    assert title_path.name == "title.json"
    assert chunk_path.name == "0000.json"
    assert term_path.name == "0000.done.json"
    assert store.load_title(job) == "中文第一章"
    loaded = store.load(job)
    assert tuple(item.translated_text for item in loaded) == (chunk.translated_text,)
    assert loaded[0].translated_chapter_title is None
    assert store.term_completed(job, chunk, "terms-v1")
    assert not store.term_completed(job, chunk, "terms-v2")
    assert not store.term_completed(
        job, TranslatedChunk(chunk.source_chunk, "不同譯文"), "terms-v1"
    )
    assert read_state(store, job)["schema_version"] == 3
    assert json.loads(term_path.read_text(encoding="utf-8"))["success"] is True


def test_load_rejects_malformed_component_without_deleting_it(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    path = store.save_chunk(job, translated(job, 0))
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CheckpointError, match="valid chunk checkpoint"):
        store.load(job)
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_legacy_v2_checkpoint_is_migrated(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    legacy_path = tmp_path / f"{job.job_id}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "job": job.metadata(),
                "translations": [
                    {
                        "index": 0,
                        "source_hash": job.chunks[0].content_hash,
                        "translated_text": "舊譯文",
                        "translated_chapter_title": "舊標題",
                    }
                ],
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert store.load_title(job) == "舊標題"
    assert tuple(item.translated_text for item in store.load(job)) == ("舊譯文",)
    assert not legacy_path.exists()
    assert not store.term_completed(job, store.load(job)[0], "terms-v1")


def test_public_methods_reject_wrong_types(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    job = make_job()
    with pytest.raises(TypeError, match="CheckpointJob"):
        store.path_for(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="TranslatedChunk"):
        store.save_chunk(job, object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    [
        lambda job: {"source_chapter": object(), "chunks": job.chunks},
        lambda job: {"source_chapter": job.source_chapter, "chunks": ()},
        lambda job: {"source_chapter": job.source_chapter, "chunks": (object(),)},
        lambda job: {
            "source_chapter": job.source_chapter,
            "chunks": (TextChunk(index=1, text=job.source_chapter.original_text),),
        },
        lambda job: {
            "source_chapter": job.source_chapter,
            "chunks": (TextChunk(index=0, text="不相同"),),
        },
    ],
)
def test_job_rejects_invalid_source_contract(factory: Any) -> None:
    base = make_job()
    values = factory(base)
    with pytest.raises((TypeError, ValueError)):
        CheckpointJob.create(
            provider="gemini",
            model="model",
            system_prompt="prompt",
            **values,
        )


@pytest.mark.parametrize(
    ("provider", "model", "prompt"),
    [("", "model", "prompt"), ("gemini", "", "prompt"), ("gemini", "model", "")],
)
def test_job_rejects_blank_identity_values(provider: str, model: str, prompt: str) -> None:
    base = make_job()
    with pytest.raises(ValueError):
        CheckpointJob.create(
            source_chapter=base.source_chapter,
            chunks=base.chunks,
            provider=provider,
            model=model,
            system_prompt=prompt,
        )
