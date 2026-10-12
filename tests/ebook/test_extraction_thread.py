import json
import threading
from unittest.mock import patch

import pytest

from core.ebook.codex_extract import extract
from core.ebook.extraction_thread import locked_thread
from core.shared.config import AppConfig
from tests.ebook.test_codex_extract import Client


def run(folder, work):
    folder.mkdir(exist_ok=True)
    return extract(folder, AppConfig(), threading.Event(), client_factory=Client, usage_directory=work)


def test_same_work_resumes_across_images_and_keeps_latest_prompt(tmp_path):
    Client.calls = []
    run(tmp_path / 'one', tmp_path)
    run(tmp_path / 'two', tmp_path)
    assert [m for m, _ in Client.calls if m.startswith('thread/') and m != 'thread/read'] == ['thread/start', 'thread/resume']
    resume = next(p for m, p in Client.calls if m == 'thread/resume')
    assert resume['threadId'] == 'test-thread'
    assert resume['sandbox'] == 'read-only'
    state = json.loads((tmp_path / 'codex_extraction_thread.json').read_text())
    assert 'pending' not in state
    assert not (tmp_path / 'codex_extraction_thread.lock').exists()


def test_different_work_starts_own_thread(tmp_path):
    Client.calls = []
    for name in ('book-a', 'book-b'):
        work = tmp_path / name
        work.mkdir()
        run(work / 'image', work)
    assert sum(m == 'thread/start' for m, _ in Client.calls) == 2
    assert not any(m == 'thread/resume' for m, _ in Client.calls)


def test_resume_failure_no_new_thread_or_image_send(tmp_path):
    run(tmp_path / 'one', tmp_path)
    Client.calls = []
    with patch.object(Client, 'call', side_effect=ValueError('resume failed')), pytest.raises(ValueError):
        run(tmp_path / 'two', tmp_path)
    assert (tmp_path / 'codex_extraction_thread.json').exists()
    assert not (tmp_path / 'codex_extraction_thread.lock').exists()


def test_account_change_rejected(tmp_path):
    run(tmp_path / 'one', tmp_path)
    Client.calls = []
    with patch.object(Client, 'account', return_value={'email': 'different@example.test'}), pytest.raises(ValueError, match='帳號'):
        run(tmp_path / 'two', tmp_path)
    assert not Client.calls


def test_pending_is_checked_before_new_send(tmp_path):
    run(tmp_path / 'one', tmp_path)
    path = tmp_path / 'codex_extraction_thread.json'
    state = json.loads(path.read_text())
    state['pending'] = {'turn_id': 'unknown', 'marker': 'unknown'}
    path.write_text(json.dumps(state))
    Client.calls = []
    with pytest.raises(ValueError, match='上次提取狀態'):
        run(tmp_path / 'two', tmp_path)
    assert not any(m == 'turn/start' for m, _ in Client.calls)


def test_lock_and_cancel_preserve_thread(tmp_path):
    run(tmp_path / 'one', tmp_path)
    with locked_thread(tmp_path), pytest.raises(ValueError, match='使用中'):
        run(tmp_path / 'two', tmp_path)
    cancel = threading.Event()
    cancel.set()
    before = (tmp_path / 'codex_extraction_thread.json').read_bytes()
    with pytest.raises(ValueError, match='取消'):
        extract(tmp_path / 'one', AppConfig(), cancel, client_factory=Client, usage_directory=tmp_path)
    assert (tmp_path / 'codex_extraction_thread.json').read_bytes() == before
