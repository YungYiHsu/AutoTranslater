"""Work-index extractor for public Shosetsuka ni Naro serials."""

from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path
from urllib.parse import SplitResult, parse_qs, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

from core.chapter_catalog import ChapterCatalog, ChapterCatalogStore
from core.exceptions import (
    ChapterNotFoundError,
    ExtractorError,
    UnsupportedUrlError,
    WorkNotFoundError,
)
from core.models import NovelChapterEntry, NovelWork
from core.work_directories import resolve_work_directory
from extractors.web_syosetu import SyosetuExtractor
from extractors.work_base import BaseWorkExtractor, CatalogProgress

_SUPPORTED_HOST = "ncode.syosetu.com"
_WORK_PATH = re.compile(r"^/(?P<ncode>n[0-9a-z]+)/?$", re.IGNORECASE)
_CHAPTER_PATH = re.compile(
    r"^/(?P<ncode>n[0-9a-z]+)/(?P<chapter>[1-9][0-9]*)/?$",
    re.IGNORECASE,
)
_MAX_INDEX_PAGES = 100
_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}


class SyosetuWorkExtractor(BaseWorkExtractor):
    """Extract metadata and every numbered chapter from one work home URL."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 15.0,
        max_index_pages: int = _MAX_INDEX_PAGES,
        catalog_root: Path | None = None,
        catalog_store: ChapterCatalogStore | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_index_pages <= 0:
            raise ValueError("max_index_pages must be greater than zero")
        self._session = session or requests.Session()
        self._session.headers.update(_REQUEST_HEADERS)
        self._timeout = timeout
        self._max_index_pages = max_index_pages
        self._catalog_root = Path(catalog_root) if catalog_root is not None else None
        self._catalog_store = catalog_store or ChapterCatalogStore()
        self._logger = logging.getLogger("novel_translator.extractor.syosetu_work")

    def can_handle(self, url: str) -> bool:
        """Return whether the URL is an explicit Naro work home page."""
        parsed = self._safe_split(url)
        return parsed is not None and _WORK_PATH.fullmatch(parsed.path) is not None

    @classmethod
    def normalize_selection_url(cls, url: str) -> tuple[str, int | None]:
        """Normalize a work or chapter URL into its work home and optional chapter."""
        parsed = cls._safe_split(url)
        if parsed is None:
            raise UnsupportedUrlError(
                "請輸入作品首頁或章節網址，例如：https://ncode.syosetu.com/n1234ab/"
            )
        if work_match := _WORK_PATH.fullmatch(parsed.path):
            ncode = work_match.group("ncode").lower()
            return cls._canonical_work_url(url, ncode), None
        if chapter_match := _CHAPTER_PATH.fullmatch(parsed.path):
            ncode = chapter_match.group("ncode").lower()
            return cls._canonical_work_url(url, ncode), int(chapter_match.group("chapter"))
        raise UnsupportedUrlError(
            "請輸入作品首頁或章節網址，例如：https://ncode.syosetu.com/n1234ab/1/"
        )

    def extract(self, url: str) -> NovelWork:
        """Fetch work metadata and follow safe index pagination links."""
        return self.extract_with_progress(url)

    def extract_with_progress(
        self,
        url: str,
        *,
        progress: CatalogProgress | None = None,
        force_refresh: bool = False,
    ) -> NovelWork:
        """Use a local catalog when possible, or rebuild it by following pagination."""
        parsed = self._safe_split(url)
        if parsed is None:
            raise UnsupportedUrlError(
                "請輸入作品首頁網址，例如：https://ncode.syosetu.com/n1234ab/"
            )
        if _CHAPTER_PATH.fullmatch(parsed.path):
            raise UnsupportedUrlError("這是章節網址，請輸入不含章節數字的作品首頁網址。")
        match = _WORK_PATH.fullmatch(parsed.path)
        if match is None:
            raise UnsupportedUrlError(
                "請輸入作品首頁網址，例如：https://ncode.syosetu.com/n1234ab/"
            )

        ncode = match.group("ncode").lower()
        home_url = self._canonical_work_url(url, ncode)
        first_soup, first_url = self._fetch(home_url, ncode)
        title = self._required_page_text(
            first_soup, ("h1.p-novel__title", ".p-novel__title"), "作品名稱"
        )
        author = self._required_page_text(
            first_soup, (".p-novel__author a", ".p-novel__author"), "作者"
        )
        synopsis = self._required_page_text(
            first_soup,
            (".p-novel__summary", "#novel_ex"),
            "作品摘要",
            preserve_lines=True,
        )
        work_directory = None
        if self._catalog_root is not None:
            work_directory = resolve_work_directory(
                self._catalog_root,
                work_id=ncode,
                source_url=home_url,
                title=title,
            )
        cached = (
            self._catalog_store.load(work_directory, ncode)
            if work_directory is not None and not force_refresh
            else None
        )
        if cached is not None:
            chapters, last_page = self._incremental_catalog(
                cached, first_soup, first_url, ncode, progress
            )
            source = "cache"
        else:
            chapters, last_page = self._complete_catalog(first_soup, first_url, ncode, progress)
            source = "refresh" if force_refresh else "full"

        if not chapters:
            raise ExtractorError("此作品沒有可選擇的數字章節，目前只支援連載作品。")

        ordered = tuple(chapters[number] for number in sorted(chapters))
        if work_directory is not None:
            self._catalog_store.save(
                work_directory,
                ChapterCatalog(ncode, home_url, last_page, ordered),
            )
        if progress is not None:
            progress(last_page, last_page, source)

        return NovelWork(
            ncode=ncode,
            source_url=home_url,
            title=title,
            author=author,
            synopsis=synopsis,
            chapters=ordered,
        )

    def _complete_catalog(
        self,
        first_soup: BeautifulSoup,
        first_url: str,
        ncode: str,
        progress: CatalogProgress | None,
    ) -> tuple[dict[int, NovelChapterEntry], int]:
        chapters = {
            item.number: item for item in self._extract_chapters(first_soup, first_url, ncode)
        }
        pending = deque(self._pagination_urls(first_soup, first_url, ncode))
        visited = {self._page_key(first_url)}
        highest_page = max((self._page_number(url) or 1 for url in pending), default=1)
        if progress is not None:
            progress(1, highest_page, "refresh")
        while pending:
            page_url = pending.popleft()
            page_key = self._page_key(page_url)
            if page_key in visited:
                continue
            if len(visited) >= self._max_index_pages:
                raise ExtractorError("作品目錄頁數超過安全上限，無法完成擷取。")
            visited.add(page_key)
            soup, final_url = self._fetch(page_url, ncode)
            self._merge_chapters(chapters, self._extract_chapters(soup, final_url, ncode))
            links = self._pagination_urls(soup, final_url, ncode)
            for pagination_url in links:
                if self._page_key(pagination_url) not in visited:
                    pending.append(pagination_url)
            highest_page = max(
                highest_page,
                self._page_number(final_url) or 1,
                *(self._page_number(item) or 1 for item in links),
            )
            if progress is not None:
                progress(len(visited), highest_page, "refresh")
        return chapters, max(self._page_number_from_key(key) for key in visited)

    def _incremental_catalog(
        self,
        cached: ChapterCatalog,
        first_soup: BeautifulSoup,
        first_url: str,
        ncode: str,
        progress: CatalogProgress | None,
    ) -> tuple[dict[int, NovelChapterEntry], int]:
        chapters = {item.number: item for item in cached.chapters}
        self._merge_chapters(chapters, self._extract_chapters(first_soup, first_url, ncode))
        if cached.last_page == 1:
            links = self._pagination_urls(first_soup, first_url, ncode)
            new_links = [url for url in links if (self._page_number(url) or 1) > 1]
            if not new_links:
                if progress is not None:
                    progress(1, 1, "cache")
                return chapters, 1
            tail_url = min(new_links, key=lambda item: self._page_number(item) or 1)
        else:
            tail_url = f"{cached.source_url}?p={cached.last_page}"

        visited_pages = {1}
        pending = deque([tail_url])
        last_page = cached.last_page
        while pending:
            page_url = pending.popleft()
            page_number = self._page_number(page_url) or 1
            if page_number in visited_pages:
                continue
            if len(visited_pages) >= self._max_index_pages:
                raise ExtractorError("作品目錄頁數超過安全上限，無法完成擷取。")
            soup, final_url = self._fetch(page_url, ncode)
            visited_pages.add(page_number)
            self._merge_chapters(chapters, self._extract_chapters(soup, final_url, ncode))
            last_page = max(last_page, page_number)
            for link in self._pagination_urls(soup, final_url, ncode):
                linked_page = self._page_number(link) or 1
                if linked_page > last_page and linked_page not in visited_pages:
                    pending.append(link)
            if progress is not None:
                progress(len(visited_pages), max(2, len(visited_pages)), "cache")
        return chapters, last_page

    @staticmethod
    def _merge_chapters(
        chapters: dict[int, NovelChapterEntry], incoming: tuple[NovelChapterEntry, ...]
    ) -> None:
        for chapter in incoming:
            chapters[chapter.number] = chapter

    @staticmethod
    def _page_number_from_key(key: str) -> int:
        return int(parse_qs(urlsplit(key).query)["p"][0])

    def _fetch(self, url: str, ncode: str) -> tuple[BeautifulSoup, str]:
        self._logger.info("Fetching Naro work index: %s", url)
        try:
            response = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except requests.Timeout as exc:
            raise ExtractorError("讀取作品資料逾時，請稍後再試。") from exc
        except requests.RequestException as exc:
            raise ExtractorError("連線到小說網站失敗，請稍後再試。") from exc

        final_url = response.url or url
        if not self._is_safe_index_page(final_url, ncode):
            raise UnsupportedUrlError("作品頁面重新導向到不支援的網址。")
        if response.status_code == 404:
            raise WorkNotFoundError(
                "找不到這部作品，請確認作品首頁網址是否正確，或作品是否已被刪除。"
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ExtractorError(f"小說網站回傳 HTTP {response.status_code}。") from exc
        content_type = response.headers.get("Content-Type", "")
        if content_type and "html" not in content_type.lower():
            raise ExtractorError("小說網站回傳的內容不是 HTML。")
        if not response.content:
            raise ExtractorError("小說網站回傳空白內容。")
        return BeautifulSoup(response.content, "lxml"), final_url

    def resolve_chapter(self, work: NovelWork, number: int) -> NovelWork:
        """Directly validate a chapter missing from a possibly stale local catalog."""
        try:
            work.get_chapter(number)
            return work
        except ChapterNotFoundError:
            chapter_url = f"{work.source_url}{number}/"
            try:
                source = SyosetuExtractor(session=self._session, timeout=self._timeout).extract(
                    chapter_url
                )
            except ChapterNotFoundError as exc:
                raise ChapterNotFoundError(f"作品中不存在第 {number} 章。") from exc
            entry = NovelChapterEntry(number, source.chapter_title, chapter_url)
            chapters = tuple(sorted((*work.chapters, entry), key=lambda item: item.number))
            resolved = NovelWork(
                work.ncode,
                work.source_url,
                work.title,
                work.author,
                work.synopsis,
                chapters,
            )
            if self._catalog_root is not None:
                directory = resolve_work_directory(
                    self._catalog_root,
                    work_id=work.work_id,
                    source_url=work.source_url,
                    title=work.title,
                )
                catalog = self._catalog_store.load(directory, work.ncode)
                last_page = catalog.last_page if catalog is not None else 1
                self._catalog_store.save(
                    directory,
                    ChapterCatalog(work.ncode, work.source_url, last_page, chapters),
                )
            return resolved

    @staticmethod
    def _extract_chapters(
        soup: BeautifulSoup, page_url: str, ncode: str
    ) -> tuple[NovelChapterEntry, ...]:
        found: dict[int, NovelChapterEntry] = {}
        for link in soup.select(".p-eplist a[href], a.p-eplist__subtitle[href]"):
            if not isinstance(link, Tag):
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            absolute = urljoin(page_url, href)
            parsed = urlsplit(absolute)
            match = _CHAPTER_PATH.fullmatch(parsed.path)
            if (
                parsed.hostname is None
                or parsed.hostname.lower() != _SUPPORTED_HOST
                or match is None
                or match.group("ncode").lower() != ncode
            ):
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            number = int(match.group("chapter"))
            found[number] = NovelChapterEntry(
                number=number,
                title=title,
                source_url=urlunsplit(("https", _SUPPORTED_HOST, f"/{ncode}/{number}/", "", "")),
            )
        return tuple(found[number] for number in sorted(found))

    @classmethod
    def _pagination_urls(cls, soup: BeautifulSoup, page_url: str, ncode: str) -> tuple[str, ...]:
        found: set[str] = set()
        for link in soup.select(".c-pager a[href], a.c-pager__item[href]"):
            href = link.get("href")
            if not isinstance(href, str):
                continue
            absolute = urljoin(page_url, href)
            if cls._is_safe_index_page(absolute, ncode) and cls._page_number(absolute) is not None:
                found.add(absolute)
        return tuple(sorted(found, key=lambda item: cls._page_number(item) or 1))

    @staticmethod
    def _required_page_text(
        soup: BeautifulSoup,
        selectors: tuple[str, ...],
        label: str,
        *,
        preserve_lines: bool = False,
    ) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if isinstance(element, Tag):
                separator = "\n" if preserve_lines else " "
                value = element.get_text(separator, strip=True)
                if value:
                    return value
        raise WorkNotFoundError(f"找不到作品的{label}，作品可能不存在或網站結構已改變。")

    @staticmethod
    def _safe_split(url: str) -> SplitResult | None:
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            parsed = urlsplit(url.strip())
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.hostname is None or parsed.hostname.lower() != _SUPPORTED_HOST:
            return None
        if parsed.username or parsed.password or port not in {None, 80, 443}:
            return None
        return parsed

    @classmethod
    def _is_safe_index_page(cls, url: str, ncode: str) -> bool:
        parsed = cls._safe_split(url)
        if parsed is None:
            return False
        match = _WORK_PATH.fullmatch(parsed.path)
        if match is None or match.group("ncode").lower() != ncode:
            return False
        return cls._page_number(url) is not None

    @staticmethod
    def _page_number(url: str) -> int | None:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if not query:
            return 1
        if set(query) != {"p"} or len(query["p"]) != 1:
            return None
        value = query["p"][0]
        return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None

    @classmethod
    def _page_key(cls, url: str) -> str:
        parsed = urlsplit(url)
        page = cls._page_number(url) or 1
        return urlunsplit(("https", _SUPPORTED_HOST, parsed.path.lower(), f"p={page}", ""))

    @staticmethod
    def _canonical_work_url(url: str, ncode: str) -> str:
        scheme = urlsplit(url).scheme.lower()
        return urlunsplit((scheme, _SUPPORTED_HOST, f"/{ncode}/", "", ""))


__all__ = ["SyosetuWorkExtractor"]
