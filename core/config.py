"""External configuration and API credential loading."""

from __future__ import annotations

import getpass
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key

from core.exceptions import NovelTranslatorError
from core.paths import get_app_dir

API_KEY_NAME = "GEMINI_API_KEY"


class ConfigurationError(NovelTranslatorError, ValueError):
    """Raised when an external configuration file is invalid."""


class MissingApiKeyError(ConfigurationError):
    """Raised when the selected provider has no configured API key."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated non-secret application settings."""

    model: str = ""
    saved_models: tuple[str, ...] = ()
    output_directory: str = "outputs"
    chunk_size: int = 4000
    retry_attempts: int = 3

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> AppConfig:
        """Create settings from JSON-compatible values and validate them."""
        defaults = cls()
        known_fields = {
            "model",
            "saved_models",
            "output_directory",
            "chunk_size",
            "retry_attempts",
        }
        unknown = set(values) - known_fields
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ConfigurationError(f"Unknown config field(s): {names}")

        model = values.get("model", defaults.model)
        saved_models = values.get("saved_models", defaults.saved_models)
        output_directory = values.get("output_directory", defaults.output_directory)
        chunk_size = values.get("chunk_size", defaults.chunk_size)
        retry_attempts = values.get("retry_attempts", defaults.retry_attempts)

        if not isinstance(model, str):
            raise ConfigurationError("model must be a string")
        if not isinstance(saved_models, (list, tuple)) or isinstance(saved_models, str):
            raise ConfigurationError("saved_models must be a list of non-empty strings")
        normalized_saved_models: list[str] = []
        for saved_model in saved_models:
            if not isinstance(saved_model, str) or not saved_model.strip():
                raise ConfigurationError("saved_models must contain non-empty strings")
            normalized = saved_model.strip()
            if normalized not in normalized_saved_models:
                normalized_saved_models.append(normalized)
        if not isinstance(output_directory, str) or not output_directory.strip():
            raise ConfigurationError("output_directory must be a non-empty string")
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
            raise ConfigurationError("chunk_size must be a positive integer")
        if (
            not isinstance(retry_attempts, int)
            or isinstance(retry_attempts, bool)
            or retry_attempts < 0
        ):
            raise ConfigurationError("retry_attempts must be a non-negative integer")

        return cls(
            model=model,
            saved_models=tuple(normalized_saved_models),
            output_directory=output_directory,
            chunk_size=chunk_size,
            retry_attempts=retry_attempts,
        )

    @property
    def provider(self) -> str:
        """Return the only supported cloud provider."""
        return "gemini"


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load config JSON, creating a safe default file when it is absent."""
    path = config_path or get_app_dir() / "config.json"
    if not path.exists():
        config = AppConfig()
        write_config(config, path)
        return config

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Unable to read valid JSON from {path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path.name} must contain a JSON object")
    return AppConfig.from_mapping(raw)


def write_config(config: AppConfig, config_path: Path | None = None) -> None:
    """Write non-secret settings as formatted UTF-8 JSON."""
    path = config_path or get_app_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_environment(env_path: Path | None = None) -> Path:
    """Load local API credentials without overriding process environment values."""
    path = env_path or get_app_dir() / ".env"
    load_dotenv(dotenv_path=path, override=False)
    return path


def get_api_key(config: AppConfig, environment: Mapping[str, str] | None = None) -> str | None:
    """Return the selected provider's API key without logging it."""
    source = environment if environment is not None else os.environ
    value = source.get(API_KEY_NAME, "").strip()
    return value or None


def ensure_api_key(
    config: AppConfig,
    *,
    env_path: Path | None = None,
    secret_reader: Callable[[str], str] = getpass.getpass,
) -> str:
    """Return an API key, securely prompting and persisting it when absent."""
    path = load_environment(env_path)
    existing = get_api_key(config)
    if existing:
        return existing

    entered = secret_reader(f"Enter {API_KEY_NAME} (input hidden): ").strip()
    if not entered:
        raise MissingApiKeyError(
            f"{API_KEY_NAME} is required. Add it to {path.name} and run the program again."
        )

    return save_api_key(config, entered, env_path=path)


def save_api_key(
    config: AppConfig,
    api_key: str,
    *,
    env_path: Path | None = None,
) -> str:
    """Persist one validated provider key without exposing it in config JSON."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise MissingApiKeyError("API Key 不可為空白。")
    path = env_path or get_app_dir() / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    value = api_key.strip()
    set_key(str(path), API_KEY_NAME, value, quote_mode="always")
    os.environ[API_KEY_NAME] = value
    return value
