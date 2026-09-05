"""Offline tests for Naro work metadata and paginated chapter indexes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from core.exceptions import (
    ChapterNotFoundError,
    ExtractorError,
    UnsupportedUrlError,
    WorkNotFoundError,
)
from core.models import NovelChapterEntry, NovelWork
from extractors.web_syosetu_work import SyosetuWorkExtractor

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WORK_URL = "https://ncode.syosetu.com/n1234ab/"
CHAPTER_URL = f"{WORK_URL}1/"


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def make_response(
    url: str,
    html: str,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=UTF-8",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = content_type
    response._content = html.encode("utf-8")
    response.encoding = "utf-8"
    return response


class StubSession:
    def __init__(self, results: list[requests.Response | requests.RequestException]) -> None:
        self.headers: dict[str, str] = {}
        self._results: Iterator[requests.Response | requests.RequestException] = iter(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((url, kwargs))
        result = next(self._results)
        if isinstance(result, requests.RequestException):
            raise result
        return result


def make_extractor(
    *results: requests.Response | requests.RequestException,
    max_index_pages: int = 100,
) -> tuple[SyosetuWorkExtractor, StubSession]:
    session = StubSession(list(results))
    extractor = SyosetuWorkExtractor(
        session=cast(requests.Session, session),
        timeout=7.5,
        max_index_pages=max_index_pages,
    )
    return extractor, session


@pytest.mark.parametrize(
    "url",
    [
        WORK_URL,
        "http://ncode.syosetu.com/n1234ab",
        "https://ncode.syosetu.com/N1234AB/?from=test#top",
    ],
)
def test_can_handle_supported_work_urls(url: str) -> None:
    assert SyosetuWorkExtractor().can_handle(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        CHAPTER_URL,
        "ftp://ncode.syosetu.com/n1234ab/",
        "https://example.com/n1234ab/",
        "https://user@ncode.syosetu.com/n1234ab/",
        "https://ncode.syosetu.com:444/n1234ab/",
        "https://ncode.syosetu.com/n1234ab/info/",
    ],
)
def test_can_handle_rejects_non_work_urls(url: str) -> None:
    assert not SyosetuWorkExtractor().can_handle(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WORK_URL, (WORK_URL, None)),
        ("https://ncode.syosetu.com/N1234AB", (WORK_URL, None)),
        (CHAPTER_URL, (WORK_URL, 1)),
        ("http://ncode.syosetu.com/N1234AB/25/?x=1#top", ("http://ncode.syosetu.com/n1234ab/", 25)),
    ],
)
def test_normalize_selection_url_accepts_work_and_chapter_urls(
    url: str, expected: tuple[str, int | None]
) -> None:
    assert SyosetuWorkExtractor.normalize_selection_url(url) == expected


def test_normalize_selection_url_rejects_unsupported_urls() -> None:
    with pytest.raises(UnsupportedUrlError, match="作品首頁或章節網址"):
        SyosetuWorkExtractor.normalize_selection_url("https://example.com/n1234ab/1/")


def test_extract_reads_metadata_and_all_paginated_chapters() -> None:
    first = make_response(WORK_URL, fixture_text("syosetu_work_page_1.html"))
    second_url = f"{WORK_URL}?p=2"
    second = make_response(second_url, fixture_text("syosetu_work_page_2.html"))
    extractor, session = make_extractor(first, second)

    work = extractor.extract("https://ncode.syosetu.com/N1234AB?ignored=yes")

    assert work.ncode == "n1234ab"
    assert work.source_url == WORK_URL
    assert work.title == "測試作品"
    assert work.author == "測試作者"
    assert work.synopsis == "第一段摘要。\n第二段摘要。"
    assert [(chapter.number, chapter.title) for chapter in work.chapters] == [
        (1, "第一章"),
        (3, "第三章"),
        (5, "第五章"),
    ]
    assert work.get_chapter(5).source_url == f"{WORK_URL}5/"
    with pytest.raises(ChapterNotFoundError, match="第 2 章"):
        work.get_chapter(2)
    assert [call[0] for call in session.calls] == [WORK_URL, second_url]


def test_cached_extract_checks_only_home_and_cached_tail(tmp_path: Path) -> None:
    first_html = fixture_text("syosetu_work_page_1.html")
    second_url = f"{WORK_URL}?p=2"
    second_html = fixture_text("syosetu_work_page_2.html")
    first_extractor, _ = make_extractor(
        make_response(WORK_URL, first_html), make_response(second_url, second_html)
    )
    first_extractor._catalog_root = tmp_path
    first_extractor.extract(WORK_URL)

    cached_extractor, session = make_extractor(
        make_response(WORK_URL, first_html), make_response(second_url, second_html)
    )
    cached_extractor._catalog_root = tmp_path
    work = cached_extractor.extract(WORK_URL)

    assert len(work.chapters) == 3
    assert [call[0] for call in session.calls] == [WORK_URL, second_url]
    assert (tmp_path / "測試作品" / "chapters.json").exists()


def test_force_refresh_ignores_existing_catalog(tmp_path: Path) -> None:
    first_html = fixture_text("syosetu_work_page_1.html")
    second_url = f"{WORK_URL}?p=2"
    second_html = fixture_text("syosetu_work_page_2.html")
    seed, _ = make_extractor(
        make_response(WORK_URL, first_html), make_response(second_url, second_html)
    )
    seed._catalog_root = tmp_path
    seed.extract(WORK_URL)
    extractor, session = make_extractor(
        make_response(WORK_URL, first_html), make_response(second_url, second_html)
    )
    extractor._catalog_root = tmp_path

    extractor.extract_with_progress(WORK_URL, force_refresh=True)

    assert [call[0] for call in session.calls] == [WORK_URL, second_url]


def test_missing_cached_chapter_can_be_validated_directly() -> None:
    extractor, session = make_extractor(
        make_response(CHAPTER_URL, fixture_text("syosetu_chapter.html"))
    )
    work = NovelWork(
        "n1234ab",
        WORK_URL,
        "測試作品",
        "作者",
        "摘要",
        (NovelChapterEntry(3, "第三章", f"{WORK_URL}3/"),),
    )

    resolved = extractor.resolve_chapter(work, 1)

    assert resolved.get_chapter(1).title == "第一章"
    assert [call[0] for call in session.calls] == [CHAPTER_URL]


def test_extract_rejects_chapter_url_before_request() -> None:
    extractor, session = make_extractor()
    with pytest.raises(UnsupportedUrlError, match="這是章節網址"):
        extractor.extract(CHAPTER_URL)
    assert session.calls == []


def test_extract_reports_missing_work() -> None:
    extractor, _session = make_extractor(make_response(WORK_URL, "missing", status=404))
    with pytest.raises(WorkNotFoundError, match="找不到這部作品"):
        extractor.extract(WORK_URL)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (requests.Timeout(), "逾時"),
        (requests.ConnectionError(), "連線"),
        (make_response(WORK_URL, "error", status=500), "HTTP 500"),
        (make_response(WORK_URL, "json", content_type="application/json"), "不是 HTML"),
        (make_response(WORK_URL, ""), "空白"),
    ],
)
def test_extract_reports_network_and_response_errors(
    result: requests.Response | requests.RequestException,
    message: str,
) -> None:
    extractor, _session = make_extractor(result)
    with pytest.raises(ExtractorError, match=message):
        extractor.extract(WORK_URL)


def test_extract_rejects_redirect_to_another_work() -> None:
    response = make_response(
        "https://ncode.syosetu.com/n9999zz/",
        fixture_text("syosetu_work_page_1.html"),
    )
    extractor, _session = make_extractor(response)
    with pytest.raises(UnsupportedUrlError, match="重新導向"):
        extractor.extract(WORK_URL)


def test_extract_rejects_serial_without_numbered_chapters() -> None:
    html = fixture_text("syosetu_work_page_1.html").replace("p-eplist", "missing-list")
    html = html.replace("c-pager", "missing-pager")
    extractor, _session = make_extractor(make_response(WORK_URL, html))
    with pytest.raises(ExtractorError, match="沒有可選擇"):
        extractor.extract(WORK_URL)


def test_extract_stops_when_index_page_limit_is_exceeded() -> None:
    first = make_response(WORK_URL, fixture_text("syosetu_work_page_1.html"))
    extractor, _session = make_extractor(first, max_index_pages=1)
    with pytest.raises(ExtractorError, match="安全上限"):
        extractor.extract(WORK_URL)


def test_constructor_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="timeout"):
        SyosetuWorkExtractor(timeout=0)
    with pytest.raises(ValueError, match="max_index_pages"):
        SyosetuWorkExtractor(max_index_pages=0)
