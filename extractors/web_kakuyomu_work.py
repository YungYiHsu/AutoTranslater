"""Work metadata and episode-index extractor for Kakuyomu."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from core.chapter_catalog import ChapterCatalog, ChapterCatalogStore
from core.exceptions import (
    ChapterNotFoundError,
    ExtractorError,
    UnsupportedUrlError,
    WorkNotFoundError,
)
from core.models import NovelChapterEntry, NovelWork
from core.work_directories import resolve_work_directory
from extractors.work_base import BaseWorkExtractor, CatalogProgress

_HOST = "kakuyomu.jp"
_WORK_PATH = re.compile(r"^/works/(?P<work_id>[0-9]+)/?$")
_EPISODE_PATH = re.compile(r"^/works/(?P<work_id>[0-9]+)/episodes/(?P<episode_id>[0-9]+)/?$")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}


class KakuyomuWorkExtractor(BaseWorkExtractor):
    """Extract one public Kakuyomu work and its ordered episode list."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 15.0,
        catalog_root: Path | None = None,
        catalog_store: ChapterCatalogStore | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._session = session or requests.Session()
        self._session.headers.update(_HEADERS)
        self._timeout = timeout
        self._catalog_root = Path(catalog_root) if catalog_root is not None else None
        self._catalog_store = catalog_store or ChapterCatalogStore()

    def can_handle(self, url: str) -> bool:
        parsed = self._safe_split(url)
        return parsed is not None and _WORK_PATH.fullmatch(parsed.path) is not None

    @classmethod
    def normalize_selection_url(cls, url: str) -> tuple[str, int | None]:
        parsed = cls._safe_split(url)
        if parsed is None:
            raise UnsupportedUrlError(
                "請輸入 Kakuyomu 作品首頁或章節網址，例如：https://kakuyomu.jp/works/123456789"
            )
        if match := _WORK_PATH.fullmatch(parsed.path):
            return cls._work_url(parsed.scheme, match.group("work_id")), None
        if match := _EPISODE_PATH.fullmatch(parsed.path):
            return (
                cls._work_url(parsed.scheme, match.group("work_id")),
                int(match.group("episode_id")),
            )
        raise UnsupportedUrlError("請輸入 Kakuyomu 的作品首頁或 episode 章節網址。")

    def extract(self, url: str) -> NovelWork:
        return self.extract_with_progress(url)

    def extract_with_progress(
        self,
        url: str,
        *,
        progress: CatalogProgress | None = None,
        force_refresh: bool = False,
    ) -> NovelWork:
        del force_refresh
        parsed = self._safe_split(url)
        match = _WORK_PATH.fullmatch(parsed.path) if parsed is not None else None
        if match is None:
            raise UnsupportedUrlError("請輸入 Kakuyomu 作品首頁網址。")
        work_id = match.group("work_id")
        home_url = self._work_url(parsed.scheme, work_id)
        soup = self._fetch(home_url, work_id)
        apollo = self._apollo_state(soup)
        work_data = self._entity(apollo, f"Work:{work_id}")
        title = self._required_string(work_data.get("title"), "作品名稱")
        synopsis = self._required_string(
            work_data.get("introduction") or work_data.get("catchphrase"), "作品摘要"
        )
        author = self._author_name(apollo, work_data)
        chapters = self._chapters(apollo, work_data, work_id)
        if not chapters:
            raise ExtractorError("此 Kakuyomu 作品沒有可讀取的公開章節。")
        work_directory = None
        if self._catalog_root is not None:
            work_directory = resolve_work_directory(
                self._catalog_root,
                work_id=work_id,
                source_url=home_url,
                title=title,
            )
        if work_directory is not None:
            self._catalog_store.save(
                work_directory,
                ChapterCatalog(work_id, home_url, 1, chapters),
            )
        if progress is not None:
            progress(1, 1, "refresh")
        return NovelWork(work_id, home_url, title, author, synopsis, chapters)

    def resolve_chapter(self, work: NovelWork, number: int) -> NovelWork:
        try:
            work.get_chapter(number)
            return work
        except ChapterNotFoundError:  # Episode IDs are not GUI chapter numbers.
            episode_id = str(number)
            if any(
                urlsplit(chapter.source_url).path.rstrip("/").endswith(f"/episodes/{episode_id}")
                for chapter in work.chapters
            ):
                return work
        raise ChapterNotFoundError("作品中不存在指定的 Kakuyomu 章節。")

    @staticmethod
    def chapter_number_for_episode(work: NovelWork, episode_id: int) -> int | None:
        suffix = f"/episodes/{episode_id}"
        for chapter in work.chapters:
            if urlsplit(chapter.source_url).path.rstrip("/").endswith(suffix):
                return chapter.number
        return None

    def _fetch(self, url: str, work_id: str) -> BeautifulSoup:
        try:
            response = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except requests.Timeout as exc:
            raise ExtractorError("讀取 Kakuyomu 作品資料逾時。") from exc
        except requests.ConnectionError as exc:
            raise ExtractorError("無法連線至 Kakuyomu。") from exc
        except requests.RequestException as exc:
            raise ExtractorError("讀取 Kakuyomu 作品資料失敗。") from exc
        if response.status_code in {401, 403, 404}:
            raise WorkNotFoundError("找不到這部 Kakuyomu 作品，或作品目前無法公開讀取。")
        if response.status_code >= 400:
            raise ExtractorError(f"Kakuyomu 回傳 HTTP {response.status_code}。")
        final = self._safe_split(response.url)
        if final is None or final.path.rstrip("/") != f"/works/{work_id}":
            raise WorkNotFoundError("Kakuyomu 作品已被刪除、設為非公開，或需要登入。")
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ExtractorError("Kakuyomu 回傳的內容不是 HTML。")
        if not response.text.strip():
            raise ExtractorError("Kakuyomu 回傳空白頁面。")
        return BeautifulSoup(response.text, "lxml")

    @staticmethod
    def _apollo_state(soup: BeautifulSoup) -> dict[str, Any]:
        script = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
        if script is None or not isinstance(script.string, str):
            raise ExtractorError("無法解析 Kakuyomu 作品資料，網站結構可能已變更。")
        try:
            payload = json.loads(script.string)
            apollo = payload["props"]["pageProps"]["__APOLLO_STATE__"]
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ExtractorError("無法解析 Kakuyomu 作品資料，網站結構可能已變更。") from exc
        if not isinstance(apollo, dict):
            raise ExtractorError("Kakuyomu 作品資料格式無效。")
        return apollo

    @staticmethod
    def _entity(apollo: dict[str, Any], reference: str) -> dict[str, Any]:
        value = apollo.get(reference)
        if not isinstance(value, dict):
            raise ExtractorError("Kakuyomu 作品資料缺少必要項目。")
        return value

    @classmethod
    def _author_name(cls, apollo: dict[str, Any], work: dict[str, Any]) -> str:
        alternate = work.get("alternateAuthorName")
        if isinstance(alternate, str) and alternate.strip():
            return alternate.strip()
        reference = work.get("author", {}).get("__ref")
        author = cls._entity(apollo, reference)
        return cls._required_string(author.get("activityName") or author.get("name"), "作者")

    @classmethod
    def _chapters(
        cls, apollo: dict[str, Any], work: dict[str, Any], work_id: str
    ) -> tuple[NovelChapterEntry, ...]:
        result: list[NovelChapterEntry] = []
        for toc_ref in work.get("tableOfContentsV2", []):
            toc = cls._entity(apollo, toc_ref.get("__ref"))
            for episode_ref in toc.get("episodeUnions", []):
                episode = cls._entity(apollo, episode_ref.get("__ref"))
                episode_id = cls._required_string(episode.get("id"), "章節 ID")
                title = cls._required_string(episode.get("title"), "章節名稱")
                number = len(result) + 1
                result.append(
                    NovelChapterEntry(
                        number,
                        title,
                        f"https://{_HOST}/works/{work_id}/episodes/{episode_id}",
                    )
                )
        return tuple(result)

    @staticmethod
    def _required_string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ExtractorError(f"Kakuyomu 作品資料缺少{label}。")
        return value.strip()

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

    @staticmethod
    def _work_url(scheme: str, work_id: str) -> str:
        return urlunsplit((scheme.lower(), _HOST, f"/works/{work_id}", "", ""))


__all__ = ["KakuyomuWorkExtractor"]
