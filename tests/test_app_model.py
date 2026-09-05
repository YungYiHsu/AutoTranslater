from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.config import AppConfig
from ui.app import DesktopApp


class Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


def make_app(tmp_path: Path, model: str = "gemini-3.5-flash") -> Any:
    app: Any = DesktopApp.__new__(DesktopApp)
    app.root = SimpleNamespace()
    app.app_directory = tmp_path
    app.config = AppConfig(model=model)
    app.model_name = Value(model)
    app.mode = Value("gemini")
    app.model_entry = SimpleNamespace(configure=lambda **_kwargs: None)
    app.model_frame = SimpleNamespace(configure=lambda **_kwargs: None)
    app.state = "work_ready"
    app.plan = None
    app.controller = None
    app.result = None
    app.work_result = SimpleNamespace()
    app.summary = Value("")
    app._analyzed_mode = None
    app._term_organization_service = SimpleNamespace()
    app._apply_state = lambda state, status=None: setattr(app, "state", state)
    return app


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
    app.model_frame = SimpleNamespace(configure=lambda **kwargs: frame_updates.append(kwargs))

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
    assert frame_updates[-1]["text"] == "API 模型"


def test_output_chapter_number_has_no_leading_zeroes(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    txt_path = tmp_path / "0012 - 測試.txt"
    html_path = tmp_path / "0012 - 測試.html"
    source_chapter = SimpleNamespace(source_url="https://example.test/work/12/")
    app.result = SimpleNamespace(
        output_paths=(txt_path, html_path),
        chapter=SimpleNamespace(source_chapter=source_chapter),
    )

    assert app._output_chapter_number((txt_path, html_path)) == 12


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
