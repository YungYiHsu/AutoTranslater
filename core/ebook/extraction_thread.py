"""Durable per-work Codex extraction thread, isolated from OCR backups."""
import hashlib
import json
from contextlib import contextmanager

from core.shared.storage import write_json


@contextmanager
def locked_thread(directory):
    path = directory / 'codex_extraction_thread.json'
    lock = directory / 'codex_extraction_thread.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('此作品提取對話使用中；若上次程式異常關閉，請確認沒有提取工作後移除 codex_extraction_thread.lock。') from exc
    try:
        with handle:
            data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if not isinstance(data, dict) or (data and (
                data.get('schema') != 1 or not isinstance(data.get('thread_id'), str)
                or not data['thread_id'] or not data.get('owner')
                or data.get('work_directory') != str(directory.resolve())
            )):
                raise ValueError('提取對話紀錄不合法或不屬於此作品，未建立新對話。')
            yield path, data
    finally:
        lock.unlink(missing_ok=True)


def open_thread(client, account, params, path, data):
    email = account.get('email')
    if not email:
        raise ValueError('無法確認 Codex 帳號身分，請重新檢查登入。')
    owner = hashlib.sha256(email.encode()).hexdigest()
    if data.get('owner') and data['owner'] != owner:
        raise ValueError('Codex 登入帳號與此作品提取對話不同，請切回原帳號。')
    existing = data.get('thread_id')
    opened = client.call('thread/resume' if existing else 'thread/start',
                         {**params, **({'threadId': existing} if existing else {})})
    thread = opened['thread']['id']
    if not thread or (existing and thread != existing):
        raise ValueError('恢復的提取對話識別不符，未送出圖片。')
    data.update(schema=1, thread_id=thread, owner=owner, work_directory=str(path.parent.resolve()))
    write_json(path, data)
    if data.get('pending'):
        pending = data['pending']
        history = client.call('thread/read', {'threadId': thread, 'includeTurns': True})
        turns = history['thread'].get('turns', [])
        previous = next((t for t in turns if t.get('id') == pending.get('turn_id')), None)
        if previous is None:
            previous = next((t for t in turns if pending['marker'] in json.dumps(t.get('items', []))), None)
        if previous is None or previous.get('status') not in ('completed', 'failed', 'interrupted'):
            raise ValueError('上次提取狀態尚未確認或仍進行中，未送出新圖片；請稍後重試或檢查原對話。')
        data.pop('pending')
        write_json(path, data)
    return opened
