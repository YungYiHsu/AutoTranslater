import json
import tkinter as tk
from unittest.mock import patch

import pytest
from PIL import Image

from core.ebook.chapters import export_chapter, save_translation
from core.ebook.exports import has_page_images, save_page_image, suggested_pages
from core.ebook.ocr.session import clear_sessions, save_session
from tests.ebook.test_ebook_exports import make_work, sample_record
from tests.ebook.ui_helpers import empty_detection, wait_idle
from ui.ebook.ocr_page import EbookOcrPanel


@pytest.fixture
def panel(tmp_path):
    work, directory = make_work(tmp_path)
    root = tk.Tk()
    root.withdraw()
    panel = EbookOcrPanel(root, require_work=True)
    panel.bind_work(work, directory)
    with patch('ui.ebook.ocr_page.detect_lines', side_effect=empty_detection):
        panel.accept_image(Image.new('RGB', (1920, 1200), 'white'), 'test')
        wait_idle(panel)
    panel.data['columns'] = sample_record(directory)['columns']
    save_session(panel.folder, panel.data)
    yield panel
    panel.shutdown()
    root.destroy()


def export(panel, numbers=(1, 2, 0), span='1,2'):
    pages = tuple(numbers[:2]) if len(numbers) >= 2 and None not in numbers[:2] else None
    with patch('ui.ebook.ocr_export.PageRangeDialog') as dialog, \
            patch('ui.ebook.ocr_page.simpledialog.askinteger', return_value=numbers[2] if len(numbers) > 2 else None), \
            patch('ui.ebook.ocr_page.simpledialog.askstring', return_value=span) as ask:
        dialog.return_value.result = pages
        panel.export_original()
        wait_idle(panel)
        return ask


def test_deferred_export_reuse_and_clear(panel):
    directory = panel.work_directory
    assert (panel.folder / 'original.png').exists()
    assert not (directory / 'images').exists()
    assert panel.page_suggestion.get() == '沒有'
    ask = export(panel)
    assert ask.call_count == 1  # Range only, no chapter-title question.
    assert (directory / 'images/p001-p002.png').exists()
    assert (directory / 'originals/000.txt').read_text(encoding='utf-8') == 'aB\n'
    assert panel.status.get() == '匯出完成：000.txt'
    assert panel.page_suggestion.get() == '003–004'
    with pytest.raises(ValueError, match='已屬於第 000 章'):
        export(panel, (1, 2, 1), '2,2')
    assert len(list((directory / 'images').glob('*.png'))) == 1
    save_translation(directory, 0, 'translation')
    panel.set_busy(True)
    with patch('ui.ebook.ocr_page.messagebox.askyesno', return_value=True) as confirm:
        panel.clear_all_records()
        confirm.assert_not_called()
        assert panel.buttons['清空 OCR 紀錄'].instate(['disabled'])
        panel.set_busy(False)
        panel.clear_all_records()
        wait_idle(panel)
    assert panel.image is None and panel.data is None
    assert not list((directory / 'ocr').iterdir())
    assert (directory / 'images/p001-p002.png').exists()
    assert (directory / 'originals/000.txt').exists()
    assert (directory / 'translations/000.txt').exists()
    assert panel.page_suggestion.get() == '003–004'


@pytest.mark.parametrize('numbers,span', [((None,), '1,2'), ((1,None), '1,2'),
                                        ((1,2,None), '1,2'), ((1,2,0), None)])
def test_cancel_writes_no_formal_outputs(panel, numbers, span):
    export(panel, numbers, span)
    assert not (panel.work_directory / 'images').exists()
    assert not (panel.work_directory / 'chapters.json').exists()


def test_incomplete_creates_no_image(panel):
    panel.data['columns'][0]['status'] = 'pending'
    with pytest.raises(ValueError):
        export(panel)
    assert not (panel.work_directory / 'images').exists()


def test_zero_single_page(panel):
    export(panel, (0,0,0))
    assert (panel.work_directory / 'images/p000.png').exists()
    assert panel.page_suggestion.get() == '001–002'


def test_suggestions_maximum_versions_and_gaps(tmp_path):
    assert not has_page_images(tmp_path)
    assert suggested_pages(tmp_path) == (1,2)
    image = Image.new('RGB', (10,10))
    save_page_image(tmp_path, image, 10, 11)
    save_page_image(tmp_path, image, 0, 0)
    save_page_image(tmp_path, image, 10, 11, overwrite=True)
    assert suggested_pages(tmp_path) == (12,13)
    save_page_image(tmp_path, image, 999,1000)
    assert suggested_pages(tmp_path) == (1001,1002)


def test_legacy_title_files_preserved(tmp_path):
    _, directory = make_work(tmp_path)
    data = sample_record(directory)
    export_chapter(directory, data, 'test', 0, 1, 2)
    chapters_path = directory / 'chapters.json'
    chapters = json.loads(chapters_path.read_text(encoding='utf-8'))
    chapters['0']['filename'] = '000_old.txt'
    chapters['0']['title'] = 'old'
    chapters_path.write_text(json.dumps(chapters), encoding='utf-8')
    (directory / 'originals/000.txt').rename(directory / 'originals/000_old.txt')
    save_translation(directory, 0, 'old translation')
    export_chapter(directory, data, 'test', 0, 1, 2, replace=True)
    assert (directory / 'translations/000.txt').read_text(encoding='utf-8') == 'old translation\n'
    assert (directory / 'translations/000_old.txt').exists()
    assert (directory / 'originals/000_old.txt').exists()


def test_clear_rejects_foreign_directory(tmp_path):
    with pytest.raises(ValueError):
        clear_sessions(tmp_path)


def test_empty_work_prefills_without_chapter_name(panel):
    with patch('ui.ebook.ocr_export.PageRangeDialog') as dialog, \
            patch('ui.ebook.ocr_page.simpledialog.askinteger', return_value=None) as ask:
        dialog.return_value.result = (1, 2)
        panel.export_original()
        wait_idle(panel)
    dialog.assert_called_once_with(panel.root, 1, None)
    assert ask.call_args.kwargs['initialvalue'] == 0
    assert ask.call_args.args[1] == '章節編號'


def test_chapter_prefill_is_numeric_maximum(panel):
    (panel.work_directory / 'chapters.json').write_text(
        json.dumps({'2': {}, '10': {}, '9': {}}), encoding='utf-8'
    )
    with patch('ui.ebook.ocr_export.PageRangeDialog') as dialog, \
            patch('ui.ebook.ocr_page.simpledialog.askinteger', return_value=None) as ask:
        dialog.return_value.result = (1, 2)
        panel.export_original()
    assert ask.call_args.kwargs['initialvalue'] == 10


def test_page_dialog_defaults_edit_and_validation(panel):
    from ui.ebook.ocr_export import PageRangeDialog

    with patch.object(PageRangeDialog, 'wait_window'):
        dialog = PageRangeDialog(panel.root, 5)
    try:
        assert dialog.head.get() == '5'
        assert dialog.tail.get() == '6'
        dialog.head.set('0')
        assert dialog.tail.get() == '1'
        dialog.tail.set('0')
        assert dialog.validate()
        dialog.apply()
        assert dialog.result == (0, 0)
        with patch('ui.ebook.ocr_export.messagebox.showerror'):
            dialog.tail.set('-1')
            assert not dialog.validate()
            dialog.head.set('abc')
            assert not dialog.validate()
    finally:
        dialog.cancel()


def test_page_dialog_preserves_existing_single_page(panel):
    from ui.ebook.ocr_export import PageRangeDialog

    with patch.object(PageRangeDialog, 'wait_window'):
        dialog = PageRangeDialog(panel.root, 5, 5)
    try:
        assert dialog.tail.get() == '5'
    finally:
        dialog.cancel()


def test_duplicate_image_cancel_preserves_text(panel):
    directory = panel.work_directory
    save_page_image(directory, Image.new('RGB',(10,10)), 1,2)
    with patch('ui.ebook.ocr_page.messagebox.askyesno', return_value=False):
        export(panel)
    assert not (directory / 'chapters.json').exists()
    assert len(list((directory / 'images').glob('*.png'))) == 1


def test_chapter_failure_rolls_back_image_and_text(panel):
    directory = panel.work_directory
    with (patch('core.ebook.chapters.write_json', side_effect=OSError('disk failure')),
          pytest.raises(OSError)):
        export(panel)
    assert not (directory / 'chapters.json').exists()
    assert not (directory / 'originals/000.txt').exists()
    assert not list((directory / 'images').glob('*.png'))
    assert panel.page_suggestion.get() == '沒有'


def test_clear_cancel_keeps_ocr(panel):
    folder = panel.folder
    with patch('ui.ebook.ocr_page.messagebox.askyesno', return_value=False):
        panel.clear_all_records()
    assert (folder / 'original.png').exists()
    assert panel.image is not None


def test_image_write_failure_leaves_no_partial_file(tmp_path):
    image = Image.new('RGB', (10, 10))
    saved = save_page_image(tmp_path, image, 1, 2)
    before = saved.read_bytes()
    with (patch('PIL.Image.Image.save', side_effect=OSError('write failed')),
          pytest.raises(OSError)):
        save_page_image(tmp_path, image, 1, 2, overwrite=True)
    assert saved.read_bytes() == before
    assert not (tmp_path / 'images/p001-p002_v02.png').exists()


def test_overwrite_failure_restores_existing_image(panel):
    directory = panel.work_directory
    saved = save_page_image(directory, Image.new('RGB', (10, 10), 'black'), 1, 2)
    before = saved.read_bytes()
    with (patch('ui.ebook.ocr_export.messagebox.askyesno', return_value=True),
          patch('core.ebook.chapters.write_json', side_effect=OSError('disk failure')),
          pytest.raises(OSError)):
        export(panel)
    assert saved.read_bytes() == before
    assert not (directory / 'chapters.json').exists()
    assert list((directory / 'images').iterdir()) == [saved]


def test_overwrite_confirmed_uses_exact_filename(panel):
    directory = panel.work_directory
    saved = save_page_image(directory, Image.new('RGB', (10, 10), 'black'), 1, 2)
    with patch('ui.ebook.ocr_export.messagebox.askyesno', return_value=True) as confirm:
        export(panel)
        assert confirm.call_args.args[0] == '覆蓋圖片'
    with Image.open(saved) as image:
        assert image.size == panel.image.size
    assert list((directory / 'images').glob('*.png')) == [saved]


def test_stale_image_confirmation_rejected(tmp_path):
    from core.ebook.exports import export_image
    image = Image.new('RGB', (10, 10), 'black')
    saved = save_page_image(tmp_path, image, 1, 2)
    before = saved.read_bytes()
    save_page_image(tmp_path, Image.new('RGB', (12, 12)), 1, 2, overwrite=True)
    current = saved.read_bytes()
    with pytest.raises(ValueError, match='圖片已變更'), export_image(tmp_path, image, 1, 2, before):
        pytest.fail('must reject stale approval')
    assert saved.read_bytes() == current
