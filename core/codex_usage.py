"""Local append-only usage journal. Never sends model requests."""

import json
import logging
import queue
from datetime import UTC, datetime

events = queue.Queue()


def context_summary(estimate):
    if not isinstance(estimate, dict):
        return "最近輸入上下文估計：無法取得"
    used, capacity = estimate.get("input_tokens"), estimate.get("capacity")
    if type(used) is not int or used < 0 or type(capacity) is not int or capacity <= 0:
        return "最近輸入上下文估計：無法取得"
    return (f"最近輸入上下文估計：約 {used:,}／{capacity:,} Tokens（{used / capacity:.1%}）"
            "\n以最近回報為準，不代表實際記住的內容比例。")


def duration_label(minutes):
    """Format reported minutes without rounding away partial hours or days."""
    if type(minutes) is not int or minutes <= 0:
        return "時間未知"
    days, remainder = divmod(minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    return " ".join(f"{value} {unit}" for value, unit in
                    ((days, "天"), (hours, "小時"), (minutes, "分鐘")) if value)


def window_label(window, record=None):
    name, _, key = window.rpartition("/")
    snapshot = (record or {}).get("before") or {}
    buckets = snapshot.get("rateLimitsByLimitId") or {"codex": snapshot.get("rateLimits") or {}}
    data = (buckets.get(name) or {}).get(key) or {}
    return f"{name}/{duration_label(data.get('windowDurationMins'))}"


def record(directory, entry):
    entry = {**entry, "directory": str(directory.resolve()), "time": datetime.now(UTC).isoformat()}
    try:
        with (directory / "codex_usage.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        logging.getLogger(__name__).exception("Unable to save Codex usage")
        events.put({"warning": "用量紀錄寫入失敗；翻譯繼續。"})
    events.put(entry)


def quota_delta(before, after):
    """Compare only identical account windows, never infer per-request billing."""
    def buckets(data):
        if not isinstance(data, dict):
            return {}
        values = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits") or {}}
        return values if isinstance(values, dict) else {}
    result = {}
    for name, previous in buckets(before).items():
        current = buckets(after).get(name) or {}
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        for window in ("primary", "secondary"):
            left, right = previous.get(window), current.get(window)
            if not isinstance(left, dict) or not isinstance(right, dict):
                continue
            if (left.get("resetsAt") is None or left.get("resetsAt") != right.get("resetsAt")
                    or left.get("windowDurationMins") != right.get("windowDurationMins")):
                continue
            a, b = left.get("usedPercent"), right.get("usedPercent")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b >= a:
                result[f"{name}/{window}"] = b - a
    return result


def batch_total_summary(records):
    """Sum actual title/body requests; unknown measurements remain explicit."""
    rows = [r for r in records if r.get("status") != "sending"
            and (r.get("component") == "title" or r.get("component", "").startswith("chunk:"))]
    values = [(r.get("tokens") or {}).get("totalTokens") for r in rows]
    known = [v for v in values if type(v) is int and v >= 0]
    missing = len(rows) - len(known)
    if not rows:
        return "本次總消耗（標題＋內文）：0 tokens（無實際請求）"
    total = f"{sum(known):,} tokens" if known else "無法取得"
    lines = [f"本次總消耗（標題＋內文）：{total}"]
    if missing:
        lines.append(f"僅計已知用量；另有 {missing} 次 Token 用量無法取得。")
    changes = {}
    for row in rows:
        for window, delta in (row.get("quota_change") or {}).items():
            label = window_label(window, row)
            changes[label] = changes.get(label, 0) + delta
    if changes:
        lines.append("帳號額度已知變化合計：" + "、".join(
            f"{label} 消耗 {delta:g} 個百分點" for label, delta in changes.items()))
        lines.append("可能包含其他任務；缺失或跨重置的額度變化未計入。")
    else:
        lines.append("帳號額度變化合計：無法取得")
    return "\n".join(lines)


def usage_summary(records, *, average=False):
    finished = [r for r in records if r.get("status") != "sending" and "component" in r]
    if average:
        lines = []
        for kind, label in (("title", "標題"), ("chunk:", "內文 chunk")):
            values = [r["tokens"]["totalTokens"] for r in finished
                      if r["component"].startswith(kind) and isinstance(r.get("tokens"), dict)
                      and isinstance(r["tokens"].get("totalTokens"), int)]
            lines.append(f"{label}平均：{sum(values)/len(values):,.0f} tokens（{len(values)} 次）"
                         if values else f"{label}平均：無法取得")
            changes = {}
            for row in finished:
                if row["component"].startswith(kind):
                    for window, delta in (row.get("quota_change") or {}).items():
                        changes.setdefault(window_label(window, row), []).append(delta)
            if changes:
                lines.append("帳號額度變化平均：" + "、".join(
                    f"{window} 消耗 {sum(items)/len(items):.2f} 個百分點（{len(items)} 次）"
                    for window, items in changes.items()))
            else:
                lines.append("帳號額度變化平均：無法取得")
        lines.append("額度為帳號觀測值，可能包含其他任務；未知值不計入平均。")
        return "\n".join(lines)
    lines = []
    values = []
    for r in finished:
        total = (r.get("tokens") or {}).get("totalTokens")
        text = f"{total:,} tokens" if isinstance(total, int) else "用量無法取得"
        lines.append(f"{r.get('label', r['component'])}：{text}（{r['status']}）")
        if r.get("quota_change"):
            lines.append("帳號額度變化：" + "、".join(
                f"{window_label(window, r)} 消耗 {delta:g} 個百分點"
                for window, delta in r["quota_change"].items()))
        if isinstance(total, int):
            values.append(total)
    if values:
        lines.append(f"已知用量合計：{sum(values):,} tokens")
    return "\n".join(lines) or "尚無本次請求紀錄"
