"""Tests for safe standalone HTML output."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import FormatterError
from formatters.html_formatter import HtmlFormatter
from formatters.txt_formatter import TxtFormatter
from tests.formatter_helpers import make_translated_chapter


def prepare_txt(chapter: object, output_dir: Path) -> None:
    TxtFormatter(overwrite=True).save(chapter, output_dir)  # type: ignore[arg-type]


def test_save_renders_standalone_dark_mode_page_and_escapes_content(tmp_path: Path) -> None:
    chapter = make_translated_chapter(
        title="<script>小說</script>",
        translated_work_title="中文小說",
        translated_chapter_title="中文章節",
        translations=('正文<script>alert("x")</script>&結尾',),
    )
    prepare_txt(chapter, tmp_path)
    destination = HtmlFormatter(auto_open=False).save(chapter, tmp_path)
    content = destination.read_text(encoding="utf-8")

    assert '<html lang="zh-Hant">' in content
    assert '<meta charset="utf-8">' in content
    assert 'name="txt-body-sha256"' in content
    assert "AUTOTRANSLATER_NAV_START" in content
    assert "沒有上一章" in content
    assert "沒有下一章" in content
    assert "prefers-color-scheme: dark" in content
    assert "white-space: pre-wrap" in content
    assert "fake / deterministic-v1" in content
    assert "中文小說" in content
    assert "中文章節" in content
    assert "原文：" in content
    assert "&lt;script&gt;" in content
    assert '<script>alert("x")</script>' not in content
    assert "part=1&amp;lang=ja" in content


def test_save_opens_the_written_file_uri_when_enabled(tmp_path: Path) -> None:
    opened_urls: list[str] = []

    def opener(url: str) -> bool:
        opened_urls.append(url)
        return True

    chapter = make_translated_chapter()
    prepare_txt(chapter, tmp_path)
    destination = HtmlFormatter(opener=opener).save(chapter, tmp_path)
    assert opened_urls == [destination.resolve().as_uri()]


@pytest.mark.parametrize("behavior", ["reject", "raise"])
def test_browser_failure_does_not_discard_saved_output(
    tmp_path: Path,
    behavior: str,
) -> None:
    def opener(_url: str) -> bool:
        if behavior == "raise":
            raise RuntimeError("browser unavailable")
        return False

    chapter = make_translated_chapter()
    prepare_txt(chapter, tmp_path)
    destination = HtmlFormatter(opener=opener).save(chapter, tmp_path)
    assert destination.is_file()


def test_save_requires_explicit_overwrite_for_an_existing_file(tmp_path: Path) -> None:
    formatter = HtmlFormatter(auto_open=False)
    chapter = make_translated_chapter()
    prepare_txt(chapter, tmp_path)
    first = formatter.save(chapter, tmp_path)
    with pytest.raises(FormatterError, match="already exists"):
        formatter.save(chapter, tmp_path)
    second = HtmlFormatter(auto_open=False, overwrite=True).save(chapter, tmp_path)
    assert first.name == "0001 - 測試小說.html"
    assert second == first


def test_missing_or_invalid_template_raises_formatter_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.html"
    chapter = make_translated_chapter()
    output = tmp_path / "output"
    prepare_txt(chapter, output)
    with pytest.raises(FormatterError, match="template"):
        HtmlFormatter(auto_open=False, template_path=missing).save(chapter, output)

    invalid = tmp_path / "invalid.html"
    invalid.write_text("{{ unavailable_variable }}", encoding="utf-8")
    with pytest.raises(FormatterError, match="template"):
        HtmlFormatter(auto_open=False, template_path=invalid).save(chapter, output)


def test_save_wraps_filesystem_errors(tmp_path: Path) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("occupied", encoding="utf-8")
    with pytest.raises(FormatterError, match="HTML"):
        HtmlFormatter(auto_open=False).save(make_translated_chapter(), output_file)


def test_save_rejects_wrong_model_type(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        HtmlFormatter(auto_open=False).save(object(), tmp_path)  # type: ignore[arg-type]


def test_html_body_is_regenerated_from_manually_edited_txt(tmp_path: Path) -> None:
    chapter = make_translated_chapter(translations=("原始翻譯",))
    txt_path = TxtFormatter().save(chapter, tmp_path)
    original = txt_path.read_text(encoding="utf-8-sig")
    txt_path.write_text(original.replace("原始翻譯", "人工修訂內容"), encoding="utf-8-sig")

    html_path = HtmlFormatter(auto_open=False).save(chapter, tmp_path)

    html = html_path.read_text(encoding="utf-8")
    assert "人工修訂內容" in html
    assert "原始翻譯" not in html


def test_html_regeneration_preserves_existing_navigation(tmp_path: Path) -> None:
    chapter = make_translated_chapter(translations=("原始翻譯",))
    txt_path = TxtFormatter().save(chapter, tmp_path)
    html_path = HtmlFormatter(auto_open=False).save(chapter, tmp_path)
    existing = html_path.read_text(encoding="utf-8")
    html_path.write_text(existing.replace("沒有下一章", "第 2 章 →"), encoding="utf-8")
    original_txt = txt_path.read_text(encoding="utf-8-sig")
    txt_path.write_text(
        original_txt.replace("原始翻譯", "人工修訂內容"), encoding="utf-8-sig"
    )

    HtmlFormatter(auto_open=False, overwrite=True).save(chapter, tmp_path)

    regenerated = html_path.read_text(encoding="utf-8")
    assert "人工修訂內容" in regenerated
    assert "第 2 章 →" in regenerated


def test_txt_and_html_difference_is_detected_before_overwrite(tmp_path: Path) -> None:
    chapter = make_translated_chapter(translations=("原始翻譯",))
    txt_path = TxtFormatter().save(chapter, tmp_path)
    html_path = HtmlFormatter(auto_open=False).save(chapter, tmp_path)
    assert HtmlFormatter.txt_matches_html(txt_path, html_path)

    original = txt_path.read_text(encoding="utf-8-sig")
    txt_path.write_text(original.replace("原始翻譯", "人工修訂"), encoding="utf-8-sig")

    assert not HtmlFormatter.txt_matches_html(txt_path, html_path)


def test_html_generation_rejects_missing_txt_without_overwriting_html(tmp_path: Path) -> None:
    chapter = make_translated_chapter()
    html_path = tmp_path / "0001 - 測試小說.html"
    html_path.write_text("保留內容", encoding="utf-8")

    with pytest.raises(FormatterError, match="paired TXT"):
        HtmlFormatter(auto_open=False, overwrite=True).save(chapter, tmp_path)

    assert html_path.read_text(encoding="utf-8") == "保留內容"
