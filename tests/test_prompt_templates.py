from pathlib import Path

import pytest

from core.prompt_templates import PROMPT_TEMPLATES, PromptTemplateStore


def test_ensure_creates_both_editable_templates(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)

    store.ensure()

    assert {path.name for path in store.directory.iterdir()} == {
        template.filename for template in PROMPT_TEMPLATES
    }
    assert all(store.read(template.key).strip() for template in PROMPT_TEMPLATES)


def test_ensure_does_not_overwrite_user_content(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    store.save("chapter", "我的規則")

    store.ensure()

    assert store.read("chapter") == "我的規則\n"


def test_save_rejects_blank_prompt(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)
    store.ensure()

    with pytest.raises(ValueError, match="不可為空白"):
        store.save("chapter", " \n")


def test_default_text_remains_available_after_customization(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    original = store.default_text("work_metadata")
    store.save("work_metadata", "自訂")

    assert store.read("work_metadata") == "自訂\n"
    assert store.default_text("work_metadata") == original
