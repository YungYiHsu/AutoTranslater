import threading
import tkinter as tk
from unittest.mock import patch

import pytest

from core.ebook.codex_extract import extract
from core.ebook.usage import history
from core.shared.config import AppConfig
from core.shared.prompt_templates import PromptTemplateStore
from tests.ebook.test_codex_extract import Client
from tests.ebook.test_ebook_exports import make_work
from tests.ebook.ui_helpers import wait_idle
from ui.ebook.codex_page import CodexExtractPage
from ui.ebook.ocr_page import EbookOcrPanel


def test_custom_prompt_and_usage(tmp_path):
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    store.save('ebook_extract', '自訂提取規則')
    assert store.read('ebook_extract').strip() == '自訂提取規則'
    extract(tmp_path, AppConfig(), threading.Event(), client_factory=Client,
            prompt=store.read('ebook_extract'), usage_directory=tmp_path)
    assert (tmp_path / 'prompt.txt').read_text(encoding='utf-8').startswith('自訂提取規則')
    rows = history(tmp_path)
    assert len(rows) == 1
    assert rows[0]['status'] == 'completed'
    assert rows[0]['elapsed_seconds'] >= 0
    assert rows[0]['model'] == 'chosen-model'
    assert rows[0]['effort'] == 'low'


def test_failure_usage_preserved(tmp_path):
    with patch.object(Client, 'wait_turn', side_effect=ValueError('failed')), pytest.raises(ValueError):
        extract(tmp_path, AppConfig(), threading.Event(), client_factory=Client)
    assert history(tmp_path)[0]['status'] == 'failed'


def test_ocr_restore_skips_invalid_latest(tmp_path):
    work, directory = make_work(tmp_path)
    for name in ('001', '002'):
        folder = directory / 'ocr' / name
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text('{}')
    root = tk.Tk()
    root.withdraw()
    panel = EbookOcrPanel(root, require_work=True)
    result = (directory / 'ocr/001', {}, None)
    try:
        with patch('ui.ebook.ocr_page.load_session', side_effect=[ValueError('bad'), result]), \
                patch.object(panel, '_record_opened') as opened:
            panel.bind_work(work, directory)
            wait_idle(panel)
            opened.assert_called_once_with(result)
    finally:
        panel.shutdown()
        root.destroy()


def test_model_choices_persist_and_filter_efforts(tmp_path):
    root = tk.Tk()
    root.withdraw()
    panel = CodexExtractPage(root, app_directory=tmp_path)
    try:
        panel.models_loaded({'models': [{'model': 'test-model',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]}]})
        panel.model.set('test-model')
        panel.model_changed()
        assert tuple(panel.effort_combo['values']) == ('low', 'high')
        assert panel.effort.get() == 'low'
        panel.effort.set('high')
        panel.save_model()
        panel.set_busy(True)
        assert panel.model_combo.instate(['disabled'])
        panel.set_busy(False)
        assert panel.model_combo.instate(['readonly'])
        other = CodexExtractPage(root, app_directory=tmp_path)
        try:
            assert other.model.get() == 'test-model'
            assert other.effort.get() == 'high'
        finally:
            other.shutdown()
            other.destroy()
    finally:
        panel.shutdown()
        root.destroy()


def test_new_default_preserves_custom_prompt(tmp_path):
    store = PromptTemplateStore(tmp_path)
    store.ensure()
    assert '小字假名' in store.read('ebook_extract')
    assert '肉眼無法區分的 Unicode 字碼差異不作要求' not in store.read('ebook_extract')
    store.save('ebook_extract', '我的自訂規則')
    store.ensure()
    assert store.read('ebook_extract').strip() == '我的自訂規則'


def test_model_parity_memory_invalid_and_autoload(tmp_path):
    root = tk.Tk()
    root.withdraw()
    panel = CodexExtractPage(root, app_directory=tmp_path)
    catalog = [{'model': m, 'supportedReasoningEfforts': [
        {'reasoningEffort': 'high'}, {'reasoningEffort': 'low'}]} for m in ('a', 'b')]
    try:
        with pytest.raises(ValueError):
            panel.validate_model()
        assert panel.effort_combo.instate(['disabled'])
        with patch.object(panel, 'background') as launch:
            panel.auto_load_models()
            panel.auto_load_models()
            assert launch.call_count == 1
        panel.models_loaded({'models': catalog})
        assert panel.model.get() == 'a'
        assert panel.effort.get() == 'low'
        assert tuple(panel.model_combo['values']) == ('a', 'b')
        panel.effort.set('high')
        panel.save_model()
        panel.model.set('b')
        panel.model_changed()
        assert panel.effort.get() == 'low'
        panel.model.set('a')
        panel.model_changed()
        assert panel.effort.get() == 'high'
        panel.models_loaded({'models': catalog[1:]})
        assert panel.model.get() == 'b'
        panel.models_loaded({'models': []})
        with pytest.raises(ValueError):
            panel.validate_model()
        assert panel.effort_combo.instate(['disabled'])
    finally:
        panel.shutdown()
        root.destroy()
