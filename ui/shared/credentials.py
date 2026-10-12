"""Shared masked Gemini credential prompt for both translation workspaces."""

from tkinter import simpledialog

from core.shared.config import get_api_key, load_environment, save_api_key


def get_or_request_api_key(config, app_directory, parent):
    path = app_directory / ".env"
    load_environment(path)
    existing = get_api_key(config)
    if existing:
        return existing
    entered = simpledialog.askstring(
        "Gemini API Key", "請輸入 API Key（內容會儲存在本機 .env）：",
        show="*", parent=parent,
    )
    if entered is None or not entered.strip():
        return None
    return save_api_key(config, entered, env_path=path)
