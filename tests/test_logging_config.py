"""Tests for safe application logging."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.logging_config import setup_logging


def test_logging_redacts_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "do-not-log-this-value"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    logger = setup_logging(tmp_path)

    logger.info("Credential value: %s", secret)
    for handler in logger.handlers:
        handler.flush()

    log_text = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert secret not in log_text
    assert "[REDACTED]" in log_text


def test_logging_redacts_key_assignment(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)

    logger.warning("GEMINI_API_KEY=plain-text-secret")
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()

    log_text = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "plain-text-secret" not in log_text
    assert "GEMINI_API_KEY=[REDACTED]" in log_text
