"""One-EPUB Gmail SMTP delivery with TLS, durable attempt records and no retry."""

from __future__ import annotations

import hashlib
import io
import json
import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from threading import Event
from zipfile import BadZipFile, ZipFile

from core.email_settings import EmailSettings, normalize_password
from formatters.utils import atomic_write_text

MAX_EPUB_BYTES = 18_000_000
MAX_MESSAGE_BYTES = 25_000_000
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class KindleEmailError(RuntimeError):
    pass


class EmailCancelled(KindleEmailError):
    pass


@dataclass(frozen=True)
class PreparedEmail:
    settings: EmailSettings
    filename: str
    title: str
    start: int
    end: int
    size: int
    fingerprint: str
    message_id: str
    payload: bytes = field(repr=False)

    @property
    def history_key(self) -> str:
        return hashlib.sha256((self.fingerprint + "\n" + self.settings.recipient).encode()).hexdigest()


def prepare_email(path: Path, settings: EmailSettings, title: str, start: int, end: int) -> PreparedEmail:
    settings = settings.validated()
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise KindleEmailError("匯出章節範圍無效，請重新匯出 EPUB。")
    try:
        if path.suffix.lower() != ".epub" or not path.is_file():
            raise KindleEmailError("找不到 EPUB，請先匯出。")
        with path.open("rb") as stream:
            data = stream.read(MAX_EPUB_BYTES + 1)
    except OSError:
        raise KindleEmailError("EPUB 無法讀取，請確認檔案仍存在且可存取。") from None
    if not data or len(data) > MAX_EPUB_BYTES:
        raise KindleEmailError("Email 寄送上限為 18 MB，且 EPUB 不可為空；請縮小章節範圍或改用網頁上傳。")
    try:
        with ZipFile(io.BytesIO(data)) as book:
            info = book.getinfo("mimetype")
            if info.file_size != 20 or book.read(info) != b"application/epub+zip":
                raise ValueError
    except (BadZipFile, KeyError, ValueError, RuntimeError):
        raise KindleEmailError("EPUB 結構無效，請重新匯出。") from None
    title = " ".join(title.split())[:180] or path.stem
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = f"{title}｜第 {start}～{end} 章"
    message["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=settings.sender.split("@")[1])
    message["Message-ID"] = message_id
    message.set_content("EPUB 由 AutoTranslator 匯出，請見附件。")
    message.add_attachment(data, maintype="application", subtype="epub+zip", filename=path.name)
    payload = message.as_bytes()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise KindleEmailError("編碼後郵件過大，請縮小章節範圍或改用網頁上傳。")
    return PreparedEmail(settings, path.name, title, start, end, len(data),
                         hashlib.sha256(data).hexdigest(), message_id, payload)


class EmailHistory:
    def __init__(self, path: Path):
        self.path = path

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("attempts"), dict):
                raise ValueError
            if any(not isinstance(v, dict) or not isinstance(v.get("message_id"), str)
                   or v.get("status") not in {"sending", "accepted", "failed", "unknown", "cancelled"}
                   for v in data["attempts"].values()):
                raise ValueError
            return data["attempts"]
        except (OSError, ValueError, TypeError):
            raise KindleEmailError("寄送紀錄無法讀取或已損壞，未自動覆蓋；請先處理紀錄檔。") from None

    def previous(self, email: PreparedEmail):
        return self._load().get(email.history_key)

    def write(self, email: PreparedEmail, status: str):
        data = self._load()
        data[email.history_key] = {
            "sha256": email.fingerprint, "recipient": email.settings.recipient,
            "message_id": email.message_id, "time": datetime.now(UTC).isoformat(), "status": status,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps({"version": 1, "attempts": data}, indent=2), encoding="utf-8")
        except OSError:
            raise KindleEmailError("寄送紀錄無法儲存，請檢查目錄權限。") from None

    @contextmanager
    def locked(self):
        lock = self.path.with_suffix(".lock")
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise KindleEmailError("Email 寄送占用中；請先確認沒有其他程式正在寄送。") from None
        except OSError:
            raise KindleEmailError("無法建立寄送鎖定檔，尚未寄出。") from None
        try:
            yield
        finally:
            handle.close()
            try:
                lock.unlink()
            except OSError:
                pass


def _cancelled(cancel):
    if cancel.is_set():
        raise EmailCancelled("已取消寄送，EPUB 已保留。")


def _error_text(exc, *, sending=False):
    # Do not expose raw server text, which may echo addresses or credentials.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"Gmail 認證失敗（{exc.smtp_code}），請檢查寄件地址與應用程式密碼。"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = sorted({str(value[0]) for value in exc.recipients.values()})
        return f"寄件伺服器拒絕收件人（{', '.join(codes)}），請檢查 Kindle 地址。"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"寄件伺服器拒絕請求（{exc.smtp_code}）。"
    if sending:
        return "寄送結果不明，可能已被伺服器接收；未自動重送，請先查看寄件信箱與 Kindle 書庫。"
    if isinstance(exc, ssl.SSLError):
        return "安全連線驗證失敗，未降低 TLS 安全設定，尚未寄出。"
    if isinstance(exc, TimeoutError):
        return "連線或認證逾時，尚未寄出。"
    if isinstance(exc, (OSError, smtplib.SMTPServerDisconnected)):
        return "網路連線失敗，尚未寄出。"
    return "Email 請求失敗，原因未知；尚未寄出。"


def _close_client(client):
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110 - cleanup cannot invalidate an accepted message
            pass  # An accepted DATA reply must not be reclassified by a close failure.


def test_email_connection(settings: EmailSettings, password: str, *, smtp_factory=None):
    """Authenticate only: no MAIL, RCPT, DATA or test message."""
    settings = settings.validated()
    password = normalize_password(password)
    client = None
    try:
        client = (smtp_factory or smtplib.SMTP_SSL)(SMTP_HOST, SMTP_PORT,
                                                    timeout=30, context=ssl.create_default_context())
        client.login(settings.sender, password)
    except Exception as exc:  # noqa: BLE001 - sanitize all transport errors before display
        raise KindleEmailError(_error_text(exc)) from None
    finally:
        _close_client(client)
    return "Gmail 連線與認證成功；未寄出郵件，也尚未驗證 Kindle 地址或核准寄件人設定。"


def send_email(email: PreparedEmail, password: str, history: EmailHistory, *,
               cancel: Event, status, expected_previous_id=None, smtp_factory=None):
    password = normalize_password(password)
    client = None
    sending = False
    with history.locked():
        previous = history.previous(email)
        if (previous or {}).get("message_id") != expected_previous_id:
            raise KindleEmailError("寄送紀錄已更新或已有寄送紀錄，請重新確認後再寄送。")
        _cancelled(cancel)
        history.write(email, "sending")  # Must succeed before any network submission.
        try:
            status("正在連線 Gmail……")
            client = (smtp_factory or smtplib.SMTP_SSL)(SMTP_HOST, SMTP_PORT,
                                                        timeout=30, context=ssl.create_default_context())
            _cancelled(cancel)
            status("正在驗證 Gmail 寄件帳號……")
            client.login(email.settings.sender, password)
            _cancelled(cancel)
            status("正在寄送 EPUB……送出後無法撤回。")
            sending = True
            refused = client.sendmail(email.settings.sender, [email.settings.recipient], email.payload)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except Exception as exc:  # noqa: BLE001 - persist ambiguous failures without exposing secrets
            known_rejection = isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
            state = "cancelled" if isinstance(exc, EmailCancelled) else (
                "unknown" if sending and not known_rejection else "failed")
            warning = ""
            try:
                history.write(email, state)
            except KindleEmailError:
                warning = "\n寄送結果紀錄寫入失敗，請先查證再重寄。"
            if isinstance(exc, EmailCancelled):
                raise EmailCancelled(str(exc) + warning) from None
            raise KindleEmailError(_error_text(exc, sending=sending) + warning) from None
        finally:
            _close_client(client)
        warning = ""
        try:
            history.write(email, "accepted")
        except KindleEmailError:
            warning = "\n但結果紀錄寫入失敗，請勿直接重寄。"
        return ("郵件已交由 Gmail 接受，請等待 Amazon 處理並查看 Kindle 書庫；"
                "若收到驗證郵件，請依指示確認。" + warning)
