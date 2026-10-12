import json
import queue
import threading
import tkinter as tk
from typing import ClassVar
from unittest.mock import patch

import pytest
from PIL import Image

from core.ebook.chapters import commit_export, prepare_export
from core.ebook.codex_extract import extract, parse_response
from core.shared.config import AppConfig
from core.shared.exceptions import InvalidLlmResponseError
from tests.ebook.test_ebook_exports import make_work, sample_record
from translators.codex_client import CodexClient
from ui.ebook.codex_page import CodexExtractPage


class Client:
    calls: ClassVar[list] = []
    response = '本文\n次段'

    def __init__(self, folder):
        self.folder = folder

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def account(self):
        return {"type": "chatgpt", "email": "synthetic@example.test"}

    def translation_config(self):
        return {}

    def call(self, method, params):
        self.calls.append((method, params))
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'test-thread'}, 'model': 'chosen-model'}
        if method == 'turn/start':
            return {'turn': {'id': 'test-turn'}}
        if method == 'thread/read':
            return {'thread': {'turns': [{'id': 'test-turn', 'status': 'completed',
                    'items': [{'type': 'agentMessage', 'text': self.response}]}]}}
        raise AssertionError(method)

    def wait_turn(self, thread, turn, *, cancel):
        pass


def test_transport_image_settings_and_backup(tmp_path):
    Client.calls = []
    Image.new('RGB', (20, 10)).save(tmp_path / 'original.png')
    result = extract(tmp_path, AppConfig(codex_model='chosen-model'), threading.Event(), client_factory=Client)
    assert result == Client.response
    start = next(params for method, params in Client.calls if method == 'turn/start')
    assert start['input'][1] == {'type': 'localImage', 'path': str((tmp_path / 'original.png').resolve())}
    assert start['effort'] == 'low'
    assert 'outputSchema' not in start
    assert (tmp_path / 'raw-response.txt').read_text(encoding='utf-8') == Client.response
    assert json.loads((tmp_path / 'request.json').read_text())['status'] == 'completed'
    assert not any(method == 'thread/resume' for method, _ in Client.calls)


def test_bad_response_kept_and_no_retry(tmp_path):
    Client.calls = []
    with patch.object(Client, 'response', '   '), pytest.raises(InvalidLlmResponseError):
        extract(tmp_path, AppConfig(), threading.Event(), client_factory=Client)
    assert json.loads((tmp_path / 'raw-turn.json').read_text())['items'][0]['text'] == '   '
    assert sum(method == 'turn/start' for method, _ in Client.calls) == 1


def test_cancel_before_sending(tmp_path):
    Client.calls = []
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match='取消'):
        extract(tmp_path, AppConfig(), cancel, client_factory=Client)
    assert not Client.calls


@pytest.mark.parametrize('text', ['', ' ', '\n'])
def test_invalid_schema(text):
    with pytest.raises(ValueError):
        parse_response(text)


def test_page_replace_keeps_other_pages(tmp_path):
    _, directory = make_work(tmp_path)
    data = sample_record(directory)
    commit_export(prepare_export(directory, data, 'old', 0, 1, 2))
    data['engine'] = 'codex'
    data['text'] = 'replacement'
    data['work_capture']['left'] = None
    plan = prepare_export(directory, data, 'new', 0)
    assert plan.conflict
    commit_export(plan, replace=True)
    rows = json.loads((directory / 'chapters.json').read_text())['0']['pages']
    assert len(rows) == 2
    assert rows[0]['text'] == 'replacement'
    data.pop('engine')
    assert prepare_export(directory, data, 'ocr-again', 0, 1, 1).conflict


def test_ui_capture_save_restore_and_dirty(tmp_path):
    work, directory = make_work(tmp_path)
    root = tk.Tk()
    root.withdraw()
    panel = CodexExtractPage(root, app_directory=tmp_path)
    try:
        panel.bind_work(work, directory)
        panel.accept_image(Image.new('RGB', (40, 20), 'white'))
        assert (panel.folder / 'original.png').exists()
        assert not (directory / 'images').exists()
        panel.editors['text'].insert('1.0', '本文')
        with patch('ui.ebook.codex_page.messagebox.askyesnocancel', return_value=None):
            assert not panel.can_leave()
        panel.save()
        panel.bind_work(work, directory)
        assert panel.values()['text'] == '本文'
        panel.set_busy(True)
        assert not panel.can_leave()
        panel.set_busy(False)
    finally:
        panel.shutdown()
        root.destroy()


def test_wait_turn_interrupts_on_owner_thread():
    client = object.__new__(CodexClient)
    client.timeout = 1
    client.events = []
    client._messages = queue.Queue()
    cancel = threading.Event()
    cancel.set()
    calls = []
    def call(method, params):
        calls.append((method, params))
        client.events.append({'method': 'turn/completed', 'params': {
            'threadId': 't', 'turn': {'id': 'u', 'status': 'interrupted'}}})
    client.call = call
    assert client.wait_turn('t', 'u', cancel=cancel)['status'] == 'interrupted'
    assert calls == [('turn/interrupt', {'threadId': 't', 'turnId': 'u'})]


def test_open_image_preserves_size_cancel_and_no_request(tmp_path):
    work, directory = make_work(tmp_path)
    source = tmp_path / 'input.png'
    Image.new('RGB', (123, 87), 'white').save(source)
    root = tk.Tk()
    root.withdraw()
    panel = CodexExtractPage(root, app_directory=tmp_path)
    try:
        panel.bind_work(work, directory)
        with patch('ui.ebook.codex_page.filedialog.askopenfilename', return_value=str(source)), \
                patch('ui.ebook.codex_page.extract') as extract_mock:
            panel.open_image()
            extract_mock.assert_not_called()
        assert panel.image.size == (123, 87)
        assert (panel.folder / 'original.png').exists()
        assert not (directory / 'images').exists()
        previous = panel.folder
        with patch('ui.ebook.codex_page.filedialog.askopenfilename', return_value=''):
            panel.open_image()
        assert panel.folder == previous
        with patch('ui.ebook.codex_page.filedialog.askopenfilename', return_value=str(tmp_path / 'missing.png')), pytest.raises(OSError):
            panel.open_image()
        assert panel.folder == previous
    finally:
        panel.shutdown()
        root.destroy()


def test_large_preview_zoom_and_shared_editors(tmp_path):
    root = tk.Tk()
    root.geometry('850x1050')
    panel = CodexExtractPage(root, app_directory=tmp_path)
    panel.pack(fill='both', expand=True)
    try:
        panel.image = Image.new('RGB', (1920, 1200), 'white')
        panel.fill({'text': '內文'})
        panel.draw()
        root.update()
        assert [panel.sections.tab(t, 'text') for t in panel.sections.tabs()] == ['圖片預覽', '校訂與匯出']
        assert panel.full_preview.canvas.winfo_height() > 600
        panel.full_preview.zoom.set('150%')
        panel.full_preview.render()
        assert panel.full_preview.photo.width() <= 1152
        assert panel.full_preview.photo.height() <= 720
        panel.preview_side.set('右頁')
        panel.draw()
        assert panel.full_preview.source.size == (960, 1200)
        panel.sections.select(1)
        root.update()
        assert panel.edit_preview.photo.height() == 720
        assert abs(panel.splitter.sashpos(0) - panel.edit_preview.photo.height()) < 40
        panel.preview_side.set('左頁')
        panel.draw()
        root.update()
        assert panel.edit_preview.source_key[1] == '左頁'
        panel.editors['text'].insert('end', '校訂')
        panel.sections.select(0)
        panel.sections.select(1)
        root.update()
        assert panel.values()['text'] == '內文校訂'
        assert panel.image.size == (1920, 1200)
        root.geometry('650x1050')
        panel.preview_side.set('雙頁')
        panel.draw()
        root.update()
        assert panel.edit_preview.photo.width() <= panel.edit_preview.canvas.winfo_width()
        assert panel.edit_preview.photo.height() < 720
        assert panel.tabs.winfo_height() > 120
    finally:
        panel.shutdown()
        root.destroy()


def test_ui_selected_text_cannot_duplicate_pages_across_chapters(tmp_path):
    work, directory = make_work(tmp_path)
    root = tk.Tk()
    root.withdraw()
    panel = CodexExtractPage(root, app_directory=tmp_path)
    try:
        panel.bind_work(work, directory)
        panel.accept_image(Image.new('RGB', (40, 20), 'white'))
        panel.fill({'text': 'ABCDEF'})
        panel.tabs.select(0)
        for number, start, end in ((0, '1.0', '1.2'), (1, '1.2', '1.4')):
            editor = panel.editors['text']
            editor.tag_remove('sel', '1.0', 'end')
            editor.tag_add('sel', start, end)
            with patch('ui.ebook.codex_page.PageRangeDialog') as dialog, \
                    patch('ui.ebook.codex_page.simpledialog.askinteger', return_value=number), \
                    patch('ui.ebook.codex_page.messagebox.askyesno', return_value=True):
                dialog.return_value.result = (10, 11)
                if number == 1:
                    with pytest.raises(ValueError, match='已屬於第 000 章'):
                        panel.export(selection=True)
                    continue
                panel.export(selection=True)
                # Same Tk/background queue contract, without invoking an LLM.
                import time
                deadline = time.monotonic() + 5
                while panel.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                assert not panel.busy
                assert panel.status.get() == '匯出完成：000.txt'
        assert (directory / 'originals/000.txt').read_text() == 'AB\n'
        assert not (directory / 'originals/001.txt').exists()
        assert len(list((directory / 'images').glob('*.png'))) == 1
    finally:
        panel.shutdown()
        root.destroy()
