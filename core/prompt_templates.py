"""Manage user-editable prompt templates outside the bundled resources."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.paths import get_resource_path
from core.prompt_composer import (
    validate_term_organization_template,
    validate_translation_template,
    validate_work_metadata_template,
)
from core.prompt_contracts import (
    CHAPTER_OUTPUT_CONTRACT,
    TERM_ORGANIZATION_OUTPUT_CONTRACT,
    WORK_METADATA_OUTPUT_CONTRACT,
)


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    key: str
    label: str
    description: str
    required_text: str
    filename: str
    bundled_resource: str


PROMPT_TEMPLATES = (
    PromptTemplate(
        key="chapter",
        label="章節翻譯",
        description=(
            "可使用 {Novel_Content} 與 {Term_Memory}。章節名稱由程式另外翻譯；"
            "動態標記缺少時會自動附加，重複時無法儲存。"
        ),
        required_text=CHAPTER_OUTPUT_CONTRACT,
        filename="translation.txt",
        bundled_resource="resources/prompts/translation.txt",
    ),
    PromptTemplate(
        key="work_metadata",
        label="作品名稱與摘要",
        description=(
            "可使用 {Work_Title} 與 {Work_Synopsis}；缺少時會自動附加，"
            "重複時無法儲存。固定 JSON 輸出格式顯示於下方。"
        ),
        required_text=WORK_METADATA_OUTPUT_CONTRACT,
        filename="work_metadata_translation.txt",
        bundled_resource="resources/prompts/work_metadata_translation.txt",
    ),
    PromptTemplate(
        key="term_organization",
        label="重新整理記憶",
        description=(
            "可使用 {Term_Memory} 表示本次整理批次；缺少時會自動附加，"
            "重複時無法儲存。固定 JSON 輸出格式顯示於下方。"
        ),
        required_text=TERM_ORGANIZATION_OUTPUT_CONTRACT,
        filename="term_organization.txt",
        bundled_resource="resources/prompts/term_organization.txt",
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
            else:
                current = path.read_text(encoding="utf-8-sig")
                migrated = self._strip_legacy_contract(current, template)
                if migrated != current:
                    self._atomic_write(path, migrated)

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
        elif key == "work_metadata":
            validate_work_metadata_template(text)
        elif key == "term_organization":
            validate_term_organization_template(text)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.path_for(key), text.rstrip() + "\n")

    def default_text(self, key: str) -> str:
        template = self._template(key)
        return get_resource_path(template.bundled_resource).read_text(encoding="utf-8-sig")

    @staticmethod
    def _strip_legacy_contract(text: str, template: PromptTemplate) -> str:
        """Remove the previously editable copy of an exact built-in contract."""
        legacy_lines = {
            "chapter": (
                (
                    "7. 只輸出翻譯結果，不得加入前言、摘要、註解、翻譯說明、道歉、"
                    "引號包覆或 Markdown 程式碼區塊。"
                ),
                (
                    "7. 除了下方固定格式要求外，不得加入前言、摘要、註解、翻譯說明、"
                    "道歉、引號包覆或 Markdown 程式碼區塊。"
                ),
            ),
            "work_metadata": (
                "4. 只輸出指定 JSON，不加入說明或 Markdown。",
                "4. 翻譯結果保持簡潔，不加入原文沒有的說明。",
            ),
        }
        replacement = legacy_lines.get(template.key)
        if replacement is not None:
            text = text.replace(*replacement)
        legacy = template.required_text.replace("（程式固定要求）", "")
        for candidate in (template.required_text, legacy):
            suffix = f"\n\n{candidate.strip()}"
            if text.rstrip().endswith(suffix):
                return text.rstrip()[: -len(suffix)].rstrip() + "\n"
        legacy_markers = {
            "chapter": "\n輸出格式：",
            "work_metadata": "\n輸出格式：",
            "term_organization": "\n只輸出 JSON object，格式必須是：",
        }
        marker = legacy_markers[template.key]
        position = text.rfind(marker)
        if position >= 0:
            return text[:position].rstrip() + "\n"
        return text

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
