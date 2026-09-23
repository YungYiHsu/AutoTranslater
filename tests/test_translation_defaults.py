"""Persist effective selections only at the first provider request."""

from pathlib import Path

import pytest

from core.api_usage import DailyApiUsage, ObservedApiUsage
from core.config import AppConfig, ConfigurationError, load_config
from core.controller import ChapterExecutionOptions
from tests.test_app_model import make_app
from ui.messages import WorkerMessage


def test_defaults_round_trip(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.translate_title.set(False)
    assert not (tmp_path / "config.json").exists()
    selected = ChapterExecutionOptions(False, True, False)
    app._handle_message(WorkerMessage("translation_api_started", selected))
    saved = load_config(tmp_path / "config.json")
    assert (saved.translate_title, saved.translate_body, saved.update_terms) == (False, True, False)
    app.config = saved
    app.translate_title.set(True)
    app._reset_execution_options()
    assert app._execution_options() == selected


def test_observer_notifies_once_per_run(tmp_path: Path) -> None:
    usage = DailyApiUsage(tmp_path / "usage.json")
    events: list[str] = []
    observed = ObservedApiUsage(usage, lambda: events.append("started"))
    assert events == []  # No request, including checkpoint-only runs.
    observed.record("model")
    observed.record("model")  # Retry or another component.
    assert events == ["started"]
    assert usage.count("model") == observed.count("model") == 2


def test_old_config_defaults_and_disabled_terms() -> None:
    assert AppConfig.from_mapping({}) == AppConfig()
    assert not AppConfig.from_mapping({"translate_body": False}).update_terms
    with pytest.raises(ConfigurationError):
        AppConfig.from_mapping({"translate_title": "false"})
