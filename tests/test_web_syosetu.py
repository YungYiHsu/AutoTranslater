"""Offline tests for explicit Naro chapter URLs and the current chapter DOM."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from core.exceptions import ExtractorError, UnsupportedUrlError
from extractors.web_syosetu import SyosetuExtractor

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WORK_URL = "https://ncode.syosetu.com/n1234ab/"
CHAPTER_URL = "https://ncode.syosetu.com/n1234ab/1/"


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
    """Minimal requests session replacement that never accesses the network."""

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
) -> tuple[SyosetuExtractor, StubSession]:
    session = StubSession(list(results))
    extractor = SyosetuExtractor(session=cast(requests.Session, session), timeout=7.5)
    return extractor, session


def test_constructor_sets_browser_compatible_request_headers() -> None:
    _extractor, session = make_extractor()

    assert session.headers["User-Agent"].startswith("Mozilla/5.0")
    assert "text/html" in session.headers["Accept"]
    assert session.headers["Accept-Language"].startswith("ja")


@pytest.mark.parametrize(
    "url",
    [
        CHAPTER_URL,
        "http://ncode.syosetu.com/n1234ab/1",
        "https://ncode.syosetu.com/N1234AB/12?from=test#top",
    ],
)
def test_can_handle_supported_chapter_urls(url: str) -> None:
    assert SyosetuExtractor().can_handle(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        WORK_URL,
        "http://ncode.syosetu.com/n1234ab",
        "ftp://ncode.syosetu.com/n1234ab/",
        "https://example.com/n1234ab/",
        "https://ncode.syosetu.com.example.com/n1234ab/",
        "https://user@ncode.syosetu.com/n1234ab/",
        "https://ncode.syosetu.com:444/n1234ab/",
        "https://ncode.syosetu.com/n1234ab/info/",
        "https://ncode.syosetu.com/n1234ab/0/",
    ],
)
def test_can_handle_rejects_unsupported_or_unsafe_urls(url: str) -> None:
    assert not SyosetuExtractor().can_handle(url)


def test_extract_chapter_preserves_lines_and_removes_ruby_readings() -> None:
    response = make_response(CHAPTER_URL, fixture_text("syosetu_chapter.html"))
    extractor, session = make_extractor(response)

    chapter = extractor.extract(CHAPTER_URL)

    assert chapter.title == "測試作品"
    assert chapter.chapter_title == "第一章"
    assert chapter.source_url == CHAPTER_URL
    assert chapter.original_text == ("　第一行。\n「台詞」\n\n名前は山田です。\n\n\n　場面転換。")
    assert "前言" not in chapter.original_text
    assert "後記" not in chapter.original_text
    assert "廣告" not in chapter.original_text
    assert "やまだ" not in chapter.original_text
    assert session.calls == [(CHAPTER_URL, {"timeout": 7.5, "allow_redirects": True})]


def test_extract_rejects_work_index_before_request() -> None:
    extractor, session = make_extractor()
    with pytest.raises(UnsupportedUrlError, match="章節網址"):
        extractor.extract(WORK_URL)
    assert session.calls == []


def test_chapter_title_falls_back_to_open_graph_metadata() -> None:
    html = fixture_text("syosetu_chapter.html").replace(
        '<div class="c-announce">\n        <a href="/n1234ab/">測試作品</a> 作者：<a href="/author/1/">測試作者</a>\n      </div>',
        "",
    )
    extractor, _session = make_extractor(make_response(CHAPTER_URL, html))

    assert extractor.extract(CHAPTER_URL).title == "測試作品"


def test_extract_rejects_redirect_to_unsupported_host() -> None:
    response = make_response(
        "https://example.com/redirected/",
        fixture_text("syosetu_chapter.html"),
    )
    extractor, _session = make_extractor(response)

    with pytest.raises(UnsupportedUrlError, match="redirected"):
        extractor.extract(CHAPTER_URL)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (requests.Timeout(), "timed out"),
        (requests.ConnectionError(), "connect"),
        (make_response(CHAPTER_URL, "error", status=404), "HTTP 404"),
        (
            make_response(CHAPTER_URL, "not html", content_type="application/json"),
            "not an HTML",
        ),
        (make_response(CHAPTER_URL, ""), "empty"),
    ],
)
def test_extract_reports_network_and_response_errors(
    result: requests.Response | requests.RequestException,
    message: str,
) -> None:
    extractor, _session = make_extractor(result)

    with pytest.raises(ExtractorError, match=message):
        extractor.extract(CHAPTER_URL)


def test_extract_rejects_missing_chapter_body() -> None:
    html = fixture_text("syosetu_chapter.html").replace("p-novel__body", "missing-body")
    extractor, _session = make_extractor(make_response(CHAPTER_URL, html))

    with pytest.raises(ExtractorError, match="chapter body"):
        extractor.extract(CHAPTER_URL)


def test_extract_rejects_unsupported_input_before_request() -> None:
    extractor, session = make_extractor()

    with pytest.raises(UnsupportedUrlError):
        extractor.extract("https://example.com/story/1")
    assert session.calls == []


def test_constructor_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout"):
        SyosetuExtractor(timeout=0)
