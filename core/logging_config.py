"""Application logging with credential redaction."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import API_KEY_NAME

_KEY_ASSIGNMENT = re.compile(r"(?i)(GEMINI_API_KEY)\s*[=:]\s*\S+")


class SecretRedactionFilter(logging.Filter):
    """Remove known credentials and key assignments from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _KEY_ASSIGNMENT.sub(r"\1=[REDACTED]", message)
        secret = os.environ.get(API_KEY_NAME, "")
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
        record.msg = redacted
        record.args = ()
        return True


def setup_logging(log_dir: Path, *, verbose: bool = False) -> logging.Logger:
    """Configure console and rotating UTF-8 file logging once."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("novel_translator")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redaction_filter = SecretRedactionFilter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction_filter)

    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger
