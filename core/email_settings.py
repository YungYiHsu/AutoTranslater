"""Local Gmail/Kindle settings; passwords only use Windows Credential Manager."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from formatters.utils import atomic_write_text


class EmailSettingsError(ValueError):
    pass


def validate_address(value: str, *, sender: bool) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise EmailSettingsError("信箱格式錯誤，不可含換行或控制字元。")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", value):
        raise EmailSettingsError("請輸入單一完整信箱地址，不含顯示名稱。")
    local, domain = value.split("@")
    if len(value) > 254 or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise EmailSettingsError("信箱格式錯誤。")
    if sender and domain not in {"gmail.com", "googlemail.com"}:
        raise EmailSettingsError("第一版僅支援 Gmail 寄件信箱（@gmail.com／@googlemail.com）。")
    if not sender and domain not in {"kindle.com", "free.kindle.com", "kindle.cn"}:
        raise EmailSettingsError("請填入 Amazon 提供的 Kindle 專用信箱，不是 Amazon 登入信箱。")
    return value


def normalize_password(value: str) -> str:
    value = value.replace(" ", "").strip()
    if not re.fullmatch(r"[A-Za-z]{16}", value):
        raise EmailSettingsError("請填入 Google 的 16 位英文字母應用程式密碼，不是一般帳號密碼。")
    return value


@dataclass(frozen=True)
class EmailSettings:
    sender: str = ""
    recipient: str = ""

    def validated(self) -> EmailSettings:
        return EmailSettings(validate_address(self.sender, sender=True),
                             validate_address(self.recipient, sender=False))


def load_email_settings(path: Path) -> EmailSettings:
    if not path.exists():
        return EmailSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError
        return EmailSettings(data["sender"], data["recipient"]).validated()
    except (OSError, ValueError, KeyError, TypeError):
        raise EmailSettingsError("Email 設定檔無法讀取或格式錯誤，未自動覆蓋。") from None


def save_email_settings(path: Path, settings: EmailSettings) -> None:
    data = {"version": 1, **asdict(settings.validated())}
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        raise EmailSettingsError("無法儲存 Email 設定，請檢查目錄權限。") from None


class EmailCredentials:
    """Explicit secure backend: never allow keyring's plaintext/plugin fallback."""

    SERVICE = "AutoTranslator.KindleEmail.Gmail"

    def _backend(self):
        if sys.platform != "win32":
            raise EmailSettingsError("此功能目前僅支援 Windows 認證管理員。")
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()

    def get(self, sender: str) -> str | None:
        sender = validate_address(sender, sender=True)
        try:
            return self._backend().get_password(self.SERVICE, sender)
        except Exception:  # noqa: BLE001 - never expose credential backend exception details
            raise EmailSettingsError("無法讀取 Windows 認證管理員；不會改讀明文密碼。") from None

    def set(self, sender: str, password: str) -> None:
        sender, password = validate_address(sender, sender=True), normalize_password(password)
        try:
            self._backend().set_password(self.SERVICE, sender, password)
        except Exception:  # noqa: BLE001 - never expose credential backend exception details
            raise EmailSettingsError("無法安全儲存密碼；未改存明文，請檢查 Windows 認證管理員。") from None

    def delete(self, sender: str) -> None:
        sender = validate_address(sender, sender=True)
        try:
            backend = self._backend()
            if backend.get_password(self.SERVICE, sender) is not None:
                backend.delete_password(self.SERVICE, sender)
        except Exception:  # noqa: BLE001 - never expose credential backend exception details
            raise EmailSettingsError("無法移除已儲存的應用程式密碼。") from None
