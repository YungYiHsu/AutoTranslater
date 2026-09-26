"""Local append-only usage journal. Never sends model requests."""

import json
import logging
import math
import queue
import re
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


def quota_comparison(before, after):
    """Compare UTC snapshot boundaries independently for each account window."""
    def buckets(data):
        if not isinstance(data, dict):
            return {}
        values = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits") or {}}
        return values if isinstance(values, dict) else {}
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)

    result = {}
    start = (before or {}).get("acquired_at") if isinstance(before, dict) else None
    end = (after or {}).get("acquired_at") if isinstance(after, dict) else None
    for name, previous in buckets(before).items():
        current = buckets(after).get(name) or {}
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        for window in ("primary", "secondary"):
            left, right = previous.get(window), current.get(window)
            if not isinstance(left, dict):
                continue
            key = f"{name}/{window}"
            right = right if isinstance(right, dict) else {}
            a, b = left.get("usedPercent"), right.get("usedPercent")
            reset = left.get("resetsAt")
            duration = left.get("windowDurationMins")
            reason = None
            if not (number(start) and number(end) and 0 < start <= end):
                reason = "快照取得時間缺失或異常"
            elif not number(reset):
                reason = "重置時間缺失"
            elif start <= reset <= end:
                reason = "期間發生額度重置"
            elif reset < start:
                reason = "開始快照的重置時間已過期"
            elif type(duration) is not int or duration <= 0 or duration != right.get("windowDurationMins"):
                reason = "額度窗口缺失或改變"
            elif not (number(a) and number(b) and 0 <= a <= 100 and 0 <= b <= 100):
                reason = "額度資料缺失或異常"
            elif b < a:
                reason = "剩餘額度增加，無法計算"
            result[key] = {"delta": None if reason else b - a, "reason": reason}
    return result


def quota_delta(before, after):
    return {key: value["delta"] for key, value in quota_comparison(before, after).items()
            if value["delta"] is not None}


def _batch_rows(records):
    # Preserve request order while merging start/end events of the same request.
    rows = {}
    for index, row in enumerate(records):
        component = row.get("component", "")
        if component == "title" or component.startswith("chunk:"):
            rows[row.get("id") or ("row", index)] = row
    return [r for r in rows.values() if r.get("status") not in ("sending", "recovered")]


def _comparison_text(before, after, divisor=1, *, trim_zeros=False):
    values = quota_comparison(before, after)
    parts = []
    for window, value in values.items():
        label = window_label(window, {"before": before})
        if value["delta"] is None:
            parts.append(f"{label} 無法取得（{value['reason']}）")
        else:
            amount = f"{value['delta'] / divisor:.2f}"
            if trim_zeros:
                amount = amount.rstrip("0").rstrip(".")
            parts.append(f"{label} 消耗 {amount} 個百分點")
    return "、".join(parts) or "無法取得"


def batch_total_summary(records):
    """Use first before and last after, never sum rounded per-step deltas."""
    rows = _batch_rows(records)
    if not rows:
        return "本次總消耗（標題＋內文）：無實際請求"
    lines = ["本次總消耗（標題＋內文）"]
    lines.append("帳號額度首尾變化：" + _comparison_text(
        rows[0].get("before"), rows[-1].get("after"), trim_zeros=True))
    lines.append("額度為帳號觀測值，可能包含其他任務或回報延遲；無效窗口不計算。")
    return "\n".join(lines)


def usage_summary(records, *, average=False):
    finished = [r for r in records if r.get("status") != "sending" and "component" in r]
    if average:
        rows = _batch_rows(records)
        if not rows:
            return "每章平均消耗：無實際請求"
        chapters = set()
        for row in rows:
            number = row.get("chapter_number")
            if type(number) is not int or number < 1:
                # Older records contain the chapter number in the display label.
                match = re.match(r"^第\s*(\d+)\s*章", row.get("label", ""))
                number = int(match[1]) if match else None
            if number is None or number < 1:
                return "每章平均消耗：無法取得（缺少章節資訊）"
            chapters.add((row.get("directory", ""), number))
        return (f"每章平均消耗（共 {len(chapters)} 章，標題＋內文，含失敗請求）\n"
                "帳號額度變化平均：" + _comparison_text(
                    rows[0].get("before"), rows[-1].get("after"), len(chapters)))
    lines = []
    for r in finished:
        lines.append(f"{r.get('label', r['component'])}（{r['status']}）")
        lines.append("帳號額度變化：" + _comparison_text(
            r.get("before"), r.get("after"), trim_zeros=True))
    return "\n".join(lines) or "尚無本次請求紀錄"
