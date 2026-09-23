"""Create self-contained EPUB 3 books from authoritative local chapter TXT files."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from core.chapter_progress import ChapterCompletionTracker
from core.models import NovelChapterEntry
from core.work_setup import WorkSetupResult
from formatters.utils import TXT_BODY_SEPARATOR, sanitize_filename_component

SEND_TO_KINDLE_URL = "https://www.amazon.com/sendtokindle"
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_CSS = "body { line-height: 1.7; } p { margin: 0 0 1em; } h1 { font-size: 1.5em; }"


@dataclass(frozen=True)
class EpubChapter:
    number: int
    title: str
    body: str
    work_title: str


def chapter_txt_path(setup: WorkSetupResult, entry: NovelChapterEntry) -> Path:
    current = ChapterCompletionTracker.output_paths(setup, entry)[0]
    if current.is_file():
        return current
    title = sanitize_filename_component(setup.work.source_work.title, max_length=80)
    legacy = setup.work_directory / f"{title} - {entry.number}.txt"
    return legacy if legacy.is_file() else current


def epub_destination(setup: WorkSetupResult, start: int, end: int) -> Path:
    title = sanitize_filename_component(setup.work.translated_title, max_length=60)
    return setup.work_directory / f"{start:04d}-{end:04d} - {title}.epub"


def local_chapter_numbers(setup: WorkSetupResult) -> tuple[int, ...]:
    """Find local chapter TXT files, including legacy filenames, without reading bodies."""
    return tuple(sorted(entry.number for entry in setup.work.source_work.chapters
                        if chapter_txt_path(setup, entry).is_file()))


def _read_chapter(path: Path, number: int) -> EpubChapter:
    text = path.read_text(encoding="utf-8-sig")
    if _INVALID_XML.search(text):
        raise ValueError("TXT 含 EPUB 不支援的控制字元")
    header, separator, body = text.partition(f"\n{TXT_BODY_SEPARATOR}\n\n")
    if not separator:
        raise ValueError("TXT 缺少正文分隔線")
    if not body.strip():
        raise ValueError("TXT 正文空白，請先補上譯文")
    metadata = dict(line.split("：", 1) for line in header.splitlines() if "：" in line)
    if not metadata.get("章節", "").strip() or not metadata.get("作品", "").strip():
        raise ValueError("TXT 缺少作品或章節名稱")
    return EpubChapter(number, metadata["章節"].strip(), body, metadata["作品"].strip())


def load_epub_chapters(setup: WorkSetupResult, start: int, end: int) -> tuple[EpubChapter, ...]:
    work = setup.work.source_work
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ValueError("請輸入有效章節範圍，起始章節須小於或等於結束章節。")
    if start < work.chapters[0].number or end > work.chapters[-1].number:
        raise ValueError(f"章節範圍須介於 {work.chapters[0].number}～{work.chapters[-1].number}。")
    entries = {entry.number: entry for entry in work.chapters if start <= entry.number <= end}
    problems: list[str] = []
    chapters: list[EpubChapter] = []
    for number in range(start, end + 1):
        if number not in entries:
            problems.append(f"第 {number} 章：作品目錄中不存在")
        else:
            try:
                chapters.append(_read_chapter(chapter_txt_path(setup, entries[number]), number))
            except (OSError, UnicodeError, ValueError) as exc:
                detail = "缺少本地 TXT" if isinstance(exc, FileNotFoundError) else str(exc)
                problems.append(f"第 {number} 章：{detail}")
        if len(problems) >= 20:
            problems.append("問題較多，請先修正以上章節，再重新匯出。")
            break
    if problems:
        raise ValueError("無法匯出 EPUB：\n" + "\n".join(problems))
    return tuple(chapters)


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-Hant" lang="zh-Hant">'
        f'<head><title>{escape(title)}</title><link rel="stylesheet" href="style.css" '
        f'type="text/css"/></head><body>{body}</body></html>'
    )


def export_epub(
    setup: WorkSetupResult, start: int, end: int, *, overwrite: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Validate the entire range before atomically saving a single book."""
    chapters = load_epub_chapters(setup, start, end)
    total = len(chapters) + 1  # Final unit is ZIP finalization and atomic save.
    if on_progress:
        on_progress(0, total)
    destination = epub_destination(setup, start, end)
    if destination.exists() and not overwrite:
        raise FileExistsError("同名 EPUB 已存在，請確認是否覆蓋。")
    title = f"{chapters[0].work_title}（第 {start}～{end} 章）"
    author = setup.work.source_work.author
    if _INVALID_XML.search(author):
        raise ValueError("作者名稱含 EPUB 不支援的控制字元。")
    identifier = "urn:uuid:" + str(uuid5(NAMESPACE_URL, f"{setup.work.source_work.source_url}#{start}-{end}"))
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = []
    spine = []
    links = []
    navpoints = []
    for order, chapter in enumerate(chapters, 1):
        filename = f"chapter-{chapter.number}.xhtml"
        label = escape(f"第 {chapter.number} 章　{chapter.title}")
        manifest.append(f'<item id="ch{order}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="ch{order}"/>')
        links.append(f'<li><a href="{filename}">{label}</a></li>')
        navpoints.append(f'<navPoint id="ch{order}" playOrder="{order}"><navLabel><text>{label}'
                         f'</text></navLabel><content src="{filename}"/></navPoint>')
    package = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="book-id">{identifier}</dc:identifier><dc:title>{escape(title)}</dc:title>'
        f'<dc:language>zh-Hant</dc:language><dc:creator>{escape(author)}</dc:creator>'
        f'<meta property="dcterms:modified">{modified}</meta></metadata><manifest>'
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        '<item id="css" href="style.css" media-type="text/css"/>'
        + "".join(manifest) + '</manifest><spine toc="ncx">' + "".join(spine) + '</spine></package>'
    )
    ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<head><meta name="dtb:uid" content="{identifier}"/>'
        '<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>'
        '<meta name="dtb:maxPageNumber" content="0"/></head>'
        f'<docTitle><text>{escape(title)}</text></docTitle><navMap>'
        + "".join(navpoints) + '</navMap></ncx>'
    )
    fd, name = tempfile.mkstemp(prefix=".epub-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w+b") as stream:
            with ZipFile(stream, "w", compression=ZIP_DEFLATED) as book:
                book.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
                book.writestr("META-INF/container.xml", (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="EPUB/package.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles></container>'
                ))
                book.writestr("EPUB/package.opf", package)
                book.writestr("EPUB/toc.ncx", ncx)
                book.writestr("EPUB/style.css", _CSS)
                book.writestr("EPUB/nav.xhtml", _xhtml("目錄", (
                    '<nav epub:type="toc" id="toc"><h1>目錄</h1><ol>' + "".join(links) + '</ol></nav>'
                )))
                for completed, chapter in enumerate(chapters, 1):
                    paragraphs = re.split(r"\n\s*\n", chapter.body.strip())
                    body = "".join('<p>' + escape(p).replace("\n", "<br/>") + '</p>' for p in paragraphs)
                    heading = escape(f"第 {chapter.number} 章　{chapter.title}")
                    book.writestr(f"EPUB/chapter-{chapter.number}.xhtml",
                                  _xhtml(chapter.title, f'<h1>{heading}</h1>{body}'))
                    if on_progress:
                        on_progress(completed, total)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() and not overwrite:
            raise FileExistsError("同名 EPUB 已存在，請確認是否覆蓋。")
        temporary.replace(destination)
        if on_progress:
            on_progress(total, total)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
