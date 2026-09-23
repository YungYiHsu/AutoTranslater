"""Work directory, memory reuse, and synopsis overwrite tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.exceptions import (
    WorkDirectoryConflictError,
    WorkMemoryError,
    WorkSetupCancelled,
)
from core.models import NovelChapterEntry, NovelWork, TranslatedNovelWork
from core.work_setup import WorkSetupService
from translators.work_base import BaseWorkTranslator


def make_work(
    *,
    ncode: str = "n1234ab",
    title: str = "測試/作品",
    synopsis: str = "原始摘要。",
) -> NovelWork:
    return NovelWork(
        ncode=ncode,
        source_url=f"https://ncode.syosetu.com/{ncode}/",
        title=title,
        author="作者",
        synopsis=synopsis,
        chapters=(
            NovelChapterEntry(
                1,
                "第一章",
                f"https://ncode.syosetu.com/{ncode}/1/",
            ),
        ),
    )


class RecordingWorkTranslator(BaseWorkTranslator):
    def __init__(self, *, model: str = "model-a") -> None:
        self.calls: list[NovelWork] = []
        self._model = model

    @property
    def provider(self) -> str:
        return "test-provider"

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_identity(self) -> str:
        return f"prompt-for-{self._model}"

    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        self.calls.append(work)
        return TranslatedNovelWork(
            work,
            f"中文：{work.title}",
            f"中文摘要：{work.synopsis}",
            self.provider,
            self.model,
            self.prompt_identity,
        )


def test_first_setup_creates_fixed_files_and_progress(tmp_path: Path) -> None:
    translator = RecordingWorkTranslator()
    stages: list[str] = []
    result = WorkSetupService(tmp_path, translator).prepare(make_work(), progress=stages.append)

    assert result.work_directory == tmp_path / "測試_作品"
    assert result.memory_path.name == "work.json"
    assert result.synopsis_path.name == "synopsis.txt"
    assert result.terms_path.name == "terms.json"
    assert json.loads(result.terms_path.read_text(encoding="utf-8")) == {}
    assert not result.reused_memory
    assert len(translator.calls) == 1
    assert stages == [
        "checking_directory",
        "checking_memory",
        "translating_metadata",
        "saving_memory",
        "completed",
    ]
    memory = json.loads(result.memory_path.read_text(encoding="utf-8"))
    assert memory["identity"]["ncode"] == "n1234ab"
    assert memory["translation"]["title"] == "中文：測試/作品"
    assert "中文摘要：原始摘要。" in result.synopsis_path.read_text(encoding="utf-8-sig")


def test_valid_memory_is_reused_even_when_model_and_prompt_change(tmp_path: Path) -> None:
    first = RecordingWorkTranslator(model="old-model")
    WorkSetupService(tmp_path, first).prepare(make_work())
    replacement = RecordingWorkTranslator(model="new-model")

    result = WorkSetupService(tmp_path, replacement).prepare(make_work())

    assert result.reused_memory
    assert replacement.calls == []
    assert result.work.model == "old-model"


def test_legacy_identity_without_site_or_work_id_is_reused_without_rewrite(
    tmp_path: Path,
) -> None:
    work = make_work()
    first = WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(work)
    payload = json.loads(first.memory_path.read_text(encoding="utf-8"))
    payload["identity"].pop("site")
    payload["identity"].pop("work_id")
    legacy_content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    first.memory_path.write_text(legacy_content, encoding="utf-8")
    translator = RecordingWorkTranslator(model="new-model")

    result = WorkSetupService(tmp_path, translator).prepare(work)

    assert result.reused_memory
    assert translator.calls == []
    assert first.memory_path.read_text(encoding="utf-8") == legacy_content


def test_fake_memory_is_not_reused_by_a_real_translator(tmp_path: Path) -> None:
    from tests.fake_work import FakeWorkTranslator

    WorkSetupService(tmp_path, FakeWorkTranslator()).prepare(make_work())
    translator = RecordingWorkTranslator(model="real-model")

    result = WorkSetupService(tmp_path, translator).prepare(make_work())

    assert not result.reused_memory
    assert len(translator.calls) == 1
    assert result.work.model == "real-model"


def test_added_chapters_do_not_invalidate_metadata_memory(tmp_path: Path) -> None:
    original = make_work()
    WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(original)
    expanded = NovelWork(
        ncode=original.ncode,
        source_url=original.source_url,
        title=original.title,
        author=original.author,
        synopsis=original.synopsis,
        chapters=original.chapters + (NovelChapterEntry(2, "第二章", f"{original.source_url}2/"),),
    )
    translator = RecordingWorkTranslator()
    result = WorkSetupService(tmp_path, translator).prepare(expanded)
    assert result.reused_memory
    assert translator.calls == []


def test_changed_work_title_reuses_identity_directory(tmp_path: Path) -> None:
    original = make_work(title="舊作品名稱")
    first = WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(original)
    changed = make_work(title="新作品名稱")

    result = WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(changed)

    assert result.work_directory == first.work_directory
    assert result.work_directory.name == "舊作品名稱"
    assert not (tmp_path / "新作品名稱").exists()


def test_same_title_is_isolated_by_site_output_root(tmp_path: Path) -> None:
    syosetu = WorkSetupService(tmp_path / "Syosetu", RecordingWorkTranslator()).prepare(
        make_work(title="同名作品")
    )
    kakuyomu_work = NovelWork(
        "123456789",
        "https://kakuyomu.jp/works/123456789",
        "同名作品",
        "作者",
        "摘要",
        (
            NovelChapterEntry(
                1,
                "第一話",
                "https://kakuyomu.jp/works/123456789/episodes/111",
            ),
        ),
    )
    kakuyomu = WorkSetupService(tmp_path / "Kakuyomu", RecordingWorkTranslator()).prepare(
        kakuyomu_work
    )

    assert syosetu.work_directory == tmp_path / "Syosetu" / "同名作品"
    assert kakuyomu.work_directory == tmp_path / "Kakuyomu" / "同名作品"


def test_missing_synopsis_is_rebuilt_without_translation(tmp_path: Path) -> None:
    service = WorkSetupService(tmp_path, RecordingWorkTranslator())
    first = service.prepare(make_work())
    first.synopsis_path.unlink()
    translator = RecordingWorkTranslator()

    result = WorkSetupService(tmp_path, translator).prepare(make_work())

    assert result.reused_memory
    assert result.synopsis_path.exists()
    assert translator.calls == []


def test_modified_synopsis_and_changed_source_requires_confirmation(tmp_path: Path) -> None:
    work = make_work()
    first = WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(work)
    first.synopsis_path.write_text("我的手動摘要", encoding="utf-8-sig")
    changed = make_work(synopsis="網站更新後的摘要。")
    translator = RecordingWorkTranslator()

    with pytest.raises(WorkSetupCancelled, match="取消覆蓋"):
        WorkSetupService(tmp_path, translator).prepare(
            changed, confirm_overwrite=lambda _path: False
        )

    assert translator.calls == []
    assert first.synopsis_path.read_text(encoding="utf-8-sig") == "我的手動摘要"


def test_confirming_overwrite_replaces_modified_synopsis(tmp_path: Path) -> None:
    work = make_work()
    first = WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(work)
    first.synopsis_path.write_text("我的手動摘要", encoding="utf-8-sig")
    changed = make_work(synopsis="網站更新後的摘要。")
    translator = RecordingWorkTranslator()
    asked: list[Path] = []

    def confirm(path: Path) -> bool:
        asked.append(path)
        return True

    result = WorkSetupService(tmp_path, translator).prepare(
        changed,
        confirm_overwrite=confirm,
    )

    assert asked == [first.synopsis_path]
    assert len(translator.calls) == 1
    assert "網站更新後的摘要。" in result.synopsis_path.read_text(encoding="utf-8-sig")


def test_unchanged_generated_synopsis_updates_without_confirmation(tmp_path: Path) -> None:
    WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(make_work())
    translator = RecordingWorkTranslator()
    result = WorkSetupService(tmp_path, translator).prepare(
        make_work(synopsis="新版摘要。"),
        confirm_overwrite=lambda _path: pytest.fail("should not ask"),
    )
    assert not result.reused_memory
    assert len(translator.calls) == 1


def test_same_named_directory_for_another_ncode_is_an_error(tmp_path: Path) -> None:
    WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(make_work(ncode="n1111aa"))
    with pytest.raises(WorkDirectoryConflictError, match="另一部作品"):
        WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(make_work(ncode="n2222bb"))


def test_corrupt_memory_is_not_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / "測試_作品"
    directory.mkdir()
    memory_path = directory / "work.json"
    memory_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(WorkMemoryError, match="已損壞"):
        WorkSetupService(tmp_path, RecordingWorkTranslator()).prepare(make_work())
    assert memory_path.read_text(encoding="utf-8") == "not-json"
