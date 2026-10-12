"""Non-editable output contracts required by application response parsers."""

from __future__ import annotations

CHAPTER_OUTPUT_CONTRACT = (
    "只輸出翻譯後的小說正文，不得加入章節名稱、標記、說明或 Markdown。"
)

WORK_METADATA_OUTPUT_CONTRACT = """只輸出下列 JSON，不得加入說明、Markdown 或其他欄位：
{"traditional_chinese_title":"作品名稱","traditional_chinese_synopsis":"作品摘要"}"""

TERM_ORGANIZATION_OUTPUT_CONTRACT = """只輸出 JSON object，不得加入說明、Markdown 或其他欄位：
{"add":[{"source":"核心日文","translation":"核心繁體中文"}],"remove":["要移除的原詞"]}

沒有建議時輸出：
{"add":[],"remove":[]}"""


def append_prompt_contract(prompt: str, contract: str) -> str:
    """Append one trusted contract after the user-editable instructions."""
    return f"{prompt.rstrip()}\n\n{contract}"


__all__ = [
    "CHAPTER_OUTPUT_CONTRACT",
    "TERM_ORGANIZATION_OUTPUT_CONTRACT",
    "WORK_METADATA_OUTPUT_CONTRACT",
    "append_prompt_contract",
]
