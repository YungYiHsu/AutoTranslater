"""Local Codex app-server transport. Authentication stays in Codex, never this app."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Self

from core.exceptions import TranslationError


def find_codex() -> str:
    executable = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if executable:
        return executable
    if os.name == "nt":
        directory = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        candidates = list(directory.glob("*/codex.exe"))
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    raise TranslationError("找不到 Codex。請先安裝 Codex CLI 或桌面版，並以 ChatGPT 登入。")


class CodexClient:
    """One private stdio server; no model calls during initialization/account checks."""

    def __init__(self, cwd: Path, *, timeout: float = 900) -> None:
        self.cwd = cwd.resolve()
        self.timeout = timeout
        self._serial = 0
        self.events: list[dict[str, Any]] = []
        self.token_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.latest_limits = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        environment = os.environ.copy()
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            environment.pop(name, None)
        self._process = subprocess.Popen(
            [find_codex(), "app-server", "--stdio", "-c", 'forced_login_method="chatgpt"',
             "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
             "-c", "features.apps=false", "-c", 'web_search="disabled"',
             "-c", "project_doc_max_bytes=0", "-c", "features.memories=false"],
            cwd=self.cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "autotranslater", "version": "0.4.0"}})
            self._send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        for pipe in (self._process.stdin, self._process.stdout):
            if pipe is not None:
                pipe.close()

    def _read(self) -> None:
        try:
            assert self._process.stdout is not None
            for line in self._process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        self._messages.put(message)
                except ValueError:
                    continue
        finally:
            self._messages.put(None)

    def _send(self, value: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _next(self, deadline: float) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TranslationError("Codex 等待逾時。請稍後重試以查詢原回合，勿立即重複送出。") from exc
        if message is None:
            raise TranslationError("Codex 連線已中斷。請確認本機 Codex 可啟動並已登入。")
        params = message.get("params", {})
        if message.get("method") == "thread/tokenUsage/updated":
            key = (params.get("threadId"), params.get("turnId"))
            usage = params.get("tokenUsage") or {}
            self.token_events[key] = [{"last": {"inputTokens": (usage.get("last") or {}).get("inputTokens")},
                                       "modelContextWindow": usage.get("modelContextWindow")}]
        elif message.get("method") == "account/rateLimits/updated":
            self.latest_limits = params
        if "method" in message and "id" in message:
            # This application never approves tools, filesystem changes, or external actions.
            self._send({"id": message["id"], "error": {"code": -32601, "message": "Tools disabled"}})
        return message

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout=30) -> dict[str, Any]:
        self._serial += 1
        request_id = self._serial
        self._send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message = self._next(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message["error"]
                    if "thread already has an active writer" in str(error.get("message", "")).lower():
                        raise TranslationError("占用中，可以嘗試關閉APP")
                    raise TranslationError(f"Codex：{error.get('message', '請求失敗')}")
                return message.get("result", {})
            self.events.append(message)

    def account(self) -> dict[str, Any]:
        account = self.call("account/read", {"refreshToken": False}).get("account")
        if not account or account.get("type") != "chatgpt":
            raise TranslationError("Codex 尚未以 ChatGPT 登入。請使用「登入 Codex」。不會改用 API Key。")
        return account

    def read_limits(self):
        try:
            self.latest_limits = self.call("account/rateLimits/read", timeout=3)
            if isinstance(self.latest_limits, dict):
                self.latest_limits = {**self.latest_limits, "acquired_at": time.time()}
            return self.latest_limits
        except TranslationError:
            return None

    def context_estimate(self, thread_id, turn_id):
        updates = self.token_events.get((thread_id, turn_id), [])
        if not updates:
            return None
        latest = updates[-1]
        last = latest.get("last") or {}
        used, capacity = last.get("inputTokens"), latest.get("modelContextWindow")
        if type(used) is not int or used < 0 or type(capacity) is not int or capacity <= 0:
            return None
        return {"input_tokens": used, "capacity": capacity}

    def wait_compaction(self, thread_id):
        deadline = time.monotonic() + self.timeout
        compact_turn = None
        while True:
            message = self.events.pop(0) if self.events else self._next(deadline)
            params = message.get("params", {})
            if params.get("threadId") != thread_id:
                continue
            if message.get("method") == "turn/started":
                compact_turn = params.get("turn", {}).get("id")
                self.compaction_turn_id = compact_turn
            if message.get("method") == "turn/completed" and compact_turn:
                turn = params.get("turn", {})
                if turn.get("id") == compact_turn:
                    if turn.get("status") != "completed":
                        raise TranslationError(f"壓縮失敗：{turn.get('error') or turn.get('status')}")
                    return compact_turn

    def translation_config(self) -> dict[str, Any]:
        # Empty tables can merge with existing settings. Disable each configured
        # integration explicitly without altering the user's on-disk configuration.
        config = self.call("config/read", {"includeLayers": False, "cwd": str(self.cwd)})["config"]
        overrides: dict[str, Any] = {}
        for group in ("mcp_servers", "plugins", "apps"):
            entries = config.get(group) or {}
            overrides[group] = {name: {"enabled": False} for name in entries}
        overrides["apps"]["_default"] = {"enabled": False}
        return overrides

    def models(self) -> list[dict[str, Any]]:
        """Read the picker catalog without starting a model turn."""
        models: dict[str, dict[str, Any]] = {}
        cursor = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = self.call("model/list", params)
            for item in result.get("data", []):
                if isinstance(item, dict) and isinstance(item.get("model"), str):
                    models[item["model"]] = item
            cursor = result.get("nextCursor")
            if not cursor:
                return list(models.values())
            if cursor in seen:
                raise TranslationError("Codex 模型清單分頁異常，請重新整理。")
            seen.add(cursor)

    def wait_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            message = self.events.pop(0) if self.events else self._next(deadline)
            params = message.get("params", {})
            if (message.get("method") == "turn/completed"
                    and params.get("threadId") == thread_id
                    and params.get("turn", {}).get("id") == turn_id):
                return params["turn"]


def check_login(cwd: Path, *, include_models: bool = False) -> dict[str, Any]:
    with CodexClient(cwd) as client:
        account = client.account()
        if include_models:
            account["models"] = client.models()
        return account


def login() -> None:
    """Use the official browser login flow; the caller runs on a worker thread."""
    result = subprocess.run(
        [find_codex(), "login"], stdin=subprocess.DEVNULL, capture_output=True,
        check=False, text=True, encoding="utf-8", timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise TranslationError("Codex 登入未完成。請確認瀏覽器登入成功後重試。")
