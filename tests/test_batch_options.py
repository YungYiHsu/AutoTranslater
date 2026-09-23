"""Batch choices must survive reopening without restarting the main window."""

from dataclasses import replace
from tkinter import TclError, Tk
from types import SimpleNamespace

import pytest

from core.controller import ChapterExecutionOptions
from core.models import NovelChapterEntry
from tests.test_work_setup import make_work
from ui.app import DesktopApp
from ui.batch_dialog import BatchTranslationDialog


@pytest.mark.parametrize("mode", ["gemini", "codex"])
def test_saved_batch_options_reopen(tmp_path, mode):
    try:
        root = Tk()
    except TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    try:
        app = DesktopApp(root, app_directory=tmp_path)
        app.mode.set(mode)
        app.chapter_progress = SimpleNamespace(next_number=1)
        app.translate_title.set(True)
        app.translate_body.set(True)
        app.update_terms.set(True)
        options = ChapterExecutionOptions(False, True, False)
        app._save_execution_options(options)
        assert app._execution_options() == options
        dialog = BatchTranslationDialog(app)
        dialog.withdraw()
        assert not dialog.title_option.get()
        assert dialog.body_option.get()
        assert not dialog.terms_option.get()
        assert not dialog.redo.get()
        assert hasattr(dialog, "terms_control") == (mode == "gemini")
        dialog.close()
        app._save_execution_options(ChapterExecutionOptions(True, False, False))
        dialog = BatchTranslationDialog(app)
        dialog.withdraw()
        assert dialog.title_option.get()
        assert not dialog.body_option.get()
        assert not dialog.terms_option.get()
        if mode == "gemini":
            assert str(dialog.terms_control["state"]) == "disabled"
        dialog.close()
    finally:
        root.destroy()


def test_gemini_estimate_updates_and_warns(tmp_path, monkeypatch):
    try:
        root = Tk()
    except TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    warnings = []
    monkeypatch.setattr("ui.batch_dialog.messagebox.showwarning", lambda *a, **k: warnings.append(a))
    try:
        app = DesktopApp(root, app_directory=tmp_path)
        work = make_work()
        work = replace(work, chapters=tuple(
            NovelChapterEntry(n, str(n), f"{work.source_url}{n}/") for n in range(1, 11)))
        app.work_result = SimpleNamespace(work=SimpleNamespace(source_work=work))
        app.chapter_progress = SimpleNamespace(next_number=1, completed_numbers=frozenset({1, 2}))
        app.translate_title.set(True)
        app.translate_body.set(True)
        app.update_terms.set(True)
        dialog = BatchTranslationDialog(app)
        dialog.withdraw()
        dialog.end.set("10")
        root.update_idletasks()
        assert "至少 24 次" in dialog.estimate.get()
        assert len(warnings) == 1
        dialog.title_option.set(False)
        root.update_idletasks()
        assert "至少 16 次" in dialog.estimate.get()
        dialog.redo.set(True)
        root.update_idletasks()
        assert "至少 20 次" in dialog.estimate.get()
        assert len(warnings) == 1
        dialog.title_option.set(True)
        root.update_idletasks()
        assert "至少 30 次" in dialog.estimate.get()
        assert len(warnings) == 2
        dialog.body_option.set(False)
        root.update_idletasks()
        assert "至少 10 次" in dialog.estimate.get()
        dialog.start.set("bad")
        root.update_idletasks()
        assert "有效章節範圍" in dialog.estimate.get()
        dialog.close()
    finally:
        root.destroy()
