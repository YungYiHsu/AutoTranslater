"""Chapter body extractor for public Kakuyomu episodes."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from core.exceptions import ChapterNotFoundError, ExtractorError, UnsupportedUrlError
from core.models import NovelChapter
from extractors.base import BaseExtractor

_HOST = "kakuyomu.jp"
_PATH = re.compile(r"^/works/(?P<work_id>[0-9]+)/episodes/(?P<episode_id>[0-9]+)/?$")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}


class KakuyomuExtractor(BaseExtractor):
    """Extract title and paragraphs from one explicit Kakuyomu episode URL."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 15.0,
        chapter_number: int | None = None,
        expected_work_title: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)
        self._timeout = timeout
        self._chapter_number = chapter_number
        self._expected_work_title = (
            expected_work_title.strip() if expected_work_title and expected_work_title.strip() else None
        )

    def can_handle(self, url: str) -> bool:
        parsed = self._safe_split(url)
        return parsed is not None and _PATH.fullmatch(parsed.path) is not None

    def extract(self, url: str) -> NovelChapter:
        parsed = self._safe_split(url)
        match = _PATH.fullmatch(parsed.path) if parsed is not None else None
        if match is None:
            raise UnsupportedUrlError("請輸入 Kakuyomu episode 章節網址。")
        canonical = urlunsplit(
            (
                parsed.scheme.lower(),
                _HOST,
                f"/works/{match.group('work_id')}/episodes/{match.group('episode_id')}",
                "",
                "",
            )
        )
        soup = self._fetch(canonical, match.group("work_id"), match.group("episode_id"))
        work_title = self._work_title(soup)
        chapter_title = self._required_text(soup.select_one(".widget-episodeTitle"), "章節名稱")
        body = soup.select_one(".js-episode-body")
        if not isinstance(body, Tag):
            raise ExtractorError("找不到 Kakuyomu 章節正文，網站結構可能已變更。")
        original_text = self._paragraph_text(body)
        return NovelChapter(
            work_title,
            chapter_title,
            canonical,
            original_text,
            chapter_number=self._chapter_number,
        )

    def _work_title(self, soup: BeautifulSoup) -> str:
        """Return a stable work title across Kakuyomu's episode page variants."""
        heading = soup.select_one("#contentMain-header-workTitle")
        heading_title = heading.get_text(" ", strip=True) if heading is not None else ""
        if self._expected_work_title is not None:
            if heading_title and heading_title != self._expected_work_title:
                raise ExtractorError("章節頁的作品名稱與已選作品不一致。")
            return self._expected_work_title
        if heading_title:
            return heading_title

        for selector, attribute in (
            ('meta[property="og:title"]', "content"),
            ('meta[name="twitter:title"]', "content"),
        ):
            element = soup.select_one(selector)
            value = element.get(attribute, "") if element is not None else ""
            parsed = self._work_title_from_document_title(str(value))
            if parsed:
                return parsed
        document_title = soup.title.get_text(" ", strip=True) if soup.title is not None else ""
        parsed = self._work_title_from_document_title(document_title)
        if parsed:
            return parsed
        raise ExtractorError("找不到 Kakuyomu 作品名稱，網站結構可能已變更。")

    @staticmethod
    def _work_title_from_document_title(value: str) -> str | None:
        """Parse ``episode - work（author） - カクヨム`` when available."""
        text = value.strip()
        suffix = " - カクヨム"
        if not text.endswith(suffix):
            return None
        without_site = text[: -len(suffix)]
        separator = " - "
        if separator not in without_site:
            return None
        work_and_author = without_site.split(separator, 1)[1].strip()
        if work_and_author.endswith("）") and "（" in work_and_author:
            work_and_author = work_and_author.rsplit("（", 1)[0].strip()
        return work_and_author or None

    def _fetch(self, url: str, work_id: str, episode_id: str) -> BeautifulSoup:
        try:
            response = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except requests.Timeout as exc:
            raise ExtractorError("讀取 Kakuyomu 章節逾時。") from exc
        except requests.ConnectionError as exc:
            raise ExtractorError("無法連線至 Kakuyomu。") from exc
        except requests.RequestException as exc:
            raise ExtractorError("讀取 Kakuyomu 章節失敗。") from exc
        if response.status_code in {401, 403, 404}:
            raise ChapterNotFoundError("找不到這個 Kakuyomu 章節，或章節目前無法公開讀取。")
        if response.status_code >= 400:
            raise ExtractorError(f"Kakuyomu 回傳 HTTP {response.status_code}。")
        final = self._safe_split(response.url)
        expected = f"/works/{work_id}/episodes/{episode_id}"
        if final is None or final.path.rstrip("/") != expected:
            raise ChapterNotFoundError("Kakuyomu 章節已被刪除、設為非公開，或需要登入。")
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ExtractorError("Kakuyomu 回傳的內容不是 HTML。")
        if not response.text.strip():
            raise ExtractorError("Kakuyomu 回傳空白頁面。")
        return BeautifulSoup(response.text, "lxml")

    @staticmethod
    def _paragraph_text(body: Tag) -> str:
        for reading in body.select("rt, rp"):
            reading.decompose()
        lines: list[str] = []
        for paragraph in body.find_all("p", recursive=False):
            for line_break in paragraph.find_all("br"):
                line_break.replace_with("\n")
            text = paragraph.get_text(separator="", strip=False)
            lines.append(text.strip("\r\n") if text.strip() else "")
        result = "\n".join(lines).strip("\n")
        if not result.strip():
            raise ExtractorError("Kakuyomu 章節正文是空白的。")
        return result

    @staticmethod
    def _required_text(element: Tag | None, label: str) -> str:
        if element is None or not element.get_text(" ", strip=True):
            raise ExtractorError(f"Kakuyomu 頁面缺少{label}。")
        return element.get_text(" ", strip=True)

    @staticmethod
    def _safe_split(url: str):
        try:
            parsed = urlsplit(url.strip())
        except (AttributeError, ValueError):
            return None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.hostname != _HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return None
        return parsed


__all__ = ["KakuyomuExtractor"]
