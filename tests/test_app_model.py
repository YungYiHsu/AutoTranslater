from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import AppConfig
from core.models import NovelChapter, TextChunk, TranslatedChapter, TranslatedChunk
from formatters.utils import TXT_BODY_SEPARATOR
from ui.app import DesktopApp


class Value:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


def make_app(tmp_path: Path, model: str = "gemini-3.5-flash") -> Any:
    app: Any = DesktopApp.__new__(DesktopApp)
    app.root = SimpleNamespace()
    app.app_directory = tmp_path
    app.config = AppConfig(model=model)
    app.model_name = Value(model)
    app.codex_effort = Value("default")
    app._codex_models = []
    app._codex_catalog_requested = True
    app.retry_count = Value(str(app.config.retry_attempts))
    app.retry_entry = SimpleNamespace(configure=lambda **_kwargs: None)
    app.mode = Value("gemini")
    app.model_entry = SimpleNamespace(configure=lambda **_kwargs: None)
    app.model_frame = SimpleNamespace(set_title=lambda _text: None)
    app.state = "work_ready"
    app.plan = None
    app.controller = None
    app.result = None
    app.work_result = SimpleNamespace()
    app.summary = Value("")
    app.translate_title = Value(True)
    app.translate_body = Value(True)
    app.update_terms = Value(True)
    app.update_terms_check = SimpleNamespace(configure=lambda **_kwargs: None)
    app._analyzed_mode = None
    app._term_organization_service = SimpleNamespace()
    app._apply_state = lambda state, status=None: setattr(app, "state", state)
    return app


def test_chapter_execution_options_follow_gui_selection(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.translate_title.set(False)
    app.update_terms.set(False)

    options = app._execution_options()

    assert options.translate_title is False
    assert options.translate_body is True
    assert options.update_terms is False


def test_disabling_body_translation_also_disables_term_analysis(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    states: list[str] = []
    app.state = "chapter_ready"
    app.translate_body.set(False)
    app.update_terms_check = SimpleNamespace(
        configure=lambda **kwargs: states.append(kwargs["state"])
    )

    app._on_translation_selection_changed()

    assert app.update_terms.get() is False
    assert states == ["disabled"]


def test_manual_model_is_saved_to_config(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.model_name.set("  gemini-custom-model  ")

    assert app._commit_model_selection() is True

    assert app.config.model == "gemini-custom-model"
    assert app.model_name.get() == "gemini-custom-model"
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["model"] == "gemini-custom-model"
    assert saved["saved_models"] == ["gemini-custom-model"]
    assert app._term_organization_service is None


def test_previous_custom_model_remains_in_dropdown_after_switch(tmp_path: Path) -> None:
    app = make_app(tmp_path, model="gemini-custom-old")
    configured_values: list[tuple[str, ...]] = []
    app.model_entry = SimpleNamespace(
        configure=lambda **kwargs: configured_values.append(kwargs["values"])
    )
    app.model_name.set("gemini-3.5-flash")

    assert app._commit_model_selection() is True

    assert app.config.saved_models == ("gemini-custom-old",)
    assert "gemini-custom-old" in configured_values[-1]


def test_retry_count_is_saved_without_invalidating_chapter(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.plan = SimpleNamespace()
    app.controller = SimpleNamespace()
    app.retry_count.set("5")

    assert app._commit_model_selection() is True
    assert app.config.retry_attempts == 5
    assert app.plan is not None
    assert app.controller is not None
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["retry_attempts"] == 5


@pytest.mark.parametrize("value", ["-1", "11", "３", "1.5", "abc"])
def test_retry_count_rejects_values_outside_zero_to_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    app = make_app(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(
        "ui.app.messagebox.showwarning",
        lambda _title, message, **_kwargs: warnings.append(message),
    )
    app.retry_count.set(value)

    assert app._commit_model_selection() is False
    assert app.retry_count.get() == "0"
    assert "0 到 10" in warnings[-1]
    assert not (tmp_path / "config.json").exists()


def test_model_change_invalidates_analyzed_chapter(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.plan = SimpleNamespace()
    app.controller = SimpleNamespace()
    app.result = SimpleNamespace()
    app._analyzed_mode = "gemini"
    app.state = "chapter_ready"
    app.model_name.set("gemini-3.5-flash-lite")

    assert app._commit_model_selection() is True

    assert app.plan is None
    assert app.controller is None
    assert app.result is None
    assert app._analyzed_mode is None
    assert app.state == "work_ready"
    assert "重新分析章節" in app.summary.get()


def test_model_dropdown_follows_execution_mode(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    entry_updates: list[dict[str, Any]] = []
    frame_updates: list[dict[str, Any]] = []
    app.model_entry = SimpleNamespace(configure=lambda **kwargs: entry_updates.append(kwargs))
    app.model_frame = SimpleNamespace(set_title=lambda text: frame_updates.append({"text": text}))

    app.mode.set("local")
    app._sync_model_selector(busy=False)

    assert app.model_name.get() == "尚未提供本地模型"
    assert entry_updates[-1]["values"] == ("尚未提供本地模型",)
    assert entry_updates[-1]["state"] == "readonly"
    assert frame_updates[-1]["text"] == "本地模型"
    assert app._commit_model_selection() is True
    assert not (tmp_path / "config.json").exists()

    app.mode.set("gemini")
    app._sync_model_selector(busy=False)

    assert app.model_name.get() == "gemini-3.5-flash"
    assert "gemini-3.5-flash-lite" in entry_updates[-1]["values"]
    assert entry_updates[-1]["state"] == "normal"
    assert frame_updates[-1]["text"] == "Gemini API 模型"


def test_output_chapter_number_has_no_leading_zeroes(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    txt_path = tmp_path / "0012 - 測試.txt"
    html_path = tmp_path / "0012 - 測試.html"
    source_chapter = SimpleNamespace(source_url="https://example.test/work/12/", chapter_number=12)
    app.result = SimpleNamespace(
        output_paths=(txt_path, html_path),
        chapter=SimpleNamespace(source_chapter=source_chapter),
    )

    assert app._output_chapter_number((txt_path, html_path)) == 12


def test_html_regeneration_reads_all_metadata_from_txt(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    txt_path = tmp_path / "0001 - 測試.txt"
    html_path = tmp_path / "0001 - 測試.html"
    txt_path.write_text(
        "作品：手動中文作品\n"
        "章節：手動中文章節\n"
        "日文作品名：手動日文作品\n"
        "日文章節名：手動日文章節\n"
        "來源：https://example.test/manual\n"
        "翻譯引擎：其他軟體 / 手動翻譯（手動）\n\n"
        f"{TXT_BODY_SEPARATOR}\n\n手動正文",
        encoding="utf-8-sig",
    )
    source = NovelChapter(
        "原日文作品",
        "原日文章節",
        "https://ncode.syosetu.com/n1234ab/1/",
        "原文",
        chapter_number=1,
    )
    translated = TranslatedChapter(
        source,
        (TranslatedChunk(TextChunk(0, "原文"), "原翻譯"),),
        "gemini",
        "old-model",
        translated_work_title="原中文作品",
        translated_chapter_title="原中文章節",
    )
    app.result = SimpleNamespace(output_paths=(txt_path, html_path), chapter=translated)

    chapter = app._chapter_for_html((txt_path, html_path))

    assert chapter.translated_work_title == "手動中文作品"
    assert chapter.translated_chapter_title == "手動中文章節"
    assert chapter.source_chapter.title == "手動日文作品"
    assert chapter.source_chapter.chapter_title == "手動日文章節"
    assert chapter.source_chapter.source_url == "https://example.test/manual"
    assert chapter.provider == "其他軟體"
    assert chapter.model == "手動翻譯（手動）"


def test_open_html_can_update_non_complete_chapter_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    app.status = Value("")
    app.chapter_progress = SimpleNamespace(completed_numbers=frozenset())
    refreshed = SimpleNamespace(completed_numbers=frozenset({1}))
    app.chapter_tracker = SimpleNamespace(reconcile=lambda _setup: refreshed)
    app._output_chapter_number = lambda _local: 1
    detail_updates: list[bool] = []
    app._update_work_details = lambda: detail_updates.append(True)
    monkeypatch.setattr("ui.app.messagebox.askyesno", lambda *_args, **_kwargs: True)

    app._offer_chapter_completion_update((tmp_path / "1.txt", tmp_path / "1.html"))

    assert app.chapter_progress is refreshed
    assert app.summary.get() == "第 1 章已更新為已完成。"
    assert app.status.get() == "第 1 章狀態已更新。"
    assert detail_updates == [True]


def test_open_html_does_not_prompt_for_completed_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    app.chapter_progress = SimpleNamespace(completed_numbers=frozenset({1}))
    app._output_chapter_number = lambda _local: 1
    prompts: list[bool] = []

    def record_prompt(*_args: object, **_kwargs: object) -> bool:
        prompts.append(True)
        return True

    monkeypatch.setattr(
        "ui.app.messagebox.askyesno",
        record_prompt,
    )

    app._offer_chapter_completion_update((tmp_path / "1.txt", tmp_path / "1.html"))

    assert prompts == []


def test_prompt_description_follows_selected_tab() -> None:
    app: Any = DesktopApp.__new__(DesktopApp)
    app.prompt_description = Value("")
    app.prompt_tabs = SimpleNamespace(
        select=lambda: "selected-tab",
        index=lambda _tab: 2,
    )

    app._on_prompt_tab_changed()

    assert "{Term_Memory}" in app.prompt_description.get()
    assert "固定 JSON" in app.prompt_description.get()


def test_site_change_switches_output_and_resets_work(tmp_path: Path) -> None:
    app: Any = DesktopApp.__new__(DesktopApp)
    app.site = Value("Kakuyomu")
    app.site_hint = Value("")
    app.site_output_directories = {
        "syosetu": tmp_path / "Syosetu",
        "kakuyomu": tmp_path / "Kakuyomu",
    }
    app.worker = SimpleNamespace(is_running=False)
    calls: list[tuple[str, object]] = []
    app._clear_work_data = lambda *, keep_url: calls.append(("clear", keep_url))
    app._reload_local_work_choices = lambda: calls.append(("reload", None))
    app._apply_state = lambda state, status=None: calls.append((state, status))

    app._on_site_changed()

    assert app.output_directory == tmp_path / "Kakuyomu"
    assert "kakuyomu.jp" in app.site_hint.get()
    assert calls[0] == ("clear", False)
    assert calls[1] == ("reload", None)
    assert calls[2][0] == "idle"


def test_work_extractor_follows_selected_site(tmp_path: Path) -> None:
    from extractors.web_kakuyomu_work import KakuyomuWorkExtractor
    from extractors.web_syosetu_work import SyosetuWorkExtractor

    app = make_app(tmp_path)
    app.output_directory = tmp_path
    app.site = Value("Kakuyomu")
    assert isinstance(app._work_extractor(), KakuyomuWorkExtractor)

    app.site.set("成為小說家吧")
    assert isinstance(app._work_extractor(), SyosetuWorkExtractor)


@pytest.mark.parametrize(
    ("selected", "url", "expected"),
    [
        ("成為小說家吧", "https://kakuyomu.jp/works/123", "Kakuyomu"),
        ("Kakuyomu", "https://ncode.syosetu.com/n1234ab/", "成為小說家吧"),
    ],
)
def test_known_cross_site_url_reports_the_required_button(
    tmp_path: Path, selected: str, url: str, expected: str
) -> None:
    from core.exceptions import UnsupportedUrlError

    app = make_app(tmp_path)
    app.site = Value(selected)

    with pytest.raises(UnsupportedUrlError, match=expected):
        app._validate_selected_site_url(url)
