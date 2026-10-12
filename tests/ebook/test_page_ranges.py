import json

import pytest

from core.ebook.chapters import chapter_text, commit_export, covers, prepare_export
from tests.ebook.test_ebook_exports import make_work, sample_record


def codex(directory, start=10, end=11):
    return {'engine': 'codex', 'text': 'AB', 'work_capture': {
        'work_id': directory.name, 'right': start, 'left': end}}


@pytest.mark.parametrize('engine,start,end', [('codex', 10, 11), ('codex', 11, 12), ('ocr', 10, 11)])
def test_pages_unique_across_chapters(tmp_path, engine, start, end):
    _, directory = make_work(tmp_path)
    commit_export(prepare_export(directory, codex(directory), 'old', 1))
    before = (directory / 'chapters.json').read_bytes()
    data = codex(directory, start, end) if engine == 'codex' else sample_record(directory)
    with pytest.raises(ValueError, match='已屬於第 001 章'):
        prepare_export(directory, data, 'new', 2, 1, 2)
    assert (directory / 'chapters.json').read_bytes() == before
    assert not (directory / 'originals/002.txt').exists()
    commit_export(prepare_export(directory, codex(directory, 12, 13), 'next', 2))


@pytest.mark.parametrize('first,last', [(1, 1), (2, 2)])
def test_single_page_cannot_replace_spread(tmp_path, first, last):
    _, directory = make_work(tmp_path)
    commit_export(prepare_export(directory, codex(directory), 'codex', 0))
    before = (directory / 'chapters.json').read_bytes()
    with pytest.raises(ValueError, match='完整範圍'):
        prepare_export(directory, sample_record(directory), 'ocr', 0, first, last)
    assert (directory / 'chapters.json').read_bytes() == before
    assert (directory / 'originals/000.txt').read_text() == 'AB\n'


def test_whole_spread_replacement_both_directions(tmp_path):
    _, directory = make_work(tmp_path)
    commit_export(prepare_export(directory, codex(directory), 'codex', 0))
    plan = prepare_export(directory, sample_record(directory), 'ocr', 0, 1, 2)
    assert plan.conflict
    with pytest.raises(FileExistsError):
        commit_export(plan)
    commit_export(plan, replace=True)
    pages = json.loads((directory / 'chapters.json').read_text())['0']['pages']
    assert [(p['start_page'], p['end_page']) for p in pages] == [(10, 10), (11, 11)]
    plan = prepare_export(directory, codex(directory), 'codex2', 0)
    assert plan.conflict
    commit_export(plan, replace=True)
    assert len(plan.chapters['0']['pages']) == 1


def test_partial_columns_and_shifted_ranges_rejected(tmp_path):
    _, directory = make_work(tmp_path)
    commit_export(prepare_export(directory, codex(directory), 'codex', 0))
    data = sample_record(directory)
    data['columns'].append({'id': 'left_02_01', 'status': 'done', 'raw': 'C'})
    with pytest.raises(ValueError, match='全部文字'):
        prepare_export(directory, data, 'ocr', 0, 1, 2)
    with pytest.raises(ValueError, match='完整範圍'):
        prepare_export(directory, codex(directory, 11, 12), 'shift', 0)
    assert prepare_export(directory, codex(directory, 9, 12), 'whole', 0).conflict
    assert not covers(10, 12, [{'start_page': 10, 'end_page': 10}, {'start_page': 12, 'end_page': 12}])


def test_insert_sorted_and_legacy_backup(tmp_path):
    _, directory = make_work(tmp_path)
    legacy = {'0': {'rows': [
        {'page': 10, 'line': 'right_02_01', 'record': 'old', 'text': 'B'},
        {'page': 10, 'line': 'right_01_01', 'record': 'old', 'text': 'A'}]}}
    path = directory / 'chapters.json'
    path.write_text(json.dumps(legacy), encoding='utf-8')
    before = path.read_bytes()
    assert chapter_text(legacy['0']) == 'AB'
    plan = prepare_export(directory, codex(directory, 14, 15), 'later', 0)
    assert path.read_bytes() == before
    commit_export(plan)
    backup = next(directory.glob('chapters.legacy-*.json'))
    assert backup.read_bytes() == before
    plan = prepare_export(directory, codex(directory, 12, 13), 'middle', 0)
    commit_export(plan)
    assert [p['start_page'] for p in plan.chapters['0']['pages']] == [10, 12, 14]
    assert 'rows' not in plan.chapters['0']
