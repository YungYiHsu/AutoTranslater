from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from core.exceptions import ChapterNotFoundError, ExtractorError, UnsupportedUrlError
from extractors.web_kakuyomu import KakuyomuExtractor

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CHAPTER_URL = "https://kakuyomu.jp/works/123456789/episodes/111"


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def make_response(url: str, html: str, *, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "text/html; charset=UTF-8"
    response._content = html.encode("utf-8")
    response.encoding = "utf-8"
    return response


class StubSession:
    def __init__(self, results: list[requests.Response | requests.RequestException]) -> None:
        self.headers: dict[str, str] = {}
        self.results: Iterator[requests.Response | requests.RequestException] = iter(results)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> requests.Response:
        self.calls.append(url)
        result = next(self.results)
        if isinstance(result, requests.RequestException):
            raise result
        return result


def make_extractor(*results: requests.Response | requests.RequestException):
    session = StubSession(list(results))
    return KakuyomuExtractor(cast(requests.Session, session), chapter_number=2), session


def test_extracts_kakuyomu_chapter_and_preserves_blank_lines() -> None:
    extractor, session = make_extractor(
        make_response(CHAPTER_URL, fixture_text("kakuyomu_chapter.html"))
    )

    chapter = extractor.extract(CHAPTER_URL)

    assert chapter.title == "テスト作品"
    assert chapter.chapter_title == "プロローグ"
    assert chapter.original_text == "第一行。山田\n\n「台詞」"
    assert chapter.chapter_number == 2
    assert "やまだ" not in chapter.original_text
    assert session.calls == [CHAPTER_URL]


def test_uses_selected_work_title_when_episode_page_omits_work_heading() -> None:
    html = fixture_text("kakuyomu_chapter.html").replace(
        '<p id="contentMain-header-workTitle">\u30c6\u30b9\u30c8\u4f5c\u54c1</p>', ""
    )
    session = StubSession([make_response(CHAPTER_URL, html)])
    extractor = KakuyomuExtractor(
        cast(requests.Session, session),
        chapter_number=2,
        expected_work_title="テスト作品",
    )

    chapter = extractor.extract(CHAPTER_URL)

    assert chapter.title == "テスト作品"
    assert chapter.chapter_number == 2


def test_parses_work_title_from_document_title_without_work_heading() -> None:
    html = fixture_text("kakuyomu_chapter.html").replace(
        '<p id="contentMain-header-workTitle">\u30c6\u30b9\u30c8\u4f5c\u54c1</p>', ""
    ).replace(
        "</head>", "<title>\u7b2c1\u8a71 - \u30c6\u30b9\u30c8\u4f5c\u54c1\uff08\u30c6\u30b9\u30c8\u4f5c\u8005\uff09 - \u30ab\u30af\u30e8\u30e0</title></head>"
    )
    extractor, _ = make_extractor(make_response(CHAPTER_URL, html))

    chapter = extractor.extract(CHAPTER_URL)

    assert chapter.title == "テスト作品"


def test_rejects_episode_heading_from_another_selected_work() -> None:
    extractor = KakuyomuExtractor(
        cast(
            requests.Session,
            StubSession([make_response(CHAPTER_URL, fixture_text("kakuyomu_chapter.html"))]),
        ),
        chapter_number=2,
        expected_work_title="其他作品",
    )

    with pytest.raises(ExtractorError, match="不一致"):
        extractor.extract(CHAPTER_URL)


@pytest.mark.parametrize(
    "url",
    [
        "https://kakuyomu.jp/works/123456789/episodes/111",
        "http://kakuyomu.jp/works/123456789/episodes/111/",
    ],
)
def test_can_handle_episode_urls(url: str) -> None:
    assert KakuyomuExtractor().can_handle(url)


def test_rejects_work_url_before_request() -> None:
    extractor, session = make_extractor()
    with pytest.raises(UnsupportedUrlError):
        extractor.extract("https://kakuyomu.jp/works/123456789")
    assert session.calls == []


def test_reports_missing_episode() -> None:
    extractor, _ = make_extractor(make_response(CHAPTER_URL, "missing", status=404))
    with pytest.raises(ChapterNotFoundError):
        extractor.extract(CHAPTER_URL)


@pytest.mark.parametrize("status", [401, 403])
def test_reports_non_public_episode(status: int) -> None:
    extractor, _ = make_extractor(make_response(CHAPTER_URL, "private", status=status))
    with pytest.raises(ChapterNotFoundError, match="無法公開讀取"):
        extractor.extract(CHAPTER_URL)


def test_rejects_missing_body() -> None:
    html = fixture_text("kakuyomu_chapter.html").replace("js-episode-body", "missing")
    extractor, _ = make_extractor(make_response(CHAPTER_URL, html))
    with pytest.raises(ExtractorError, match="正文"):
        extractor.extract(CHAPTER_URL)
