"""Codex protocol/session coverage: no real login, network, or model requests."""

from __future__ import annotations

import json
import queue
from pathlib import Path
from typing import Any

import pytest

from core.api_usage import DailyApiUsage, ObservedApiUsage
from core.bootstrap import build_translation_controller
from core.config import AppConfig, ConfigurationError
from core.controller import ChapterExecutionOptions
from core.exceptions import InvalidLlmResponseError, TranslationError
from core.models import NovelChapter, TextChunk
from tests.test_app_model import make_app
from tests.test_controller import StaticExtractor
from translators.api_work import ApiWorkTranslator
from translators.codex_client import CodexClient
from translators.codex_llm import CodexTranslator
from translators.codex_session import CodexSession, final_text


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.turns: list[dict[str, Any]] = []
        self.email = "test@example.invalid"
        self.disconnect = False
        self.lose_ack = False
        self.response = "翻譯正文。"
        self.status = "completed"

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def account(self) -> dict[str, str]:
        return {"type": "chatgpt", "email": self.email}

    def translation_config(self) -> dict[str, Any]:
        return {"apps": {"_default": {"enabled": False}}}

    def models(self) -> list[dict[str, Any]]:
        return [{"model": "test-model", "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]}]

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": "work-thread"}, "model": "test-model"}
        if method == "thread/read":
            return {"thread": {"turns": self.turns}}
        assert method == "turn/start"
        turn = {
            "id": f"turn-{len(self.turns)}", "status": self.status,
            "items": [
                {"type": "userMessage", "content": params["input"]},
                {"type": "agentMessage", "text": "處理中", "phase": "commentary"},
                {"type": "agentMessage", "text": self.response, "phase": "final_answer"},
            ],
        }
        self.turns.append(turn)
        if self.lose_ack:
            raise TranslationError("lost acknowledgement")
        return {"turn": turn}

    def wait_turn(self, *_args: str) -> None:
        if self.disconnect:
            raise TranslationError("disconnected")

    @property
    def sent(self) -> int:
        return sum(method == "turn/start" for method, _ in self.calls)


def session(path: Path, client: FakeClient, **kwargs: Any) -> CodexSession:
    return CodexSession(path, "default", client_factory=lambda _path: client, **kwargs)


def test_new_chapter_thread_initializes_background_once(tmp_path):
    (tmp_path / "work.json").write_text(json.dumps({
        "source": {"title": "日文", "synopsis": "原文摘要"},
        "translation": {"title": "中文", "synopsis": "舊摘要"}}), encoding="utf-8")
    (tmp_path / "synopsis.txt").write_text("中文作品名稱：中文\n摘要：\n手動摘要", encoding="utf-8")
    client = FakeClient()
    s = session(tmp_path, client)
    assert s.background_request_count() == 1
    s.ask("正文", key="c1", component="chunk:1")
    assert client.sent == 2
    assert "手動摘要" in client.turns[0]["items"][0]["content"][0]["text"]
    assert s.background_request_count() == 0
    s.ask("下章", key="c2", component="chunk:1")
    assert client.sent == 3


def test_metadata_initializes_without_extra_request(tmp_path):
    client = FakeClient()
    s = session(tmp_path, client)
    s.ask("作品名稱與摘要", key="work-metadata")
    assert s._load()["background_thread_id"] == "work-thread"
    s.ask("正文", key="c1", component="chunk:1")
    assert client.sent == 2


def test_background_disconnect_recovers_without_resending(tmp_path):
    (tmp_path / "work.json").write_text(json.dumps({"source": {"synopsis": "摘要"}}), encoding="utf-8")
    client = FakeClient()
    client.disconnect = True
    s = session(tmp_path, client)
    with pytest.raises(TranslationError):
        s.ask("正文", key="c1", component="chunk:1")
    assert client.sent == 1
    client.disconnect = False
    s.ask("正文", key="c1", component="chunk:1")
    assert client.sent == 2


def test_resume_per_work_and_pin_default_model(tmp_path: Path) -> None:
    client = FakeClient()
    assert session(tmp_path, client).ask("第一段", key="1") == "翻譯正文。"
    session(tmp_path, client).ask("第二段", key="2")
    assert client.sent == 2
    resumed = next(p for m, p in client.calls if m == "thread/resume")
    assert resumed["threadId"] == "work-thread"
    assert resumed["model"] == "test-model"
    assert resumed["sandbox"] == "read-only"
    assert resumed["approvalPolicy"] == "never"
    assert not (tmp_path / "codex_session.lock").exists()
    other = tmp_path / "another-work"
    session(other, client).ask("第三段", key="3")
    assert sum(m == "thread/start" for m, _ in client.calls) == 2


def test_completed_reply_reused_but_explicit_force_resends(tmp_path: Path) -> None:
    client = FakeClient()
    callbacks: list[bool] = []
    for _ in range(2):
        session(tmp_path, client).ask("第一段", key="1", on_request=lambda: callbacks.append(True))
    assert client.sent == 1
    assert callbacks == [True]
    session(tmp_path, client).ask("第一段", key="1", force=True)
    assert client.sent == 2


@pytest.mark.parametrize("lost_ack", [False, True])
def test_recover_completed_turn_without_duplicate_even_with_force(tmp_path: Path, lost_ack: bool) -> None:
    client = FakeClient()
    client.lose_ack = lost_ack
    client.disconnect = not lost_ack
    with pytest.raises(TranslationError):
        session(tmp_path, client).ask("第一段", key="1")
    client.lose_ack = client.disconnect = False
    assert session(tmp_path, client).ask("第一段", key="1", force=True) == "翻譯正文。"
    assert client.sent == 1


def test_unknown_turn_never_automatically_resends(tmp_path: Path) -> None:
    client = FakeClient()
    client.disconnect = True
    with pytest.raises(TranslationError):
        session(tmp_path, client).ask("第一段", key="1")
    client.turns.clear()
    with pytest.raises(TranslationError, match="狀態不明"):
        session(tmp_path, client).ask("第一段", key="1")
    assert client.sent == 1


@pytest.mark.parametrize("status,response", [("failed", ""), ("completed", "")])
def test_known_failure_allows_next_manual_attempt(tmp_path: Path, status: str, response: str) -> None:
    client = FakeClient()
    client.status, client.response = status, response
    with pytest.raises(TranslationError):
        session(tmp_path, client).ask("第一段", key="1")
    assert "pending" not in json.loads((tmp_path / "codex_session.json").read_text())
    client.status, client.response = "completed", "正文"
    assert session(tmp_path, client).ask("第一段", key="1") == "正文"
    assert client.sent == 2


def test_invalid_metadata_not_cached_forever(tmp_path: Path) -> None:
    client = FakeClient()
    with pytest.raises(InvalidLlmResponseError):
        session(tmp_path, client, validate_response=ApiWorkTranslator._parse_response).ask("資料", key="meta")
    client.response = '{"traditional_chinese_title":"作品","traditional_chinese_synopsis":"摘要"}'
    result = session(tmp_path, client, validate_response=ApiWorkTranslator._parse_response).ask("資料", key="meta")
    assert json.loads(result)["traditional_chinese_title"] == "作品"
    assert client.sent == 2


def test_account_change_is_rejected(tmp_path: Path) -> None:
    client = FakeClient()
    session(tmp_path, client).ask("第一段", key="1")
    client.email = "another@example.invalid"
    with pytest.raises(TranslationError, match="帳號"):
        session(tmp_path, client).ask("第二段", key="2")
    assert client.sent == 1


@pytest.mark.parametrize("content", ["broken-json", "[]", '{"version":1,"pending":42}'])
def test_corrupt_session_is_preserved(tmp_path: Path, content: str) -> None:
    path = tmp_path / "codex_session.json"
    path.write_text(content)
    client = FakeClient()
    with pytest.raises(TranslationError):
        session(tmp_path, client).ask("第一段", key="1")
    assert path.read_text() == content
    assert not client.calls


def test_lock_prevents_second_instance(tmp_path: Path) -> None:
    (tmp_path / "codex_session.lock").touch()
    with pytest.raises(TranslationError, match="鎖定"):
        session(tmp_path, FakeClient()).ask("第一段", key="1")
    assert (tmp_path / "codex_session.lock").exists()


def test_final_answer_excludes_commentary() -> None:
    assert final_text({"status": "completed", "items": [
        {"type": "agentMessage", "phase": "commentary", "text": "正在翻譯"},
        {"type": "agentMessage", "phase": "final_answer", "text": "正文"},
    ]}) == "正文"


def test_transport_correlates_reply_and_retains_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CodexClient.__new__(CodexClient)
    client._serial = 0
    client.timeout = 0.01
    client.events = []
    client._messages = queue.Queue()
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(client, "_send", writes.append)
    client._messages.put({"method": "turn/completed", "params": {
        "threadId": "thread", "turn": {"id": "turn", "status": "completed"},
    }})
    client._messages.put({"id": 1, "result": {"ok": True}})
    assert client.call("example") == {"ok": True}
    assert client.wait_turn("thread", "turn")["status"] == "completed"
    assert writes == [{"id": 1, "method": "example", "params": {}}]


def test_transport_denies_server_tool_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CodexClient.__new__(CodexClient)
    client._serial = 0
    client.events = []
    client._messages = queue.Queue()
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(client, "_send", writes.append)
    client._messages.put({"id": 99, "method": "item/commandExecution/requestApproval"})
    client._messages.put({"id": 1, "result": {}})
    client.call("example")
    assert writes[1]["id"] == 99
    assert "error" in writes[1]


@pytest.mark.parametrize("account", [None, {"type": "apiKey"}])
def test_account_requires_chatgpt(monkeypatch: pytest.MonkeyPatch, account: Any) -> None:
    client = CodexClient.__new__(CodexClient)
    monkeypatch.setattr(client, "call", lambda *_a: {"account": account})
    with pytest.raises(TranslationError, match="ChatGPT"):
        client.account()


def test_tools_disabled_individually(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = CodexClient.__new__(CodexClient)
    client.cwd = tmp_path
    monkeypatch.setattr(client, "call", lambda *_a: {"config": {
        "mcp_servers": {"example": {"enabled": True}}, "plugins": {"p": {}}, "apps": {"a": {}},
    }})
    overrides = client.translation_config()
    assert overrides["mcp_servers"]["example"] == {"enabled": False}
    assert overrides["plugins"]["p"] == {"enabled": False}
    assert overrides["apps"]["a"] == {"enabled": False}


def test_observer_does_not_count_codex_as_gemini(tmp_path: Path) -> None:
    usage = DailyApiUsage(tmp_path / "usage.json")
    calls: list[bool] = []
    observer = ObservedApiUsage(usage, lambda: calls.append(True), count_requests=False)
    observer.record("codex")
    observer.record("codex")
    assert calls == [True]
    assert not usage.path.exists()


def test_codex_gui_model_and_options_are_separate(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    app.model_name.set("codex-test")
    app._codex_models = [{**FakeClient().models()[0], "model": "codex-test"}]
    assert app._commit_model_selection()
    assert app.config.codex_model == "codex-test"
    assert app.config.model == "gemini-3.5-flash"
    assert not app._execution_options().update_terms
    assert app._available_model_choices() == ("codex-test",)


def test_codex_prompt_uses_template_and_terms(tmp_path: Path) -> None:
    template = tmp_path / "prompt.txt"
    template.write_text("規則甲\n{Novel_Content}\n{Term_Memory}", encoding="utf-8")
    translator = CodexTranslator(tmp_path, prompt_path=template)
    client = FakeClient()
    translator.session = session(tmp_path, client)
    result = translator.translate(TextChunk(0, "アルベルト"), {"アルベルト": "阿爾貝特"})
    assert result.translated_text == "翻譯正文。"
    prompt = next(p for m, p in client.calls if m == "turn/start")["input"][0]["text"]
    assert "規則甲" in prompt and "アルベルト → 阿爾貝特" in prompt


def test_codex_controller_checkpoint_and_outputs(tmp_path: Path) -> None:
    chapter = NovelChapter(title="作品", chapter_title="章節", original_text="第一段。\n\n第二段。", source_url="https://ncode.syosetu.com/n0000aa/1/")
    controller = build_translation_controller(
        config=AppConfig(chunk_size=6, codex_model="test-model"), output_directory=tmp_path / "outputs",
        checkpoint_directory=tmp_path / "checkpoints", mode="codex_analysis",
        auto_open=False, work_directory=tmp_path / "outputs" / "作品",
        chapter_extractor=StaticExtractor(chapter),
    )
    client = FakeClient()
    controller._translator.session = session(tmp_path / "outputs" / "作品", client)
    plan = controller.prepare(chapter.source_url)
    assert client.sent == 0
    options = ChapterExecutionOptions(update_terms=False)
    result = controller.run(plan, options=options)
    assert client.sent == 1 + plan.total_chunks
    assert all(path.exists() for path in result.output_paths)
    resumed = controller.prepare(chapter.source_url)
    assert resumed.pending_count == 0 and resumed.pending_title_count == 0


def test_effort_sent_and_default_resets_previous_value(tmp_path: Path) -> None:
    client = FakeClient()
    session(tmp_path, client, reasoning_effort="high").ask("第一段", key="1")
    session(tmp_path, client).ask("第二段", key="2")
    requests = [p for m, p in client.calls if m == "turn/start"]
    assert [p["effort"] for p in requests] == ["high", "low"]


def test_unsupported_effort_does_not_send_turn(tmp_path: Path) -> None:
    client = FakeClient()
    with pytest.raises(TranslationError, match="推理強度"):
        session(tmp_path, client, reasoning_effort="ultra").ask("第一段", key="1")
    assert client.sent == 0
    assert "pending" not in json.loads((tmp_path / "codex_session.json").read_text())


def test_effort_changes_session_reply_cache_and_checkpoint_identity(tmp_path: Path) -> None:
    client = FakeClient()
    for effort in ("low", "high"):
        session(tmp_path, client, reasoning_effort=effort).ask("相同內容", key="1")
    assert client.sent == 2
    low = CodexTranslator(tmp_path, reasoning_effort="low")
    high = CodexTranslator(tmp_path, reasoning_effort="high")
    assert low.checkpoint_identity != high.checkpoint_identity


def test_model_catalog_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CodexClient.__new__(CodexClient)
    pages = iter([
        {"data": [{"model": "a"}], "nextCursor": "next"},
        {"data": [{"model": "b"}], "nextCursor": None},
    ])
    calls = []
    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        return next(pages)
    monkeypatch.setattr(client, "call", call)
    assert [m["model"] for m in client.models()] == ["a", "b"]
    assert calls[1][1]["cursor"] == "next"
    assert all(c[0] == "model/list" for c in calls)


def test_model_catalog_repeated_cursor_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CodexClient.__new__(CodexClient)
    monkeypatch.setattr(client, "call", lambda *_: {"data": [], "nextCursor": "same"})
    with pytest.raises(TranslationError, match="分頁"):
        client.models()


def test_gui_uses_discovered_models_and_saves_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    app._codex_models = FakeClient().models()
    app.model_name.set("test-model")
    assert app._commit_model_selection()
    assert app.config.codex_reasoning_effort == "low"
    app.codex_effort.set("high")
    assert app._commit_model_selection()
    assert "test-model" in app._available_model_choices()
    assert app._available_codex_efforts("test-model") == ("low", "medium", "high")
    assert app.config.codex_reasoning_effort == "high"
    saved = AppConfig.from_mapping(json.loads((tmp_path / "config.json").read_text()))
    assert saved.codex_model == "test-model" and saved.codex_reasoning_effort == "high"
    assert saved.model == "gemini-3.5-flash"
    app.model_name.set("unknown-custom")
    monkeypatch.setattr("ui.app.messagebox.showwarning", lambda *a, **k: None)
    assert not app._commit_model_selection()
    assert app.config.codex_reasoning_effort == "high"
    assert app._available_codex_efforts("unknown-custom") == ()


def test_legacy_default_effort_becomes_low(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    app.config = AppConfig.from_mapping({"codex_model": "test-model", "codex_reasoning_effort": "default"})
    app._codex_models = FakeClient().models()
    app.model_name.set("test-model")
    app.codex_effort.set("default")
    assert app._commit_model_selection()
    assert app.config.codex_model_efforts["test-model"] == "low"


def test_effort_change_invalidates_prepared_plan(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    app._codex_models = FakeClient().models()
    app.model_name.set("test-model")
    app._commit_model_selection()
    app.plan = object()
    app.codex_effort.set("medium")
    app._commit_model_selection()
    assert app.plan is None and app.state == "work_ready"


@pytest.mark.parametrize("value", ["unsupported", None, 123, []])
def test_config_rejects_invalid_effort(value: Any) -> None:
    with pytest.raises(ConfigurationError):
        AppConfig.from_mapping({"codex_reasoning_effort": value})


def test_effort_is_remembered_per_model_and_after_restart(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    first = FakeClient().models()[0]
    app._codex_models = [first, {**first, "model": "model-b"}]
    app.model_name.set("test-model")
    app._commit_model_selection()
    app.codex_effort.set("high")
    app._commit_model_selection()
    app.model_name.set("model-b")
    app._commit_model_selection()
    assert app.codex_effort.get() == "low"
    app.codex_effort.set("medium")
    app._commit_model_selection()
    app.model_name.set("test-model")
    app._commit_model_selection()
    assert app.codex_effort.get() == "high"
    app.config = AppConfig.from_mapping(json.loads((tmp_path / "config.json").read_text()))
    app.model_name.set("model-b")
    app._commit_model_selection()
    assert app.codex_effort.get() == "medium"


def test_legacy_effort_migrates_only_to_its_model() -> None:
    config = AppConfig.from_mapping({"codex_model": "old-model", "codex_reasoning_effort": "high"})
    assert config.codex_model_efforts == {"old-model": "high"}


@pytest.mark.parametrize("mapping", [[], {"model": "bad"}, {"model": []}, {"": "low"}])
def test_invalid_model_effort_map_rejected(mapping: Any) -> None:
    with pytest.raises(ConfigurationError):
        AppConfig.from_mapping({"codex_model_efforts": mapping})


def test_new_model_without_low_uses_its_default(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.mode.set("codex")
    app._codex_models = [{"model": "no-low", "defaultReasoningEffort": "medium",
                          "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]
    app.model_name.set("no-low")
    app._commit_model_selection()
    assert app.codex_effort.get() == "medium"
