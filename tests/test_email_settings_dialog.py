"""Settings controls use a mocked credential store, never Windows credentials."""

from tkinter import Tk
from unittest.mock import MagicMock

import pytest

from core.email_settings import EmailSettings, load_email_settings
from ui.email_settings_dialog import EmailSettingsDialog


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    backend = MagicMock()
    monkeypatch.setattr("ui.email_settings_dialog.EmailCredentials", lambda: backend)
    root = Tk()
    root.withdraw()
    window = EmailSettingsDialog(root, tmp_path / "kindle_email.json")
    window.withdraw()
    window.sender.set("reader@gmail.com")
    window.recipient.set("reader@kindle.com")
    yield window, backend
    root.destroy()


def test_settings_save_password_separately_and_clear_field(dialog):
    window, backend = dialog
    window.password.set("abcdefghijklmnop")
    window._save()
    backend.set.assert_called_once_with("reader@gmail.com", "abcdefghijklmnop")
    assert window.password.get() == ""
    assert load_email_settings(window.settings_path) == EmailSettings("reader@gmail.com", "reader@kindle.com")
    assert "abcdefghijklmnop" not in window.settings_path.read_text()


def test_blank_password_does_not_overwrite_existing(dialog):
    window, backend = dialog
    window._save()
    backend.set.assert_not_called()


def test_delete_requires_confirmation(dialog, monkeypatch):
    window, backend = dialog
    confirm = MagicMock(return_value=False)
    monkeypatch.setattr("ui.email_settings_dialog.messagebox.askyesno", confirm)
    window._delete()
    backend.delete.assert_not_called()
    confirm.return_value = True
    window._delete()
    backend.delete.assert_called_once_with("reader@gmail.com")


def test_failed_secure_save_does_not_write_config(dialog, monkeypatch):
    from core.email_settings import EmailSettingsError

    window, backend = dialog
    backend.set.side_effect = EmailSettingsError("credential store unavailable")
    monkeypatch.setattr("ui.email_settings_dialog.messagebox.showerror", MagicMock())
    window.password.set("abcdefghijklmnop")
    window._save()
    assert not window.settings_path.exists()
