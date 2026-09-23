"""Shared construction of the production translation strategy graph."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from core.api_usage import DailyApiUsage
from core.checkpoint import CheckpointStore
from core.config import AppConfig
from core.controller import TranslationController
from core.term_memory import TermMemoryStore
from core.term_organizer import TermOrganizationService
from core.text_chunker import TextChunker
from core.work_setup import WorkSetupService
from extractors.base import BaseExtractor
from extractors.web_syosetu import SyosetuExtractor
from formatters import HtmlFormatter, TxtFormatter
from translators import (
    ApiLlmTranslator,
    ApiTermAnalyzer,
    ApiTermOrganizer,
    ApiWorkTranslator,
    BaseTranslator,
)
from translators.codex_llm import CodexTranslator

RunMode = Literal["analysis", "gemini", "codex", "codex_analysis"]


def build_translation_controller(
    *,
    config: AppConfig,
    output_directory: Path,
    checkpoint_directory: Path,
    mode: RunMode,
    auto_open: bool,
    overwrite_outputs: bool = False,
    api_key: str | None = None,
    prompt_path: Path | None = None,
    translated_work_title: str | None = None,
    chapter_extractor: BaseExtractor | None = None,
    usage: DailyApiUsage | None = None,
    work_directory: Path | None = None,
) -> TranslationController:
    """Build GUI strategies while keeping chapter analysis API-free."""
    translator: BaseTranslator
    term_analyzer = None
    if mode in {"codex", "codex_analysis"}:
        if work_directory is None:
            raise ValueError("Codex requires a selected work directory")
        translator = CodexTranslator(
            work_directory, model=config.codex_model, prompt_path=prompt_path,
            reasoning_effort=config.codex_reasoning_effort,
            usage=usage if mode == "codex" else None, force=overwrite_outputs,
        )
    elif mode == "analysis":
        translator = ApiLlmTranslator.from_config(
            config,
            "analysis-placeholder",
            client=object(),
            prompt_path=prompt_path,
        )
        term_analyzer = ApiTermAnalyzer.from_config(
            config,
            "analysis-placeholder",
            client=object(),
        )
    elif mode == "gemini":
        if not api_key:
            raise ValueError("api_key is required for Gemini mode")
        translator = ApiLlmTranslator.from_config(
            config, api_key, prompt_path=prompt_path, usage=usage
        )
        term_analyzer = ApiTermAnalyzer.from_config(config, api_key, usage=usage)
    else:
        raise ValueError(f"Unsupported run mode: {mode}")

    return TranslationController(
        extractor=chapter_extractor or SyosetuExtractor(),
        chunker=TextChunker(config.chunk_size),
        translator=translator,
        checkpoint_store=CheckpointStore(checkpoint_directory),
        formatters=(
            TxtFormatter(overwrite=overwrite_outputs),
            HtmlFormatter(auto_open=auto_open, overwrite=overwrite_outputs),
        ),
        output_directory=output_directory,
        term_memory_store=TermMemoryStore(),
        term_analyzer=term_analyzer,
        translated_work_title=translated_work_title,
    )


def build_work_setup_service(
    *,
    config: AppConfig,
    output_directory: Path,
    api_key: str,
    prompt_path: Path | None = None,
    usage: DailyApiUsage | None = None,
) -> WorkSetupService:
    """Build the Gemini work metadata service without coupling it to the GUI."""
    if not api_key:
        raise ValueError("api_key is required for Gemini work setup")
    translator = ApiWorkTranslator.from_config(
        config, api_key, prompt_path=prompt_path, usage=usage
    )
    return WorkSetupService(output_directory, translator)


def build_term_organization_service(
    *,
    config: AppConfig,
    api_key: str,
    prompt_path: Path | None = None,
    usage: DailyApiUsage | None = None,
) -> TermOrganizationService:
    """Build the manually triggered Gemini term-memory organizer."""
    if not api_key:
        raise ValueError("api_key is required for term organization")
    return TermOrganizationService(
        ApiTermOrganizer.from_config(config, api_key, prompt_path=prompt_path, usage=usage)
    )


__all__ = [
    "RunMode",
    "build_term_organization_service",
    "build_translation_controller",
    "build_work_setup_service",
]
