"""Complete translation prompt composition tests."""

from __future__ import annotations

import pytest

from core.prompt_composer import compose_translation_prompt, validate_translation_template


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


def test_compose_appends_missing_sections_and_omits_empty_memory() -> None:
    result = compose_translation_prompt("規則", "本文", {"王都": "王都"})
    assert result.startswith("規則\n\n本文")
    assert "王都 → 王都" in result
    assert compose_translation_prompt("規則 {Novel_Content}", "本文", {}) == "規則 本文"


@pytest.mark.parametrize(
    "template",
    ["", "{Novel_Content}{Novel_Content}", "{Term_Memory}\n{Term_Memory}"],
)
def test_validate_rejects_blank_or_repeated_markers(template: str) -> None:
    with pytest.raises(ValueError):
        validate_translation_template(template)
