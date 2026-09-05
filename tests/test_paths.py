"""Tests for source and frozen application path handling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.paths import ensure_runtime_directories, get_app_dir, get_resource_path


def test_get_app_dir_in_source_mode() -> None:
    assert get_app_dir() == Path(__file__).resolve().parents[1]


def test_get_app_dir_in_frozen_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "AutoTranslater.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert get_app_dir() == tmp_path


def test_get_resource_path_uses_meipass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert (
        get_resource_path("resources/prompts/translation.txt")
        == (tmp_path / "resources/prompts/translation.txt").resolve()
    )


@pytest.mark.parametrize("invalid_path", ["../secret.txt", Path("C:/secret.txt")])
def test_get_resource_path_rejects_unsafe_paths(invalid_path: str | Path) -> None:
    with pytest.raises(ValueError):
        get_resource_path(invalid_path)


def test_ensure_runtime_directories(tmp_path: Path) -> None:
    output_dir, checkpoint_dir, log_dir = ensure_runtime_directories(
        "translated",
        app_dir=tmp_path,
    )

    assert output_dir == tmp_path / "translated"
    assert checkpoint_dir == tmp_path / "checkpoints"
    assert log_dir == tmp_path / "logs"
    assert all(path.is_dir() for path in (output_dir, checkpoint_dir, log_dir))
