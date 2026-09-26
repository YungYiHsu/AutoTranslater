"""EPUB dialog state tests; browser and uploads are mocked."""

from threading import Event
from tkinter import Tk
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ui.epub_dialog import EpubExportDialog


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    monkeypatch.setattr("ui.epub_dialog.local_chapter_numbers", lambda setup: (2, 3, 4))
    setup = SimpleNamespace(work_directory=tmp_path, work=SimpleNamespace(translated_title="測試作品"))
    root = Tk()
    root.withdraw()
    window = EpubExportDialog(root, setup, app_directory=tmp_path)
    window.withdraw()
    yield window
    root.destroy()


def test_upload_only_enabled_after_export(dialog, tmp_path, monkeypatch):
    assert dialog.upload_button.instate(["disabled"])
    path = tmp_path / "2-4.epub"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(dialog, "_open_web", Mock())
    dialog._results.put(path)
    dialog._poll()
    assert dialog.output == path
    assert not dialog.upload_button.instate(["disabled"])
    assert not dialog.email_button.instate(["disabled"])
    dialog._open_web.assert_not_called()
    assert dialog.export_button.cget("text") == "匯出EPUB"


def test_missing_file_does_not_start_upload(dialog, tmp_path, monkeypatch):
    error = Mock()
    monkeypatch.setattr("ui.epub_dialog.messagebox.showerror", error)
    dialog.output = tmp_path / "missing.epub"
    dialog._start_upload()
    assert error.called and not dialog._busy


def test_confirmation_uses_exported_not_current_range(dialog, tmp_path, monkeypatch):
    confirm = Mock(return_value=False)
    monkeypatch.setattr("ui.epub_dialog.messagebox.askyesno", confirm)
    dialog.output = tmp_path / "2-4.epub"
    dialog.output.write_bytes(b"fixture")
    dialog._exported_range = (2, 4)
    dialog.start.set("90")
    dialog.end.set("99")
    dialog._start_upload()
    assert "第 2～4 章" in confirm.call_args.args[1]
    assert not dialog._busy


def test_upload_completion_restores_controls(dialog, monkeypatch):
    message = Mock()
    monkeypatch.setattr("ui.epub_dialog.messagebox.showinfo", message)
    dialog._busy = dialog._uploading = True
    dialog._upload_events.put(("done", "Amazon 已確認接收"))
    dialog._poll_upload()
    assert not dialog._busy and not dialog._uploading
    assert not dialog.upload_button.instate(["disabled"])
    assert dialog.cancel_upload_button.instate(["disabled"])
    assert message.called


def test_close_during_upload_signals_cancel_without_destroying(dialog):
    dialog._busy = dialog._uploading = True
    dialog._upload_cancel = Event()
    dialog._close()
    assert dialog._upload_cancel.is_set() and dialog.winfo_exists()


def test_final_confirmation_restores_topmost(dialog, monkeypatch):
    changes = []
    monkeypatch.setattr(dialog, "attributes", lambda *args: changes.append(args) if len(args) > 1 else False)
    monkeypatch.setattr(dialog, "deiconify", Mock())
    monkeypatch.setattr(dialog, "lift", Mock())
    monkeypatch.setattr(dialog, "focus_force", Mock())

    def confirm(*args, **kwargs):
        assert changes[-1] == ("-topmost", True)
        assert kwargs["parent"] is dialog
        return True

    monkeypatch.setattr("ui.epub_dialog.messagebox.askyesno", confirm)
    assert dialog._confirm_upload("test")
    assert changes[-1] == ("-topmost", False)


def test_email_missing_settings_does_not_start(dialog, tmp_path, monkeypatch):
    error = Mock()
    monkeypatch.setattr("ui.epub_dialog.messagebox.showerror", error)
    dialog.output = tmp_path / "book.epub"
    dialog._exported_range = (2, 4)
    dialog._start_email()
    assert error.called and not dialog._busy


def test_email_worker_uses_exported_range_and_confirmation(dialog, tmp_path, monkeypatch):
    from zipfile import ZipFile

    from core.email_settings import EmailSettings, save_email_settings
    from core.kindle_email import EmailCancelled

    save_email_settings(tmp_path / "kindle_email.json", EmailSettings("a@gmail.com", "a@kindle.com"))
    dialog.output = tmp_path / "book.epub"
    with ZipFile(dialog.output, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
    dialog._exported_range = (2, 4)
    dialog.start.set("90")
    dialog.end.set("99")
    begin = Mock()
    monkeypatch.setattr(dialog, "_begin_transfer", begin)
    sender = Mock()
    monkeypatch.setattr("ui.epub_dialog.send_email", sender)
    dialog._start_email()
    assert begin.call_args.kwargs == {"email": True}
    confirmation = Mock(return_value=False)
    with pytest.raises(EmailCancelled):
        begin.call_args.args[0](confirmation)
    assert "第 2～4 章" in confirmation.call_args.args[0]
    assert "a@kindle.com" in confirmation.call_args.args[0]
    sender.assert_not_called()
