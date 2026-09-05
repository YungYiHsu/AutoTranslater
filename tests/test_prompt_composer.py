"""Complete translation prompt composition tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from core.prompt_composer import (
    compose_term_organization_prompt,
    compose_translation_prompt,
    compose_work_metadata_prompt,
    validate_term_organization_template,
    validate_translation_template,
    validate_work_metadata_template,
)
from core.prompt_contracts import CHAPTER_OUTPUT_CONTRACT


def test_compose_replaces_content_and_term_markers() -> None:
    template = "開始\n{Novel_Content}\n規則\n{Term_Memory}\n結束"
    result = compose_translation_prompt(
        template,
        "アリス出發。",
        {"アリス": "愛麗絲", "シンフォニア王国": "辛弗尼亞王國"},
    )
    assert "{Novel_Content}" not in result
    assert "{Term_Memory}" not in result
    assert "アリス出發。" in result
    assert result.index("シンフォニア王国 → 辛弗尼亞王國") < result.index(
        "アリス → 愛麗絲"
    )


def test_compose_replaces_optional_chapter_title_marker() -> None:
    template = (
        "章節：{Chapter_Title}\n正文：{Novel_Content}\n"
        "<CHAPTER_TITLE>名稱</CHAPTER_TITLE><NOVEL_CONTENT>正文</NOVEL_CONTENT>"
    )
    result = compose_translation_prompt(template, "本文", {})
    assert result.startswith("章節：\n正文：本文")
    assert "第一話" not in result


def test_compose_appends_body_only_contract_for_older_custom_template() -> None:
    result = compose_translation_prompt("正文：{Novel_Content}", "本文", {})
    assert "章節名稱、標記" in result
    assert "<CHAPTER_TITLE>" not in result
    assert "<NOVEL_CONTENT>" not in result


def test_compose_work_metadata_replaces_markers_and_appends_missing_ones() -> None:
    replaced = compose_work_metadata_prompt(
        "名稱：{Work_Title}\n摘要：{Work_Synopsis}",
        "作品名",
        "作品摘要。",
    )
    appended = compose_work_metadata_prompt("規則", "作品名", "作品摘要。")
    assert "名稱：作品名" in replaced
    assert "摘要：作品摘要。" in replaced
    assert "作品名稱：\n作品名" in appended
    assert "作品摘要：\n作品摘要。" in appended


def test_compose_term_organization_replaces_marker_and_appends_when_missing() -> None:
    terms = {"原詞": "譯名"}
    replaced = compose_term_organization_prompt("記憶：{Term_Memory}", terms)
    appended = compose_term_organization_prompt("規則", terms)
    assert '記憶：{"terms":{"原詞":"譯名"}}' in replaced
    assert '本批需要整理的專有名詞：\n{"terms":{"原詞":"譯名"}}' in appended


@pytest.mark.parametrize(
    ("validator", "template"),
    [
        (validate_work_metadata_template, "{Work_Title}{Work_Title}"),
        (validate_work_metadata_template, "{Work_Synopsis}{Work_Synopsis}"),
        (validate_term_organization_template, "{Term_Memory}{Term_Memory}"),
    ],
)
def test_other_prompt_validators_reject_repeated_markers(
    validator: Callable[[str], None],
    template: str,
) -> None:
    with pytest.raises(ValueError):
        validator(template)


def test_compose_appends_missing_sections_and_omits_empty_memory() -> None:
    result = compose_translation_prompt("規則", "本文", {"王都": "王都"})
    assert result.startswith("規則\n\n本文")
    assert "王都 → 王都" in result
    composed = compose_translation_prompt("規則 {Novel_Content}", "本文", {})
    assert composed.startswith("規則 本文")
    assert composed.endswith(CHAPTER_OUTPUT_CONTRACT)


@pytest.mark.parametrize(
    "template",
    [
        "",
        "{Novel_Content}{Novel_Content}",
        "{Term_Memory}\n{Term_Memory}",
        "{Chapter_Title}{Chapter_Title}",
    ],
)
def test_validate_rejects_blank_or_repeated_markers(template: str) -> None:
    with pytest.raises(ValueError):
        validate_translation_template(template)
