"""Compose complete per-chunk prompts from user-editable templates."""

from __future__ import annotations

import json
from collections.abc import Mapping

from core.prompt_contracts import (
    CHAPTER_OUTPUT_CONTRACT,
    TERM_ORGANIZATION_OUTPUT_CONTRACT,
    WORK_METADATA_OUTPUT_CONTRACT,
    append_prompt_contract,
)

NOVEL_CONTENT_MARKER = "{Novel_Content}"
TERM_MEMORY_MARKER = "{Term_Memory}"
CHAPTER_TITLE_MARKER = "{Chapter_Title}"
WORK_TITLE_MARKER = "{Work_Title}"
WORK_SYNOPSIS_MARKER = "{Work_Synopsis}"


def validate_translation_template(template: str) -> None:
    """Reject blank templates and ambiguous repeated dynamic markers."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Prompt 模板不可為空白。")
    for marker in (NOVEL_CONTENT_MARKER, TERM_MEMORY_MARKER, CHAPTER_TITLE_MARKER):
        if template.count(marker) > 1:
            raise ValueError(f"Prompt 標記 {marker} 最多只能出現一次。")


def validate_work_metadata_template(template: str) -> None:
    """Validate the editable work-title and synopsis template."""
    _validate_markers(template, (WORK_TITLE_MARKER, WORK_SYNOPSIS_MARKER))


def validate_term_organization_template(template: str) -> None:
    """Validate the editable term-memory organization template."""
    _validate_markers(template, (TERM_MEMORY_MARKER,))


def _validate_markers(template: str, markers: tuple[str, ...]) -> None:
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Prompt 模板不可為空白。")
    for marker in markers:
        if template.count(marker) > 1:
            raise ValueError(f"Prompt 標記 {marker} 最多只能出現一次。")


def format_term_pairs(terms: Mapping[str, str]) -> str:
    """Render every matched pair, preferring longer Japanese terms first."""
    return "\n".join(
        f"{source} → {translation}"
        for source, translation in sorted(terms.items(), key=lambda item: (-len(item[0]), item[0]))
    )


def compose_translation_prompt(
    template: str,
    source_text: str,
    terms: Mapping[str, str],
) -> str:
    """Replace template markers, appending missing dynamic sections safely."""
    validate_translation_template(template)
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("翻譯原文不可為空白。")

    prompt = template
    term_text = format_term_pairs(terms)
    # Compatibility for prompt files created while title and正文 shared one request.
    prompt = prompt.replace(CHAPTER_TITLE_MARKER, "")
    if NOVEL_CONTENT_MARKER in prompt:
        prompt = prompt.replace(NOVEL_CONTENT_MARKER, source_text)
    else:
        prompt = f"{prompt.rstrip()}\n\n{source_text}"

    if TERM_MEMORY_MARKER in prompt:
        prompt = prompt.replace(TERM_MEMORY_MARKER, term_text)
    elif term_text:
        prompt = (
            f"{prompt.rstrip()}\n\n"
            "以下是本章使用的固定專有名詞譯名。\n"
            "原詞出現時必須使用指定譯名：\n\n"
            f"{term_text}"
        )
    return append_prompt_contract(prompt, CHAPTER_OUTPUT_CONTRACT)


def compose_work_metadata_prompt(template: str, title: str, synopsis: str) -> str:
    """Insert one work's source metadata and append its protected output contract."""
    validate_work_metadata_template(template)
    if not isinstance(title, str) or not title.strip():
        raise ValueError("作品名稱不可為空白。")
    if not isinstance(synopsis, str) or not synopsis.strip():
        raise ValueError("作品摘要不可為空白。")
    prompt = template
    if WORK_TITLE_MARKER in prompt:
        prompt = prompt.replace(WORK_TITLE_MARKER, title.strip())
    else:
        prompt = f"{prompt.rstrip()}\n\n作品名稱：\n{title.strip()}"
    if WORK_SYNOPSIS_MARKER in prompt:
        prompt = prompt.replace(WORK_SYNOPSIS_MARKER, synopsis.strip())
    else:
        prompt = f"{prompt.rstrip()}\n\n作品摘要：\n{synopsis.strip()}"
    return append_prompt_contract(prompt, WORK_METADATA_OUTPUT_CONTRACT)


def compose_term_organization_prompt(template: str, terms: Mapping[str, str]) -> str:
    """Insert one raw memory batch and append its protected output contract."""
    validate_term_organization_template(template)
    term_payload = json.dumps(
        {"terms": dict(terms)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    prompt = template
    if TERM_MEMORY_MARKER in prompt:
        prompt = prompt.replace(TERM_MEMORY_MARKER, term_payload)
    else:
        prompt = f"{prompt.rstrip()}\n\n本批需要整理的專有名詞：\n{term_payload}"
    return append_prompt_contract(prompt, TERM_ORGANIZATION_OUTPUT_CONTRACT)


__all__ = [
    "CHAPTER_TITLE_MARKER",
    "NOVEL_CONTENT_MARKER",
    "TERM_MEMORY_MARKER",
    "WORK_SYNOPSIS_MARKER",
    "WORK_TITLE_MARKER",
    "compose_term_organization_prompt",
    "compose_translation_prompt",
    "compose_work_metadata_prompt",
    "format_term_pairs",
    "validate_term_organization_template",
    "validate_translation_template",
    "validate_work_metadata_template",
]
