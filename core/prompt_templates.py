"""Manage user-editable prompt templates outside the bundled resources."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.paths import get_resource_path
from core.prompt_composer import validate_translation_template


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    key: str
    label: str
    filename: str
    bundled_resource: str


PROMPT_TEMPLATES = (
    PromptTemplate(
        key="chapter",
        label="章節翻譯",
        filename="translation.txt",
        bundled_resource="resources/prompts/translation.txt",
    ),
    PromptTemplate(
        key="work_metadata",
        label="作品名稱與摘要",
        filename="work_metadata_translation.txt",
        bundled_resource="resources/prompts/work_metadata_translation.txt",
    ),
)


class PromptTemplateStore:
    """Seed, read, and atomically save writable prompt template copies."""

    def __init__(self, app_directory: Path) -> None:
        self.directory = app_directory / "prompts"

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for template in PROMPT_TEMPLATES:
            path = self.path_for(template.key)
            if not path.exists():
                self._atomic_write(path, self.default_text(template.key))

    def path_for(self, key: str) -> Path:
        template = self._template(key)
        return self.directory / template.filename

    def read(self, key: str) -> str:
        return self.path_for(key).read_text(encoding="utf-8-sig")

    def save(self, key: str, text: str) -> None:
        if not text.strip():
            raise ValueError("Prompt 模板不可為空白。")
        if key == "chapter":
            validate_translation_template(text)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.path_for(key), text.rstrip() + "\n")

    def default_text(self, key: str) -> str:
        template = self._template(key)
        return get_resource_path(template.bundled_resource).read_text(encoding="utf-8-sig")

    @staticmethod
    def _template(key: str) -> PromptTemplate:
        for template in PROMPT_TEMPLATES:
            if template.key == key:
                return template
        raise KeyError(f"Unknown prompt template: {key}")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


__all__ = ["PROMPT_TEMPLATES", "PromptTemplate", "PromptTemplateStore"]
