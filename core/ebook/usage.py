"""Per-work extraction usage, independent of the webnovel event consumer."""
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from core.shared.codex_usage import duration_label, record
from translators.codex_client import CodexClient


def limits(client):
    try:
        return client.read_limits() if hasattr(client, 'read_limits') else None
    except Exception:  # noqa: BLE001 - telemetry must not interrupt extraction.
        return None


@contextmanager
def observe(client, directory, cancel, *, model=None, effort=None):
    entry = {'id': uuid.uuid4().hex, 'component': 'ebook-extract', 'label': '圖片提取',
             'before': limits(client), 'status': 'sending', 'model': model, 'effort': effort}
    started = time.monotonic()
    record(directory, entry, publish=False)
    status = 'failed'
    try:
        yield entry
        status = 'completed'
    finally:
        record(directory, {**entry, 'after': limits(client),
                           'status': 'cancelled' if cancel.is_set() else status,
                           'elapsed_seconds': time.monotonic() - started}, publish=False)


def history(directory):
    path = directory / 'codex_usage.jsonl'
    records = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
                records[row['id']] = row
            except (ValueError, KeyError, TypeError):
                continue
    return list(records.values())


def model_summary(entry):
    return f"模型：{entry.get('model') or '未記錄'}｜推理強度：{entry.get('effort') or '未記錄'}"


def query(directory):
    with CodexClient(directory) as client:
        client.account()
        return limits(client)


def limits_text(data):
    buckets = (data or {}).get('rateLimitsByLimitId') or {'codex': (data or {}).get('rateLimits') or {}}
    lines = []
    for name, bucket in buckets.items():
        for key in ('primary', 'secondary'):
            window = bucket.get(key) or {}
            if window.get('usedPercent') is not None:
                reset = window.get('resetsAt')
                when = datetime.fromtimestamp(reset).astimezone().strftime('%m/%d %H:%M') if reset else '未知'
                lines.append(f"{name} {duration_label(window.get('windowDurationMins'))}：剩餘 {max(0, min(100, 100-window['usedPercent'])):g}%，重置 {when}")
    return '\n'.join(lines) or '帳號額度：無法取得'
