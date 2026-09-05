"""Tests for external settings and secret handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from core.config import (
    AppConfig,
    ConfigurationError,
    MissingApiKeyError,
    ensure_api_key,
    get_api_key,
    load_config,
    save_api_key,
)


def test_load_config_creates_defaults_when_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"

    config = load_config(config_path)

    assert config == AppConfig()
    assert "provider" not in json.loads(config_path.read_text(encoding="utf-8"))


def test_load_config_accepts_valid_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "model": "test-model",
                "output_directory": "results",
                "chunk_size": 2000,
                "retry_attempts": 5,
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.provider == "gemini"
    assert config.chunk_size == 2000


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"provider": "unknown"},
        {"chunk_size": 0},
        {"retry_attempts": -1},
        {"unexpected": True},
    ],
)
def test_load_config_rejects_invalid_values(tmp_path: Path, payload: object) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(config_path)


def test_get_api_key_uses_selected_provider() -> None:
    config = AppConfig()

    assert get_api_key(config, {"GEMINI_API_KEY": "secret"}) == "secret"


def test_ensure_api_key_prompts_and_saves_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = ensure_api_key(
        AppConfig(),
        env_path=env_path,
        secret_reader=lambda _prompt: "new-secret",
    )

    assert result == "new-secret"
    assert dotenv_values(env_path)["GEMINI_API_KEY"] == "new-secret"


def test_ensure_api_key_rejects_blank_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(MissingApiKeyError):
        ensure_api_key(
            AppConfig(),
            env_path=tmp_path / ".env",
            secret_reader=lambda _prompt: "",
        )


def test_save_api_key_persists_selected_provider_and_rejects_blank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = AppConfig()

    assert save_api_key(config, "  local-secret  ", env_path=env_path) == "local-secret"
    assert dotenv_values(env_path)["GEMINI_API_KEY"] == "local-secret"
    with pytest.raises(MissingApiKeyError, match="不可為空白"):
        save_api_key(config, " ", env_path=env_path)
