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
    app.model_entry = SimpleNamespace(configure=lambda **_kwargs: None)
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
