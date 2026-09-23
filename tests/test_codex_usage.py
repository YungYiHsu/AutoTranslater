"""Usage and compaction tests use synthetic protocol data only."""

import hashlib
import json

import pytest

from core.codex_usage import (
    batch_total_summary,
    context_summary,
    duration_label,
    quota_delta,
    record,
    usage_summary,
    window_label,
)
from core.exceptions import TranslationError
from tests.test_codex import FakeClient, session
from translators.codex_client import CodexClient
from translators.codex_session import CodexSession


def test_batch_total_includes_title_body_and_failed_known_usage():
    rows = [{"component": c, "status": status, "tokens": tokens} for c, status, tokens in [
        ("title", "completed", {"totalTokens": 10}),
        ("chunk:1", "completed", {"totalTokens": 30}),
        ("chunk:2", "failed", {"totalTokens": 5}),
        ("chunk:3", "unknown", None),
        ("compaction", "completed", {"totalTokens": 999}),
        ("title", "sending", None),
    ]]
    result = batch_total_summary(rows)
    assert "45 tokens" in result
    assert "1 次 Token 用量無法取得" in result
    assert "無法取得" in batch_total_summary(rows[3:4])
    assert "0 tokens（無實際請求）" in batch_total_summary([])


def test_context_estimate_uses_latest_input_not_cumulative():
    client = object.__new__(CodexClient)
    client.token_events = {("t", "r"): [
        {"last": {"inputTokens": 32000}, "total": {"totalTokens": 999999},
         "modelContextWindow": 128000}]}
    estimate = client.context_estimate("t", "r")
    assert estimate == {"input_tokens": 32000, "capacity": 128000}
    assert "25.0%" in context_summary(estimate)
    assert client.context_estimate("other", "r") is None
    client.token_events[("t", "r")].append({"last": {"inputTokens": 3}})
    assert client.context_estimate("t", "r") is None
    assert "無法取得" in context_summary(None)
    assert "無法取得" in context_summary({"input_tokens": 2, "capacity": 0})


@pytest.mark.parametrize("minutes,expected", [
    (300, "5 小時"), (10080, "7 天"), (180, "3 小時"),
    (90, "1 小時 30 分鐘"), (1501, "1 天 1 小時 1 分鐘"),
    (30, "30 分鐘"), (None, "時間未知"), (0, "時間未知"),
])
def test_duration_labels(minutes, expected):
    assert duration_label(minutes) == expected


def test_window_labels_follow_recorded_duration():
    rows = [{"component": "title", "status": "completed", "tokens": None,
             "before": {"rateLimits": {"primary": {"windowDurationMins": duration}}},
             "quota_change": {"codex/primary": 1}} for duration in (180, 300)]
    result = usage_summary(rows, average=True)
    assert "codex/3 小時" in result and "codex/5 小時" in result
    assert "codex/3 小時" in usage_summary(rows[:1])
    assert window_label("codex/secondary") == "codex/時間未知"


def test_usage_aggregates_without_duplicate_updates():
    client = object.__new__(CodexClient)
    first = {"total": {"totalTokens": 110}, "last": {"totalTokens": 10}}
    last = {"total": {"totalTokens": 130}, "last": {"totalTokens": 20}}
    client.token_events = {("t", "r"): [first, first, last, last]}
    assert client.turn_usage("t", "r") == {"totalTokens": 30}
    assert client.turn_usage("t", "other") is None
    client.token_events[("t", "r")].append({"total": {"totalTokens": 50}})
    assert client.turn_usage("t", "r") is None


def test_quota_reset_and_unknown():
    before = {"rateLimits": {"primary": {"usedPercent": 10, "resetsAt": 100, "windowDurationMins": 300}}}
    after = {"rateLimits": {"primary": {"usedPercent": 12, "resetsAt": 100, "windowDurationMins": 300}}}
    assert quota_delta(before, after) == {"codex/primary": 2}
    after["rateLimits"]["primary"]["resetsAt"] = 200
    assert quota_delta(before, after) == {}
    assert quota_delta(None, None) == {}


def test_average_excludes_unknown_and_compaction():
    records = [{"component": "title", "status": "completed", "tokens": {"totalTokens": 10}},
               {"component": "title", "status": "failed", "tokens": {"totalTokens": 20}},
               {"component": "title", "status": "failed", "tokens": None},
               {"component": "compaction", "status": "completed", "tokens": {"totalTokens": 999}}]
    result = usage_summary(records, average=True)
    assert "15 tokens（2 次）" in result
    assert "內文 chunk平均：無法取得" in result


def test_journal_only_actual_requests(tmp_path):
    client = FakeClient()
    s = session(tmp_path, client)
    s.ask("text", key="1", component="title", label="第1章 標題")
    s.ask("text", key="1", component="title", label="第1章 標題")
    rows = [json.loads(line) for line in (tmp_path / "codex_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["id"] == rows[1]["id"]
    assert rows[1]["tokens"] is None
    assert rows[1]["status"] == "completed"


def test_journal_failure_does_not_raise(tmp_path):
    (tmp_path / "codex_usage.jsonl").mkdir()
    record(tmp_path, {"status": "sending"})


class CompactClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.events = []

    def call(self, method, params):
        if method == "thread/compact/start":
            self.calls.append((method, params))
            return {}
        return super().call(method, params)

    def read_limits(self):
        return None

    def turn_usage(self, *args):
        return None

    def wait_compaction(self, thread):
        self.compaction_turn_id = "compact-1"
        if self.disconnect:
            raise TranslationError("disconnected")
        return "compact-1"


@pytest.mark.parametrize("disconnect", [False, True])
def test_compaction_preserves_translation_journal(tmp_path, disconnect):
    client = CompactClient()
    client.disconnect = disconnect
    s = CodexSession(tmp_path, "test", client_factory=lambda _: client)
    data = {"version": 1, "thread_id": "work-thread",
            "owner": hashlib.sha256(client.email.encode()).hexdigest(),
            "pending": {"key": "k", "marker": "m", "status": "completed", "response": "saved"}}
    s._save(data)
    if disconnect:
        with pytest.raises(TranslationError):
            s.compact()
        with pytest.raises(TranslationError, match="未再次送出"):
            s.compact()
        assert sum(m == "thread/compact/start" for m, _ in client.calls) == 1
    else:
        s.compact()
    saved = s._load()
    assert saved["pending"] == data["pending"]
    assert not (tmp_path / "codex_session.lock").exists()
