"""Synthetic offline coverage for ebook translation and paired reading outputs."""

import json
import time
import tkinter as tk
from unittest.mock import patch

import pytest

from core.ebook import translation
from core.ebook.chapters import export_chapter
from core.ebook.translation import EbookTranslation, load_source
from core.shared.config import AppConfig
from core.shared.exceptions import TranslationCancelled
from core.shared.prompt_templates import PromptTemplateStore
from tests.ebook.test_ebook_exports import make_work, sample_record
from tests.webnovel.test_controller import RecordingTranslator
from ui.ebook.translation_page import EbookTranslationPage


def wait_idle(page):
    deadline = time.monotonic() + 10
    while page.busy and time.monotonic() < deadline:
        page.update()
        time.sleep(.005)
    assert not page.busy


def setup(tmp_path, number=0):
    _, directory = make_work(tmp_path)
    export_chapter(directory, sample_record(directory), "synthetic", number, 1, 2)
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    return directory, store.path_for("chapter")


def job(directory, prompt, *, translator=None, mode="gemini", overwrite=False):
    with patch("core.shared.translation_bootstrap.ApiLlmTranslator.from_config", return_value=translator or RecordingTranslator()), \
         patch("core.shared.translation_bootstrap.ApiTermAnalyzer.from_config", return_value=None):
        return EbookTranslation(directory, 0, config=AppConfig(model="mock"), mode=mode,
                                api_key="synthetic", prompt_path=prompt, overwrite=overwrite)


def test_zero_fallback_and_complete_outputs(tmp_path):
    directory, prompt = setup(tmp_path)
    translator = RecordingTranslator()
    service = job(directory, prompt, translator=translator)
    result = service.run()
    assert not translator.title_calls
    assert [p.name for p in result.output_paths] == ["000.txt", "000.html"]
    html = result.output_paths[1].read_text(encoding="utf-8")
    assert "第 000 章" in html and "譯文 0" in html
    assert "start_page" not in html
    index = json.loads((directory / "chapters.json").read_text(encoding="utf-8"))
    assert index["0"]["translation_source_sha256"] == load_source(directory, 0).digest
    assert (directory / "originals" / "000.txt").read_text(encoding="utf-8") == "aB\n"
    assert not (directory / "codex_extraction_thread.json").exists()


def test_reading_breaks_after_chunk_merge_preserve_checkpoint_and_source(tmp_path):
    from bs4 import BeautifulSoup

    from formatters.html_formatter import HtmlFormatter
    from formatters.txt_formatter import TxtFormatter
    from formatters.utils import read_translated_text_from_txt

    directory, prompt = setup(tmp_path)
    original = directory / "originals" / "000.txt"
    original.write_text("一段。\n二段。", encoding="utf-8")
    original_before = original.read_bytes()
    config = AppConfig(codex_model="mock")
    service = EbookTranslation(directory, 0, config=config, mode="codex", prompt_path=prompt)
    service.resegment(2)
    chunks = ["「你好。", "再見。」次。完。"]
    with patch("translators.codex_session.CodexSession.ask", side_effect=chunks):
        result = service.run(update_terms=False)
    expected = "「你好。再見。」次。\n完。"
    txt, html = result.output_paths
    assert read_translated_text_from_txt(txt) == expected
    assert HtmlFormatter.txt_matches_html(txt, html)
    soup = BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")
    assert soup.select_one("article.novel-text").get_text() == expected
    assert [c.translated_text for c in result.chapter.chunks] == chunks
    assert original.read_bytes() == original_before
    resumed = EbookTranslation(directory, 0, config=config, mode="codex", prompt_path=prompt, overwrite=True)
    resumed.resegment(2)
    assert [c.translated_text for c in resumed.plan.completed_chunks] == chunks
    with patch("translators.codex_session.CodexSession.ask") as ask:
        resumed.run(update_terms=False)
        ask.assert_not_called()
    assert read_translated_text_from_txt(txt) == expected
    # Default formatter callers (including webnovel) retain their original behavior.
    normal = TxtFormatter().save(result.chapter, tmp_path / "default-output")
    assert read_translated_text_from_txt(normal) == "".join(chunks)


def test_named_chapter_translates_title(tmp_path):
    directory, prompt = setup(tmp_path)
    index_path = directory / "chapters.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["0"]["title"] = "序章"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    translator = RecordingTranslator()
    result = job(directory, prompt, translator=translator).run()
    assert translator.title_calls == ["序章"]
    assert result.chapter.translated_chapter_title == "譯名：序章"


def test_resume_force_and_segmentation(tmp_path):
    directory, prompt = setup(tmp_path)
    (directory / "originals" / "000.txt").write_text("一段。\n二段。", encoding="utf-8")
    translator = RecordingTranslator(fail_at=1)
    first = job(directory, prompt, translator=translator)
    first.resegment(2)
    with pytest.raises(RuntimeError):
        first.run()
    assert not (directory / "translations").exists()
    second_translator = RecordingTranslator()
    second = job(directory, prompt, translator=second_translator)
    second.resegment(2)
    assert second.plan.completed_count == 1
    second.run()
    assert second_translator.calls == [1]
    third_translator = RecordingTranslator()
    third = job(directory, prompt, translator=third_translator, overwrite=True)
    third.resegment(2)
    third.run(force=True)
    assert third_translator.calls == [0, 1]


def test_cancel_keeps_checkpoint_without_output(tmp_path):
    directory, prompt = setup(tmp_path)
    (directory / "originals" / "000.txt").write_text("一段。\n二段。", encoding="utf-8")
    service = job(directory, prompt)
    service.resegment(2)
    cancelled = []
    def progress(update):
        if update.source == "translated":
            cancelled.append(True)
    with pytest.raises(TranslationCancelled):
        service.run(progress=progress, should_cancel=lambda: bool(cancelled))
    assert not (directory / "translations").exists()
    assert job(directory, prompt).resegment(2).completed_count == 1


@pytest.mark.parametrize("change", ["source", "pages"])
def test_changed_source_is_not_exported(tmp_path, change):
    directory, prompt = setup(tmp_path)
    service = job(directory, prompt)
    def modify(update):
        if update.source != "translated":
            return
        if change == "source":
            (directory / "originals" / "000.txt").write_text("別の文章", encoding="utf-8")
        else:
            path = directory / "chapters.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["0"]["pages"][0]["start_page"] = 99
            path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="已變更"):
        service.run(progress=modify)
    assert not (directory / "translations" / "000.html").exists()


def test_failure_preserves_previous_pair_and_index(tmp_path):
    directory, prompt = setup(tmp_path)
    result = job(directory, prompt).run()
    paths = (*result.output_paths, directory / "chapters.json")
    before = {p: p.read_bytes() for p in paths}
    service = job(directory, prompt, overwrite=True)
    # Index failure occurs after both output replacements and must roll them back.
    with patch.object(translation, "write_json", side_effect=OSError("synthetic disk error")), pytest.raises(OSError):
        service.run(force=True)
    assert {p: p.read_bytes() for p in paths} == before


def test_concurrent_output_change_not_overwritten(tmp_path):
    directory, prompt = setup(tmp_path)
    service = job(directory, prompt, overwrite=True)
    folder = directory / "translations"
    folder.mkdir()
    (folder / "000.txt").write_text("外部修改", encoding="utf-8")
    with pytest.raises(ValueError, match="譯文檔案已變更"):
        service.run()
    assert (folder / "000.txt").read_text(encoding="utf-8") == "外部修改"


def test_codex_translation_uses_separate_session(tmp_path):
    directory, prompt = setup(tmp_path)
    extraction = directory / "codex_extraction_thread.json"
    extraction.write_text('{"thread_id":"extraction"}', encoding="utf-8")
    service = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock"),
                               mode="codex_analysis", prompt_path=prompt)
    assert service.controller._translator.session.path == directory / "codex_session.json"
    with pytest.raises(ValueError, match="分析模式"):
        service.run()
    assert extraction.read_text(encoding="utf-8") == '{"thread_id":"extraction"}'


def test_codex_mock_translation_uses_shared_prompt_and_checkpoint(tmp_path):
    directory, prompt = setup(tmp_path)
    (directory / "terms.json").write_text("invalid legacy terms: do not read", encoding="utf-8")
    service = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock", codex_reasoning_effort="high"),
                               mode="codex", prompt_path=prompt)
    with patch("translators.codex_session.CodexSession.ask", return_value="合成譯文") as ask:
        service.run()
        assert ask.call_count == 1
        assert "aB" in ask.call_args.args[0]
        assert ask.call_args.kwargs["chapter_number"] == 0
    resumed = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock", codex_reasoning_effort="high"),
                               mode="codex", prompt_path=prompt, overwrite=True)
    assert resumed.plan.completed_count == 1
    changed = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock", codex_reasoning_effort="low"),
                               mode="codex_analysis", prompt_path=prompt)
    assert changed.plan.completed_count == 0
    prompt.write_text("請精確翻譯 {Novel_Content}", encoding="utf-8")
    changed_prompt = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock", codex_reasoning_effort="high"),
                                      mode="codex_analysis", prompt_path=prompt)
    assert changed_prompt.plan.completed_count == 0


def test_estimates_include_retries_and_only_real_titles(tmp_path):
    directory, prompt = setup(tmp_path)
    service = job(directory, prompt, mode="analysis")
    assert service.request_estimate() == (0, 1, 1, 2)
    assert service.request_estimate(update_terms=False) == (0, 1, 0, 1)
    codex = EbookTranslation(directory, 0, config=AppConfig(codex_model="mock"),
                             mode="codex_analysis", prompt_path=prompt)
    assert codex.request_estimate() == (0, 1, 0, 1)


def test_render_failure_does_not_create_partial_outputs(tmp_path):
    directory, prompt = setup(tmp_path)
    service = job(directory, prompt)
    before = (directory / "chapters.json").read_bytes()
    with patch("core.ebook.translation.HtmlFormatter.save", side_effect=OSError("render failed")), pytest.raises(OSError):
        service.run()
    assert not (directory / "translations" / "000.txt").exists()
    assert (directory / "chapters.json").read_bytes() == before


def test_navigation_orders_zero_and_missing_chapters(tmp_path):
    directory, prompt = setup(tmp_path)
    job(directory, prompt).run()
    export_chapter(directory, sample_record(directory, 20), "synthetic2", 2, 1, 2)
    with patch("core.shared.translation_bootstrap.ApiLlmTranslator.from_config", return_value=RecordingTranslator()), \
         patch("core.shared.translation_bootstrap.ApiTermAnalyzer.from_config", return_value=None):
        service = EbookTranslation(directory, 2, config=AppConfig(model="mock"), mode="gemini",
                                   api_key="mock", prompt_path=prompt)
    service.run()
    assert 'href="002.html"' in (directory / "translations" / "000.html").read_text(encoding="utf-8")
    assert 'href="000.html"' in (directory / "translations" / "002.html").read_text(encoding="utf-8")


def test_ui_single_chapter_translation_and_busy_guard(tmp_path):
    directory, _ = setup(tmp_path)
    root = tk.Tk()
    root.withdraw()
    page = EbookTranslationPage(root, app_directory=tmp_path, config_provider=lambda: AppConfig(model="mock"))
    try:
        page.load_work(directory)
        assert page.current_key == "0"
        page.models_loaded({"models": [{"model": "mock", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]})
        assert not hasattr(page, "engine_combo")
        assert not hasattr(page, "terms_check")
        assert page.original.cget("height") == 5
        assert not hasattr(page, "editor")
        with patch("translators.codex_session.CodexSession.ask", return_value="合成譯文"), \
             patch("ui.ebook.translation_page.messagebox.askyesno", return_value=True), \
             patch("ui.ebook.translation_page.messagebox.showerror") as error:
            page.prepare()
            assert not page.can_leave()
            wait_idle(page)
            assert page.analysis.plan.total_chunks == 1
            assert "第 1 段" in page.analysis_summary.get()
            page.run()
            wait_idle(page)
            error.assert_not_called()
        assert "翻譯完成" in page.status.get()
        assert (directory / "translations" / "000.html").exists()
        (directory / "originals" / "000.txt").write_text("更新原文", encoding="utf-8")
        page.refresh()
        assert "原文已更新" in page.status.get()
    finally:
        page.shutdown()
        root.destroy()


def test_auto_connection_is_single_flight_and_clears_stale_analysis(tmp_path):
    directory, _ = setup(tmp_path)
    root = tk.Tk()
    root.withdraw()
    page = EbookTranslationPage(root, app_directory=tmp_path)
    try:
        page.load_work(directory)
        catalog = {"models": [{"model": "mock", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]}
        with patch("ui.ebook.translation_page.check_login", return_value=catalog) as connect:
            page.refresh_models()
            page.refresh_models()
            wait_idle(page)
            connect.assert_called_once_with(tmp_path, include_models=True)
        page.prepare()
        wait_idle(page)
        assert page.analysis is not None
        page.chunk_count.set("2")
        assert page.analysis is None
        assert not page.analysis_summary.get()
    finally:
        page.shutdown()
        root.destroy()


def test_analysis_summary_sizes_to_content_and_source_expands(tmp_path):
    root = tk.Tk()
    root.withdraw()
    page = EbookTranslationPage(root, app_directory=tmp_path)
    try:
        page.pack(fill="both", expand=True)
        root.geometry("850x1050")
        root.update_idletasks()
        assert page.original.master.pack_info()["expand"]
        assert not page.analysis_label.master.pack_info()["expand"]
        assert page.analysis_label.winfo_class() == "TLabel"
        page.analysis_summary.set("一行")
        root.update_idletasks()
        short = page.analysis_label.winfo_reqheight()
        page.analysis_summary.set("一行\n二行\n三行")
        root.update_idletasks()
        assert page.analysis_label.winfo_reqheight() > short
        page.clear_analysis()
        root.update_idletasks()
        assert page.analysis_label.winfo_reqheight() <= short
    finally:
        page.shutdown()
        root.destroy()
