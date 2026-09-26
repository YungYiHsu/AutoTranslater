"""Validate TXT authority, EPUB structure and safe replacement."""

from dataclasses import replace
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

import pytest

from core.chapter_progress import ChapterCompletionTracker
from core.epub_export import export_epub, local_chapter_numbers
from core.models import NovelChapterEntry
from core.work_setup import WorkSetupService
from formatters.utils import TXT_BODY_SEPARATOR
from tests.fake_work import FakeWorkTranslator
from tests.test_work_setup import make_work


@pytest.fixture
def setup(tmp_path):
    work = make_work(title="作品 & 測試")
    work = replace(work, chapters=tuple(
        NovelChapterEntry(i, f"日文{i}", f"{work.source_url}{i}/") for i in range(1, 4)
    ))
    result = WorkSetupService(tmp_path, FakeWorkTranslator()).prepare(work)
    for entry in work.chapters:
        path = ChapterCompletionTracker.output_paths(result, entry)[0]
        path.write_text(
            f"作品：手動作品 & 名稱\n章節：手動標題 {entry.number} <測試>\n"
            f"\n{TXT_BODY_SEPARATOR}\n\n手動正文 {entry.number} & <內容>\n換行\n\n第二段 😀",
            encoding="utf-8-sig",
        )
    return result


def test_structure_and_txt_authority(setup):
    original = {p: p.read_bytes() for p in setup.work_directory.iterdir() if p.is_file()}
    with ZipFile(export_epub(setup, 1, 3)) as book:
        assert book.namelist()[0] == "mimetype"
        assert book.getinfo("mimetype").compress_type == ZIP_STORED
        assert book.read("mimetype") == b"application/epub+zip"
        assert book.testzip() is None
        for name in book.namelist():
            if name.endswith((".xml", ".xhtml", ".opf", ".ncx")):
                ET.fromstring(book.read(name))
        ns = {"o": "http://www.idpf.org/2007/opf", "d": "http://purl.org/dc/elements/1.1/"}
        package = ET.fromstring(book.read("EPUB/package.opf"))
        assert package.find("o:metadata/d:language", ns).text == "zh-Hant"
        assert "手動作品 & 名稱" in package.find("o:metadata/d:title", ns).text
        for item in package.findall("o:manifest/o:item", ns):
            assert "EPUB/" + item.attrib["href"] in book.namelist()
        assert [i.attrib["idref"] for i in package.findall("o:spine/o:itemref", ns)] == ["ch1", "ch2", "ch3"]
        content = "".join(ET.fromstring(book.read("EPUB/chapter-2.xhtml")).itertext())
        assert "手動正文 2 & <內容>" in content
        assert "手動標題 2 <測試>" in content
        nav = ET.fromstring(book.read("EPUB/nav.xhtml"))
        links = nav.findall(".//{http://www.w3.org/1999/xhtml}a")
        assert [a.attrib["href"] for a in links] == [f"chapter-{i}.xhtml" for i in range(1, 4)]
    assert all(p.read_bytes() == data for p, data in original.items())


def test_local_bounds_and_gaps(setup):
    entries = setup.work.source_work.chapters
    assert local_chapter_numbers(setup) == (1, 2, 3)
    ChapterCompletionTracker.output_paths(setup, entries[1])[0].unlink()
    assert local_chapter_numbers(setup) == (1, 3)
    with pytest.raises(ValueError, match="第 2 章"):
        export_epub(setup, 1, 3)
    for entry in (entries[0], entries[2]):
        ChapterCompletionTracker.output_paths(setup, entry)[0].unlink()
    assert local_chapter_numbers(setup) == ()


def test_real_progress_and_final_save(setup):
    updates = []

    def progress(done, total):
        updates.append((done, total))
        if done == total:
            assert list(setup.work_directory.glob("*.epub"))

    export_epub(setup, 1, 3, on_progress=progress)
    assert updates == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]


@pytest.mark.parametrize(("start", "end"), [(0, 1), (3, 2), (1, 4), (True, 2)])
def test_invalid_range(setup, start, end):
    with pytest.raises(ValueError):
        export_epub(setup, start, end)
    assert not list(setup.work_directory.glob("*.epub"))


@pytest.mark.parametrize("problem", ["missing", "empty", "separator", "control"])
def test_invalid_txt_preserves_existing_book(setup, problem):
    result = export_epub(setup, 1, 3)
    previous = result.read_bytes()
    path = ChapterCompletionTracker.output_paths(setup, setup.work.source_work.chapters[1])[0]
    if problem == "missing":
        path.unlink()
    elif problem == "empty":
        path.write_text(f"作品：作品\n章節：第二章\n\n{TXT_BODY_SEPARATOR}\n\n", encoding="utf-8")
    elif problem == "separator":
        path.write_text("格式錯誤", encoding="utf-8")
    else:
        path.write_text(path.read_text(encoding="utf-8-sig") + "\x00", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 章"):
        export_epub(setup, 1, 3, overwrite=True)
    assert result.read_bytes() == previous
    assert not list(setup.work_directory.glob(".epub-*.tmp"))


def test_overwrite_and_updated_text(setup):
    result = export_epub(setup, 2, 2)
    with pytest.raises(FileExistsError):
        export_epub(setup, 2, 2)
    txt = ChapterCompletionTracker.output_paths(setup, setup.work.source_work.chapters[1])[0]
    txt.write_text(txt.read_text(encoding="utf-8-sig") + "\n最新修訂", encoding="utf-8")
    assert export_epub(setup, 2, 2, overwrite=True) == result
    with ZipFile(result) as book:
        assert "最新修訂" in book.read("EPUB/chapter-2.xhtml").decode()
        assert "EPUB/chapter-1.xhtml" not in book.namelist()


def test_old_filename_is_not_used_as_epub_input(setup):
    entry = setup.work.source_work.chapters[0]
    current, html = ChapterCompletionTracker.output_paths(setup, entry)
    old_path = current.parent / f"{setup.work.source_work.title} - 1.txt"
    current.rename(old_path)
    original = old_path.read_bytes()
    html.write_text("HTML 不應作為正文來源", encoding="utf-8")
    assert local_chapter_numbers(setup) == (2, 3)
    with pytest.raises(ValueError, match="缺少本地 TXT"):
        export_epub(setup, 1, 1)
    assert old_path.read_bytes() == original


def test_kakuyomu_display_numbers(setup):
    source = replace(
        setup.work.source_work, ncode="12345678901234567890",
        source_url="https://kakuyomu.jp/works/12345678901234567890",
        chapters=tuple(NovelChapterEntry(
            i, f"日文{i}", f"https://kakuyomu.jp/works/12345678901234567890/episodes/{10000000000000000000+i}"
        ) for i in range(1, 4)),
    )
    setup = replace(setup, work=replace(setup.work, source_work=source))
    assert export_epub(setup, 1, 2).exists()


def test_failed_write_preserves_existing_book(setup, monkeypatch):
    path = export_epub(setup, 1, 1)
    previous = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("磁碟寫入失敗")

    monkeypatch.setattr(ZipFile, "writestr", fail)
    with pytest.raises(OSError):
        export_epub(setup, 1, 1, overwrite=True)
    assert path.read_bytes() == previous
    assert not list(setup.work_directory.glob(".epub-*.tmp"))
