"""Batch choices must survive reopening without restarting the main window."""

import threading
import time
from dataclasses import replace
from tkinter import TclError, Tk
from types import SimpleNamespace

import pytest

from core.shared.models import NovelChapterEntry
from core.webnovel.controller import ChapterExecutionOptions
from tests.webnovel.test_work_setup import make_work
from ui.webnovel.batch_dialog import BatchTranslationDialog
from ui.webnovel.page import WebnovelPage as DesktopApp


def test_batch_scan_is_background_and_cancel_prevents_translation(tmp_path):
    root = Tk()
    root.withdraw()
    entered, release = threading.Event(), threading.Event()
    try:
        app = DesktopApp(root, app_directory=tmp_path)
        app.mode.set('gemini')
        app.work_result = SimpleNamespace(work=SimpleNamespace(source_work=make_work()), work_directory=tmp_path)
        progress = SimpleNamespace(next_number=1, completed_numbers=frozenset())
        app.chapter_progress = progress
        caller_ids = []
        def reconcile(_setup):
            caller_ids.append(threading.get_ident())
            entered.set()
            assert release.wait(3)
            return progress
        app.chapter_tracker = SimpleNamespace(
            reconcile=reconcile, output_paths=lambda *_: (tmp_path / '1.txt', tmp_path / '1.html'))
        dialog = BatchTranslationDialog(app)
        dialog.withdraw()
        confirmations = []
        dialog._confirm_batch = lambda *args: confirmations.append(args)
        dialog.begin_batch()
        assert dialog.busy
        assert entered.wait(1)
        assert caller_ids != [threading.get_ident()]
        dialog.cancel()
        release.set()
        deadline = time.monotonic() + 2
        while dialog.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.005)
        assert not dialog.busy and confirmations == []
        assert dialog.begin.instate(['!disabled'])
        dialog.close()
    finally:
        release.set()
        root.destroy()


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
    monkeypatch.setattr("ui.webnovel.batch_dialog.messagebox.showwarning", lambda *a, **k: warnings.append(a))
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
