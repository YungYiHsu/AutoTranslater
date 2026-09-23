"""Extractor for public chapter pages on Shosetsuka ni Naro."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from core.exceptions import ChapterNotFoundError, ExtractorError, UnsupportedUrlError
from core.models import NovelChapter
from extractors.base import BaseExtractor

_SUPPORTED_HOST = "ncode.syosetu.com"
_CHAPTER_PATH = re.compile(
    r"^/(?P<ncode>n[0-9a-z]+)/(?P<chapter>[1-9][0-9]*)/?$",
    re.IGNORECASE,
)
_EXCLUDED_TEXT_CLASSES = {
    "p-novel__text--preface",
    "p-novel__text--afterword",
}


class SyosetuExtractor(BaseExtractor):
    """Extract one chapter from an explicit Naro episode URL."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._logger = logging.getLogger("novel_translator.extractor.syosetu")
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.5",
            }
        )

    def can_handle(self, url: str) -> bool:
        """Return whether the URL points to one explicit supported chapter."""
        if not isinstance(url, str) or not url.strip():
            return False
        try:
            parsed = urlsplit(url.strip())
            port = parsed.port
        except ValueError:
            return False

        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.hostname is None or parsed.hostname.lower() != _SUPPORTED_HOST:
            return False
        if parsed.username or parsed.password or port not in {None, 80, 443}:
            return False
        return self._classify_path(parsed.path) is not None

    def extract(self, url: str) -> NovelChapter:
        """Download an explicit chapter URL without guessing a chapter from a work page."""
        if not self.can_handle(url):
            raise UnsupportedUrlError(
                "請貼上章節網址，格式如：https://ncode.syosetu.com/n1234ab/1/"
            )

        soup, final_url = self._fetch(url)
        classification = self._classify_path(urlsplit(final_url).path)
        if classification is None:
            raise UnsupportedUrlError("The final URL is not a supported Naro page.")
        ncode, _chapter_number = classification
        return self._parse_chapter(soup, final_url, ncode)

    def _fetch(self, url: str) -> tuple[BeautifulSoup, str]:
        self._logger.info("Fetching Naro page: %s", url)
        try:
            response = self._session.get(
                url,
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.Timeout as exc:
            raise ExtractorError("The Naro request timed out.") from exc
        except requests.RequestException as exc:
            raise ExtractorError("Unable to connect to the Naro website.") from exc

        final_url = response.url or url
        if not self.can_handle(final_url):
            raise UnsupportedUrlError("The request redirected to an unsupported URL.")
        if response.status_code == 404:
            raise ChapterNotFoundError("Naro returned HTTP 404; the chapter does not exist.")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ExtractorError(f"Naro returned HTTP {response.status_code}.") from exc

        content_type = response.headers.get("Content-Type", "")
        if content_type and "html" not in content_type.lower():
            raise ExtractorError("The Naro response was not an HTML document.")
        if not response.content:
            raise ExtractorError("The Naro response was empty.")

        self._logger.info("Fetched Naro page with HTTP %s", response.status_code)
        return BeautifulSoup(response.content, "lxml"), self._canonical_url(final_url)

    def _parse_chapter(
        self,
        soup: BeautifulSoup,
        source_url: str,
        ncode: str,
        *,
        work_title: str | None = None,
    ) -> NovelChapter:
        chapter_title = self._required_text(
            soup.select_one("h1.p-novel__title.p-novel__title--rensai"),
            "chapter title",
        )
        resolved_work_title = work_title or self._find_work_title(soup, ncode, chapter_title)
        body = self._find_main_text(soup)
        original_text = self._extract_paragraph_text(body)

        return NovelChapter(
            title=resolved_work_title,
            chapter_title=chapter_title,
            source_url=self._canonical_url(source_url),
            original_text=original_text,
            chapter_number=int(source_url.rstrip("/").rsplit("/", 1)[-1]),
        )

    def _find_work_title(self, soup: BeautifulSoup, ncode: str, chapter_title: str) -> str:
        expected_path = f"/{ncode}/"
        for link in soup.select(".c-announce a[href]"):
            href = link.get("href")
            if isinstance(href, str) and urlsplit(href).path.lower() == expected_path:
                title = link.get_text(" ", strip=True)
                if title:
                    return title

        for selector, attribute in (
            ('meta[property="og:title"]', "content"),
            ("title", None),
        ):
            element = soup.select_one(selector)
            if element is None:
                continue
            candidate = element.get(attribute) if attribute else element.get_text(" ", strip=True)
            if isinstance(candidate, str):
                suffix = f" - {chapter_title}"
                candidate = candidate.strip()
                if candidate.endswith(suffix):
                    candidate = candidate[: -len(suffix)].strip()
                if candidate:
                    return candidate
        raise ExtractorError("Unable to find the work title on the Naro chapter page.")

    def _find_main_text(self, soup: BeautifulSoup) -> Tag:
        for element in soup.select(".p-novel__body > .js-novel-text.p-novel__text"):
            classes = {str(class_name) for class_name in element.get_attribute_list("class")}
            if isinstance(element, Tag) and not _EXCLUDED_TEXT_CLASSES.intersection(classes):
                return element

        legacy = soup.select_one("#novel_honbun")
        if isinstance(legacy, Tag):
            return legacy
        raise ExtractorError(
            "Unable to find the chapter body; the site structure may have changed."
        )

    @staticmethod
    def _extract_paragraph_text(body: Tag) -> str:
        for reading in body.select("rt, rp"):
            reading.decompose()

        paragraphs = body.find_all("p", recursive=False)
        if paragraphs:
            lines: list[str] = []
            for paragraph in paragraphs:
                for line_break in paragraph.find_all("br"):
                    line_break.replace_with("\n")
                text = paragraph.get_text(separator="", strip=False)
                text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                lines.append(text if text.strip() else "")
            result = "\n".join(lines).strip("\n")
        else:
            result = body.get_text(separator="\n", strip=False).strip("\n")

        if not result.strip():
            raise ExtractorError("The chapter body was empty.")
        return result

    @staticmethod
    def _required_text(element: Tag | None, label: str) -> str:
        if element is None:
            raise ExtractorError(f"Unable to find the {label} on the Naro page.")
        value = element.get_text(" ", strip=True)
        if not value:
            raise ExtractorError(f"The {label} on the Naro page was empty.")
        return value

    @staticmethod
    def _classify_path(path: str) -> tuple[str, str] | None:
        if match := _CHAPTER_PATH.fullmatch(path):
            return match.group("ncode").lower(), match.group("chapter")
        return None

    @staticmethod
    def _canonical_url(url: str) -> str:
        parsed = urlsplit(url)
        path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


__all__ = ["SyosetuExtractor"]
