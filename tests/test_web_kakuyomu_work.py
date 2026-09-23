from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from core.exceptions import ChapterNotFoundError, ExtractorError, UnsupportedUrlError
from extractors.web_kakuyomu_work import KakuyomuWorkExtractor

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WORK_URL = "https://kakuyomu.jp/works/123456789"
EPISODE_URL = f"{WORK_URL}/episodes/222"


def fixture_text() -> str:
    return (FIXTURE_DIR / "kakuyomu_work.html").read_text(encoding="utf-8")


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

    def get(self, _url: str, **_kwargs: Any) -> requests.Response:
        result = next(self.results)
        if isinstance(result, requests.RequestException):
            raise result
        return result


def make_extractor(
    *results: requests.Response | requests.RequestException, root: Path | None = None
):
    session = StubSession(list(results))
    return KakuyomuWorkExtractor(cast(requests.Session, session), catalog_root=root)


def test_normalizes_work_and_episode_urls() -> None:
    assert KakuyomuWorkExtractor.normalize_selection_url(WORK_URL) == (WORK_URL, None)
    assert KakuyomuWorkExtractor.normalize_selection_url(EPISODE_URL) == (WORK_URL, 222)
    with pytest.raises(UnsupportedUrlError):
        KakuyomuWorkExtractor.normalize_selection_url("https://kakuyomu.jp/users/example")


def test_extracts_metadata_and_ordered_episode_index(tmp_path: Path) -> None:
    extractor = make_extractor(make_response(WORK_URL, fixture_text()), root=tmp_path)

    work = extractor.extract(WORK_URL)

    assert work.work_id == "123456789"
    assert work.site == "kakuyomu"
    assert work.title == "テスト作品"
    assert work.author == "テスト作者"
    assert work.synopsis == "一段目。\n\n二段目。"
    assert [(item.number, item.title) for item in work.chapters] == [
        (1, "プロローグ"),
        (2, "第一話"),
        (3, "第二話"),
    ]
    assert work.get_chapter(2).source_url == EPISODE_URL
    assert (tmp_path / "テスト作品" / "chapters.json").is_file()
    assert KakuyomuWorkExtractor.chapter_number_for_episode(work, 222) == 2
    assert extractor.resolve_chapter(work, 222) is work
    with pytest.raises(ChapterNotFoundError):
        extractor.resolve_chapter(work, 999)


def test_rejects_invalid_embedded_data() -> None:
    extractor = make_extractor(make_response(WORK_URL, "<html></html>"))
    with pytest.raises(ExtractorError, match="網站結構"):
        extractor.extract(WORK_URL)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_reports_missing_or_non_public_work(status: int) -> None:
    extractor = make_extractor(make_response(WORK_URL, "private", status=status))
    with pytest.raises(ExtractorError, match="無法公開讀取"):
        extractor.extract(WORK_URL)


def test_refresh_replaces_changed_episode_title(tmp_path: Path) -> None:
    first = make_extractor(make_response(WORK_URL, fixture_text()), root=tmp_path)
    first.extract(WORK_URL)
    changed_html = fixture_text().replace('"title":"第一話"', '"title":"修正版第一話"')
    refreshed = make_extractor(make_response(WORK_URL, changed_html), root=tmp_path)

    work = refreshed.extract_with_progress(WORK_URL, force_refresh=True)

    assert work.get_chapter(2).title == "修正版第一話"
    payload = (tmp_path / "テスト作品" / "chapters.json").read_text(encoding="utf-8")
    assert "修正版第一話" in payload
