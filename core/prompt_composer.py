"""Compose complete per-chunk prompts from user-editable templates."""

from __future__ import annotations

from collections.abc import Mapping

NOVEL_CONTENT_MARKER = "{Novel_Content}"
TERM_MEMORY_MARKER = "{Term_Memory}"


def validate_translation_template(template: str) -> None:
    """Reject blank templates and ambiguous repeated dynamic markers."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("Prompt 模板不可為空白。")
    for marker in (NOVEL_CONTENT_MARKER, TERM_MEMORY_MARKER):
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
    return prompt


__all__ = [
    "NOVEL_CONTENT_MARKER",
    "TERM_MEMORY_MARKER",
    "compose_translation_prompt",
    "format_term_pairs",
    "validate_translation_template",
]
