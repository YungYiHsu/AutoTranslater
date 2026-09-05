from pathlib import Path

import pytest

from core.prompt_templates import PROMPT_TEMPLATES, PromptTemplateStore


def test_ensure_creates_all_editable_templates(tmp_path: Path) -> None:
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


def test_term_organization_template_uses_bundled_default(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)

    store.ensure()

    assert store.read("term_organization") == store.default_text("term_organization")
    template = next(item for item in PROMPT_TEMPLATES if item.key == "term_organization")
    assert store.read("term_organization").startswith("任務目標：\n\n精簡專有名詞記憶。")
    assert "將有重複前綴的詞語合併" in store.read("term_organization")
    assert "在範例裡，都是將アルベルト翻譯成阿爾貝特" in store.read(
        "term_organization"
    )
    assert store.read("term_organization").count("{Term_Memory}") == 1
    assert '"add"' in store.read("term_organization")
    assert '"remove"' in store.read("term_organization")
    assert '"add"' in template.required_text
    assert '"remove"' in template.required_text
    assert '"groups"' not in template.required_text


def test_ensure_migrates_legacy_editable_output_contract(tmp_path: Path) -> None:
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    path = store.path_for("work_metadata")
    path.write_text(
        "我的翻譯規則\n\n輸出格式：\n"
        '{"traditional_chinese_title":"作品名稱",'
        '"traditional_chinese_synopsis":"作品摘要"}\n',
        encoding="utf-8",
    )

    store.ensure()

    assert store.read("work_metadata") == "我的翻譯規則\n"
