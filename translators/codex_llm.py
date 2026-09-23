"""Codex translation strategies sharing a persistent conversation per work."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from core.api_usage import DailyApiUsage
from core.models import NovelWork, TextChunk, TranslatedChunk, TranslatedNovelWork
from core.paths import get_resource_path
from core.prompt_composer import compose_translation_prompt, compose_work_metadata_prompt
from core.prompt_contracts import CHAPTER_OUTPUT_CONTRACT
from core.work_directories import resolve_work_directory
from translators.api_llm import _TITLE_INSTRUCTIONS, _normalize_response
from translators.api_work import ApiWorkTranslator
from translators.base import BaseTranslator
from translators.codex_session import TRANSLATION_INSTRUCTIONS, CodexSession
from translators.work_base import BaseWorkTranslator


class CodexTranslator(BaseTranslator):
    def __init__(self, directory: Path, *, model: str = "default",
                 reasoning_effort: str = "default",
                 prompt_path: Path | None = None, usage: DailyApiUsage | None = None,
                 force: bool = False) -> None:
        self._model = model
        self.reasoning_effort = reasoning_effort
        path = prompt_path or get_resource_path("resources/prompts/translation.txt")
        self._prompt = path.read_text(encoding="utf-8-sig").strip()
        self.session = CodexSession(directory, model, reasoning_effort=reasoning_effort,
                                    validate_response=lambda text: _normalize_response(text, "codex"))
        self.usage = usage
        self.force = force
        self.context = ""
        self.chapter_number = None

    @property
    def provider(self) -> str:
        return "codex"

    @property
    def model(self) -> str:
        return self._model

    @property
    def checkpoint_identity(self) -> str:
        effort_identity = "" if self.reasoning_effort == "default" else self.reasoning_effort
        return hashlib.sha256(("codex-v1" + self._prompt + CHAPTER_OUTPUT_CONTRACT
                               + _TITLE_INSTRUCTIONS + TRANSLATION_INSTRUCTIONS + effort_identity).encode()).hexdigest()

    def _ask(self, prompt: str, component: str) -> str:
        usage = self.usage
        text = self.session.ask(
            f"目前章節：{self.context}\n{prompt}", key=self.context + component,
            force=self.force,
            component=component,
            label=f"第 {self.chapter_number} 章" + ("標題" if component == "title" else "內文"),
            on_request=(lambda: usage.record(self.model)) if usage else None,
        )
        return _normalize_response(text, "codex")

    def translate(self, chunk: TextChunk, terms: Mapping[str, str] | None = None) -> TranslatedChunk:
        prompt = compose_translation_prompt(self._prompt, chunk.text, terms or {})
        return TranslatedChunk(chunk, self._ask(prompt, f"chunk:{chunk.index}"))

    def translate_title(self, title: str, terms: Mapping[str, str] | None = None) -> str:
        prompt = f"{_TITLE_INSTRUCTIONS}\n{title}\n固定譯名：{dict(terms or {})}"
        return self._ask(prompt, "title")


class CodexWorkTranslator(BaseWorkTranslator):
    def __init__(self, output_directory: Path, *, model: str = "default",
                 reasoning_effort: str = "default",
                 prompt_path: Path | None = None) -> None:
        self.output_directory = output_directory
        self._model = model
        self.reasoning_effort = reasoning_effort
        path = prompt_path or get_resource_path("resources/prompts/work_metadata_translation.txt")
        self.prompt = path.read_text(encoding="utf-8-sig").strip()

    @property
    def provider(self) -> str:
        return "codex"

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_identity(self) -> str:
        effort_identity = "" if self.reasoning_effort == "default" else self.reasoning_effort
        return hashlib.sha256(("codex-work-v1" + self.prompt + TRANSLATION_INSTRUCTIONS + effort_identity).encode()).hexdigest()

    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        directory = resolve_work_directory(self.output_directory, work_id=work.work_id,
                                           source_url=work.source_url, title=work.title)
        prompt = compose_work_metadata_prompt(self.prompt, work.title, work.synopsis)
        response = CodexSession(
            directory, self.model, validate_response=ApiWorkTranslator._parse_response,
            reasoning_effort=self.reasoning_effort,
        ).ask(prompt, key="work-metadata")
        title, synopsis = ApiWorkTranslator._parse_response(response)
        return TranslatedNovelWork(work, title, synopsis, self.provider, self.model, self.prompt_identity)
