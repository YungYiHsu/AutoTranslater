import io
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from core.ebook.chapters import commit_export, prepare_export
from core.ebook.ocr import session
from core.ebook.ocr.recognizer_process import ProcessRecognizer
from core.ebook.works import EbookWorkStore
from tests.ebook.test_ebook_core import FakeRecognizer
from ui.shared.background import submit


def test_bridge_timeout_terminates_owned_process_and_can_reset(tmp_path):
    ended = threading.Event()

    class Reader(io.StringIO):
        def readline(self):
            ended.wait(2)
            return ""

    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=Reader(),
        poll=lambda: 0 if ended.is_set() else None,
        terminate=ended.set,
        wait=lambda **_: 0,
    )
    engine = ProcessRecognizer(tmp_path, load_timeout=0.03)
    engine.process = process
    with pytest.raises(TimeoutError):
        engine.request({"action": "load"})
    assert ended.is_set() and engine.process is None


def test_bridge_reads_response_and_worker_errors(tmp_path):
    engine = ProcessRecognizer(tmp_path)
    engine.process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO('{"seconds": 1}\n{"error":"failure"}\n'),
        poll=lambda: None,
    )
    assert engine.request({"action": "load"}) == {"seconds": 1}
    with pytest.raises(RuntimeError, match="failure"):
        engine.request({"action": "recognize"})


def test_recognition_writes_manifest_per_row_and_derived_only_at_end(tmp_path):
    image = Image.new("RGB", (200, 100), "black")
    layout = session.default_layout()
    layout.update(
        width=200,
        height=100,
        line_regions={
            "right": [
                {"box": [150, 10, 170, 90], "kind": "body"},
                {"box": [120, 10, 140, 90], "kind": "body"},
            ],
            "left": [],
        },
    )
    folder, data = session.create_session(image, session_root=tmp_path)
    session.prepare_session(folder, data, image, layout)
    calls = []

    def write(path, text):
        calls.append(path.name)

    with patch.object(session, "atomic_text", side_effect=write):
        session.run_recognition(folder, data, FakeRecognizer(), threading.Event(), lambda *_: None)
    assert calls.count("raw-merged.txt") == 1
    assert calls.count("raw-columns.txt") == 1
    assert all(
        r["status"] == "done" for r in json.loads((folder / "manifest.json").read_text())["columns"]
    )


def test_export_plan_reuses_merge_and_rejects_changed_index(tmp_path):
    data = {
        "work_capture": {"work_id": tmp_path.name, "right": 0, "left": 1},
        "columns": [{"id": "right_01_01", "status": "done", "raw": "text"}],
    }
    plan = prepare_export(tmp_path, data, "record", 0, 1, 1)
    assert commit_export(plan).read_text(encoding="utf-8") == "text\n"
    with pytest.raises(ValueError, match="已變更"):
        commit_export(plan)


def test_create_work_does_not_scan_unrelated_works(tmp_path):
    store = EbookWorkStore(tmp_path)
    metadata = {
        "url": "https://bookwalker.jp/de00000000-0000-0000-0000-000000000001/",
        "title": "test",
    }
    with patch.object(store, "list_works", side_effect=AssertionError("full scan")):
        first, created = store.create(metadata)
        assert created
        assert store.create(metadata) == (first, False)


def test_background_only_queues_callback_and_errors():
    events, thread_ids, results = queue.Queue(), [], []
    main = threading.get_ident()

    def work():
        thread_ids.append(threading.get_ident())
        return 123

    callbacks = {1: results.append}
    submit(events, work, 1)
    kind, (token, value) = events.get(timeout=2)
    assert kind == "job_done" and thread_ids[0] != main and results == []
    callbacks.pop(token)(value)
    assert results == [123]
    submit(events, lambda: 1 / 0, 2)
    kind, (token, error) = events.get(timeout=2)
    assert kind == "job_error" and token == 2 and isinstance(error, ZeroDivisionError)


def test_timeout_keeps_unattempted_rows_pending(tmp_path):
    image = Image.new('RGB', (200, 100), 'black')
    layout = session.default_layout()
    layout.update(width=200, height=100, line_regions={
        'right': [{'box': [150, 10, 170, 90], 'kind': 'body'},
                  {'box': [120, 10, 140, 90], 'kind': 'body'}], 'left': []})
    folder, data = session.create_session(image, session_root=tmp_path)
    session.prepare_session(folder, data, image, layout)
    class Stalled(FakeRecognizer):
        def recognize(self, image):
            raise TimeoutError('worker timed out')
    session.run_recognition(folder, data, Stalled(), threading.Event(), lambda *_: None)
    assert data['status'] == 'partial'
    assert [r['status'] for r in data['columns']] == ['error', 'pending']
    assert json.loads((folder / 'manifest.json').read_text())['error'] == 'worker timed out'


def test_invalid_bridge_response_closes_worker(tmp_path):
    engine = ProcessRecognizer(tmp_path)
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO('not json\n'), poll=lambda: None)
    engine.process = process
    with patch.object(engine, 'close') as close:
        with pytest.raises(RuntimeError, match='格式錯誤'):
            engine.request({'action': 'load'})
        close.assert_called_once()
