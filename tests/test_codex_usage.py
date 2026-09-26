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
    assert "tokens" not in result.lower()
    assert "tokens" not in usage_summary(rows).lower()
    assert "無法取得" in batch_total_summary(rows[3:4])
    assert "無實際請求" in batch_total_summary([])


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
    rows = [{"component": "title", "status": "completed", "tokens": None, "chapter_number": 1,
             "before": {"rateLimits": {"primary": {"windowDurationMins": duration}}},
             "quota_change": {"codex/primary": 1}} for duration in (180, 300)]
    result = usage_summary(rows, average=True)
    assert "codex/3 小時" in result and "無法取得" in result
    assert "codex/3 小時" in usage_summary(rows[:1])
    assert window_label("codex/secondary") == "codex/時間未知"


def test_usage_aggregates_without_duplicate_updates():
    client = object.__new__(CodexClient)
    assert not hasattr(client, "turn_usage")


def test_quota_reset_and_unknown():
    before = snapshot(10, 10)
    after = snapshot(12, 20)
    assert quota_delta(before, after) == {"codex/primary": 2}
    after["rateLimits"]["primary"]["resetsAt"] = 200
    assert quota_delta(before, after) == {"codex/primary": 2}
    after["acquired_at"] = 100
    assert quota_delta(before, after) == {}
    assert quota_delta(None, None) == {}


def snapshot(used, acquired, reset=100, duration=300):
    return {"acquired_at": acquired, "rateLimits": {"primary": {
        "usedPercent": used, "resetsAt": reset, "windowDurationMins": duration}}}


@pytest.mark.parametrize("start,end,reset,valid", [
    (10, 20, 10, False), (10, 20, 20, False), (10, 20, 15, False),
    (10, 20, 21, True), (10, 20, 9, False), (20, 10, 100, False),
    (None, 20, 100, False), (10, None, 100, False),
])
def test_reset_snapshot_boundaries(start, end, reset, valid):
    assert bool(quota_delta(snapshot(10, start, reset), snapshot(12, end))) is valid


def test_batch_uses_endpoints_and_all_actual_requests():
    rows = [
        {"id": "a", "component": "title", "status": "sending"},
        {"id": "a", "component": "title", "status": "completed", "chapter_number": 1,
         "before": snapshot(10, 10), "after": snapshot(11, 20)},
        {"id": "b", "component": "chunk:1", "status": "failed", "chapter_number": 1,
         "before": None, "after": None},
        {"id": "c", "component": "chunk:2", "status": "completed", "chapter_number": 2,
         "before": snapshot(15, 30), "after": snapshot(16, 40)},
        {"id": "d", "component": "work-background", "status": "completed",
         "before": snapshot(1, 1), "after": snapshot(99, 99)},
    ]
    assert "6 個百分點" in batch_total_summary(rows)
    average = usage_summary(rows, average=True)
    assert "共 2 章" in average and "3.00 個百分點" in average
    rows[1]["before"] = None
    assert "無法取得" in batch_total_summary(rows)


def test_windows_reset_independently():
    before, after = snapshot(10, 10, 15), snapshot(12, 20)
    for data, used in ((before, 30), (after, 33)):
        data["rateLimits"]["secondary"] = {
            "usedPercent": used, "resetsAt": 1000, "windowDurationMins": 10080}
    assert quota_delta(before, after) == {"codex/secondary": 3}
    text = usage_summary([{"component": "title", "status": "completed",
                           "before": before, "after": after}])
    assert "期間發生額度重置" in text and "7 天 消耗 3 個百分點" in text


@pytest.mark.parametrize("used", [9, -1, 101, float("nan"), True, None])
def test_invalid_or_increased_remaining(used):
    assert quota_delta(snapshot(10, 10), snapshot(used, 20)) == {}


def test_read_limits_captures_acquisition_time(monkeypatch):
    client = object.__new__(CodexClient)
    response = {"rateLimits": {}}
    client.call = lambda *args, **kwargs: response
    monkeypatch.setattr("translators.codex_client.time.time", lambda: 1234.5)
    assert client.read_limits()["acquired_at"] == 1234.5
    assert "acquired_at" not in response


def test_average_excludes_unknown_and_compaction():
    records = [{"component": "title", "status": "completed", "tokens": {"totalTokens": 10}},
               {"component": "title", "status": "failed", "tokens": {"totalTokens": 20}},
               {"component": "title", "status": "failed", "tokens": None},
               {"component": "compaction", "status": "completed", "tokens": {"totalTokens": 999}}]
    result = usage_summary(records, average=True)
    assert "tokens" not in result.lower()
    assert "每章平均消耗：無法取得" in result


def test_journal_only_actual_requests(tmp_path):
    client = FakeClient()
    s = session(tmp_path, client)
    s.ask("text", key="1", component="title", label="第1章 標題", chapter_number=1)
    s.ask("text", key="1", component="title", label="第1章 標題")
    rows = [json.loads(line) for line in (tmp_path / "codex_usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["id"] == rows[1]["id"]
    assert "tokens" not in rows[1]
    assert rows[1]["status"] == "completed"
    assert rows[1]["chapter_number"] == 1


def test_per_chapter_average_merges_title_chunks_and_legacy_labels():
    rows = [{"id": str(i), "component": component, "status": "completed",
             "label": "第 12 章內文", "before": snapshot(10 + i, 10 + i),
             "after": snapshot(11 + i, 11 + i)}
            for i, component in enumerate(("title", "chunk:1", "chunk:2", "chunk:3"))]
    result = usage_summary(rows, average=True)
    assert "共 1 章" in result and "4.00 個百分點" in result
    rows.append({"component": "chunk:1", "status": "recovered", "chapter_number": 99})
    assert usage_summary(rows, average=True) == result
    assert "無實際請求" in usage_summary([], average=True)


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
