"""No live email or credential access: only in-memory SMTP and keyring doubles."""

import json
import smtplib
import ssl
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from threading import Event
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from core.email_settings import (
    EmailCredentials,
    EmailSettings,
    EmailSettingsError,
    load_email_settings,
    normalize_password,
    save_email_settings,
    validate_address,
)
from core.kindle_email import (
    EmailCancelled,
    EmailHistory,
    KindleEmailError,
    prepare_email,
    send_email,
)
from core.kindle_email import (
    test_email_connection as check_connection,
)

PASSWORD = "abcdefghijklmnop"  # Synthetic credential only.
SETTINGS = EmailSettings("reader@gmail.com", "reader@kindle.com")


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "0001-0003 - 測試.epub"
    with ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr("body.txt", "測試正文")
    return prepare_email(path, SETTINGS, "測試作品", 1, 3)


@pytest.fixture
def history(tmp_path):
    return EmailHistory(tmp_path / "logs" / "kindle_email_history.json")


def smtp_double():
    client = MagicMock()
    client.sendmail.return_value = {}
    factory = MagicMock(return_value=client)
    return factory, client


def deliver(email, history, factory, **kwargs):
    return send_email(email, PASSWORD, history, cancel=kwargs.pop("cancel", Event()),
                      status=lambda text: None, smtp_factory=factory, **kwargs)


def test_message_and_attachment_match_export(prepared):
    message = BytesParser(policy=policy.default).parsebytes(prepared.payload)
    assert message["To"] == SETTINGS.recipient
    assert message["From"] == SETTINGS.sender
    assert message["Subject"] == "測試作品｜第 1～3 章"
    assert message["Message-ID"] == prepared.message_id
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/epub+zip"
    assert attachments[0].get_filename() == prepared.filename
    assert len(attachments[0].get_payload(decode=True)) == prepared.size
    assert prepared.payload not in repr(prepared).encode()


def test_success_is_accepted_not_delivered(prepared, history):
    factory, client = smtp_double()
    result = deliver(prepared, history, factory)
    assert "Gmail 接受" in result and "Amazon" in result
    client.sendmail.assert_called_once_with(SETTINGS.sender, [SETTINGS.recipient], prepared.payload)
    assert history.previous(prepared)["status"] == "accepted"
    assert not history.path.with_suffix(".lock").exists()
    assert PASSWORD not in history.path.read_text()
    assert factory.call_args.args == ("smtp.gmail.com", 465)
    tls = factory.call_args.kwargs["context"]
    assert tls.verify_mode == ssl.CERT_REQUIRED and tls.check_hostname


def test_connection_only_authenticates():
    factory, client = smtp_double()
    assert "未寄出" in check_connection(SETTINGS, PASSWORD, smtp_factory=factory)
    client.login.assert_called_once_with(SETTINGS.sender, PASSWORD)
    client.sendmail.assert_not_called()
    client.mail.assert_not_called()
    client.rcpt.assert_not_called()
    client.data.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.parametrize("error,text", [
    (smtplib.SMTPAuthenticationError(535, PASSWORD.encode()), "認證失敗（535）"),
    (TimeoutError(PASSWORD), "逾時"),
    (OSError(PASSWORD), "網路連線失敗"),
    (ssl.SSLError(PASSWORD), "安全連線驗證失敗"),
])
def test_before_send_errors_redacted(prepared, history, error, text):
    factory, client = smtp_double()
    client.login.side_effect = error
    with pytest.raises(KindleEmailError, match=text) as exc:
        deliver(prepared, history, factory)
    assert PASSWORD not in str(exc.value)
    client.sendmail.assert_not_called()
    assert history.previous(prepared)["status"] == "failed"


@pytest.mark.parametrize("error", [TimeoutError("secret"), smtplib.SMTPServerDisconnected("secret")])
def test_unknown_outcome_never_retries(prepared, history, error):
    factory, client = smtp_double()
    client.sendmail.side_effect = error
    with pytest.raises(KindleEmailError, match="未自動重送"):
        deliver(prepared, history, factory)
    assert client.sendmail.call_count == 1
    assert history.previous(prepared)["status"] == "unknown"


@pytest.mark.parametrize("error", [smtplib.SMTPDataError(552, b"secret"),
    smtplib.SMTPRecipientsRefused({SETTINGS.recipient: (550, b"secret")})])
def test_server_rejection_is_not_accepted(prepared, history, error):
    factory, client = smtp_double()
    client.sendmail.side_effect = error
    with pytest.raises(KindleEmailError, match="拒絕") as exc:
        deliver(prepared, history, factory)
    assert "secret" not in str(exc.value)
    assert history.previous(prepared)["status"] == "failed"


def test_cancel_before_network(prepared, history):
    factory, _ = smtp_double()
    cancel = Event()
    cancel.set()
    with pytest.raises(EmailCancelled):
        deliver(prepared, history, factory, cancel=cancel)
    factory.assert_not_called()
    assert history.previous(prepared) is None


def test_cancel_after_login_never_sends(prepared, history):
    factory, client = smtp_double()
    cancel = Event()
    client.login.side_effect = lambda *args: cancel.set()
    with pytest.raises(EmailCancelled):
        deliver(prepared, history, factory, cancel=cancel)
    client.sendmail.assert_not_called()
    assert history.previous(prepared)["status"] == "cancelled"


def test_close_failure_cannot_override_success(prepared, history):
    factory, client = smtp_double()
    client.close.side_effect = OSError("closed")
    assert "Gmail 接受" in deliver(prepared, history, factory)
    assert history.previous(prepared)["status"] == "accepted"


def test_duplicate_and_changed_history_require_confirmation(prepared, history):
    factory, _ = smtp_double()
    deliver(prepared, history, factory)
    with pytest.raises(KindleEmailError, match="寄送紀錄"):
        deliver(prepared, history, factory)
    assert factory.call_count == 1
    second = replace(prepared, message_id="<new-message>")
    assert "Gmail 接受" in deliver(second, history, factory, expected_previous_id=prepared.message_id)
    assert len(history._load()) == 1


def test_lock_prevents_parallel_sending(prepared, history):
    factory, _ = smtp_double()
    with history.locked(), pytest.raises(KindleEmailError, match="占用中"):
        deliver(prepared, history, factory)
    factory.assert_not_called()


def test_history_failure_before_send_blocks_network(prepared, history, monkeypatch):
    factory, _ = smtp_double()
    monkeypatch.setattr(history, "write", MagicMock(side_effect=KindleEmailError("儲存失敗")))
    with pytest.raises(KindleEmailError):
        deliver(prepared, history, factory)
    factory.assert_not_called()


def test_history_failure_after_acceptance_is_warning(prepared, history, monkeypatch):
    factory, _ = smtp_double()
    original = history.write

    def write(email, state):
        if state == "accepted":
            raise KindleEmailError("儲存失敗")
        original(email, state)

    monkeypatch.setattr(history, "write", write)
    assert "但結果紀錄寫入失敗" in deliver(prepared, history, factory)
    assert history.previous(prepared)["status"] == "sending"


def test_corrupt_history_preserved(prepared, history):
    history.path.parent.mkdir()
    history.path.write_text("invalid")
    with pytest.raises(KindleEmailError, match="損壞"):
        history.previous(prepared)
    assert history.path.read_text() == "invalid"


@pytest.mark.parametrize("value", ["bad", "Name <a@gmail.com>", "a@gmail.com\r\nBcc: b@gmail.com",
                                   "a@outlook.com", "a..b@gmail.com", "a@gmail.com,b@gmail.com"])
def test_sender_validation(value):
    with pytest.raises(EmailSettingsError):
        validate_address(value, sender=True)


def test_settings_never_save_password(tmp_path):
    path = tmp_path / "kindle_email.json"
    assert load_email_settings(path) == EmailSettings()
    save_email_settings(path, SETTINGS)
    assert load_email_settings(path) == SETTINGS
    assert set(json.loads(path.read_text())) == {"version", "sender", "recipient"}
    path.write_text("bad")
    with pytest.raises(EmailSettingsError):
        load_email_settings(path)
    assert path.read_text() == "bad"


def test_credentials_explicit_backend_and_masked_errors(monkeypatch):
    store = EmailCredentials()
    backend = MagicMock()
    monkeypatch.setattr(store, "_backend", lambda: backend)
    store.set(SETTINGS.sender, "abcd efgh ijkl mnop")
    backend.set_password.assert_called_once_with(store.SERVICE, SETTINGS.sender, PASSWORD)
    backend.get_password.return_value = PASSWORD
    assert store.get(SETTINGS.sender) == PASSWORD
    store.delete(SETTINGS.sender)
    backend.delete_password.assert_called_once_with(store.SERVICE, SETTINGS.sender)
    backend.set_password.side_effect = RuntimeError(PASSWORD)
    with pytest.raises(EmailSettingsError) as exc:
        store.set(SETTINGS.sender, PASSWORD)
    assert PASSWORD not in str(exc.value)


def test_file_checks(tmp_path, monkeypatch):
    path = tmp_path / "bad.epub"
    with pytest.raises(KindleEmailError, match="找不到"):
        prepare_email(path, SETTINGS, "title", 1, 2)
    path.write_bytes(b"")
    with pytest.raises(KindleEmailError, match="不可為空"):
        prepare_email(path, SETTINGS, "title", 1, 2)
    path.write_bytes(b"not EPUB")
    with pytest.raises(KindleEmailError, match="結構無效"):
        prepare_email(path, SETTINGS, "title", 1, 2)
    monkeypatch.setattr("core.kindle_email.MAX_EPUB_BYTES", 3)
    with pytest.raises(KindleEmailError, match="18 MB"):
        prepare_email(path, SETTINGS, "title", 1, 2)


def test_encoded_size_checked(prepared, tmp_path, monkeypatch):
    monkeypatch.setattr("core.kindle_email.MAX_MESSAGE_BYTES", 10)
    with pytest.raises(KindleEmailError, match="編碼後"):
        prepare_email(tmp_path / prepared.filename, SETTINGS, "title", 1, 2)


def test_password_and_recipient_validation():
    assert normalize_password("abcd efgh ijkl mnop") == PASSWORD
    with pytest.raises(EmailSettingsError):
        normalize_password("normal-password")
    with pytest.raises(EmailSettingsError):
        validate_address("reader@gmail.com", sender=False)
