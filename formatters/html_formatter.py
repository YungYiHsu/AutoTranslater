"""Safe, reader-friendly HTML output strategy."""

from __future__ import annotations

import hashlib
import logging
import webbrowser
from collections.abc import Callable
from pathlib import Path

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, select_autoescape

from core.exceptions import FormatterError
from core.models import TranslatedChapter
from core.paths import get_resource_path
from formatters.base import BaseFormatter
from formatters.html_navigation import preserve_navigation_block
from formatters.utils import (
    TXT_BODY_SEPARATOR,
    atomic_write_text,
    build_output_stem,
    read_translated_text_from_txt,
)


class HtmlFormatter(BaseFormatter):
    """Render a translated chapter through an auto-escaped Jinja template."""

    def __init__(
        self,
        *,
        auto_open: bool = True,
        overwrite: bool = False,
        template_path: Path | None = None,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._auto_open = auto_open
        self._overwrite = overwrite
        self._template_path = template_path or get_resource_path("resources/templates/novel.html")
        self._opener = opener or webbrowser.open
        self._logger = logging.getLogger("novel_translator.formatter.html")

    def save(
        self,
        chapter: TranslatedChapter,
        output_dir: Path,
        *,
        txt_path: Path | None = None,
        destination_path: Path | None = None,
    ) -> Path:
        """Render and save one standalone HTML reading page."""
        if not isinstance(chapter, TranslatedChapter):
            raise TypeError("chapter must be a TranslatedChapter")

        output_path = Path(output_dir)
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            stem = build_output_stem(chapter)
            txt_source = txt_path or output_path / f"{stem}.txt"
            translated_text = read_translated_text_from_txt(txt_source)
            html = self._render(chapter, translated_text)
            destination = destination_path or output_path / f"{stem}.html"
            if destination.exists() and not self._overwrite:
                raise FormatterError("HTML output already exists; overwrite was not confirmed.")
            if destination.exists():
                existing_html = destination.read_text(encoding="utf-8-sig")
                html = preserve_navigation_block(existing_html, html)
            atomic_write_text(destination, html, encoding="utf-8")
        except FormatterError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise FormatterError(
                "Unable to generate HTML from the paired TXT output; "
                "the TXT file may be missing or invalid."
            ) from exc

        self._logger.info("Saved HTML output: %s", destination)
        if self._auto_open:
            self._open_in_browser(destination)
        return destination

    def save_blank(
        self,
        chapter: TranslatedChapter,
        output_dir: Path,
        *,
        txt_path: Path,
        destination_path: Path,
    ) -> Path:
        """Create a draft HTML page paired with a deliberately blank TXT body."""
        if not isinstance(chapter, TranslatedChapter):
            raise TypeError("chapter must be a TranslatedChapter")
        output_path = Path(output_dir)
        try:
            output_path.mkdir(parents=True, exist_ok=True)
            if destination_path.exists() and not self._overwrite:
                raise FormatterError("HTML output already exists; overwrite was not confirmed.")
            content = Path(txt_path).read_text(encoding="utf-8-sig")
            marker = f"\n{TXT_BODY_SEPARATOR}\n\n"
            _header, separator, body = content.partition(marker)
            if not separator:
                raise ValueError("TXT body separator is missing.")
            if body:
                raise ValueError("Draft TXT body must be empty.")
            html = self._render(chapter, "", is_draft=True)
            atomic_write_text(destination_path, html, encoding="utf-8")
        except FormatterError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise FormatterError("Unable to generate the blank HTML draft.") from exc
        self._logger.info("Saved blank HTML draft: %s", destination_path)
        return destination_path

    def _render(
        self,
        chapter: TranslatedChapter,
        translated_text: str,
        *,
        is_draft: bool = False,
    ) -> str:
        try:
            environment = Environment(
                loader=FileSystemLoader(self._template_path.parent),
                autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
                undefined=StrictUndefined,
            )
            template = environment.get_template(self._template_path.name)
            return template.render(
                chapter=chapter,
                source=chapter.source_chapter,
                translated_text=translated_text,
                txt_body_hash=self._body_hash(translated_text),
                is_draft=is_draft,
            )
        except (OSError, TemplateError) as exc:
            raise FormatterError("Unable to load or render the HTML template.") from exc

    @classmethod
    def txt_matches_html(cls, txt_path: Path, html_path: Path) -> bool:
        """Compare editable TXT body with HTML, supporting HTML created before hashes."""
        body = read_translated_text_from_txt(txt_path, allow_empty=True)
        html = Path(html_path).read_text(encoding="utf-8-sig")
        soup = BeautifulSoup(html, "lxml")
        marker = soup.select_one('meta[name="txt-body-sha256"]')
        if marker is not None:
            stored_hash = marker.get("content")
            if isinstance(stored_hash, str) and stored_hash:
                return stored_hash == cls._body_hash(body)
        article = soup.select_one("article.novel-text")
        if article is None:
            raise ValueError("HTML does not contain the novel text element.")
        return article.get_text() == body

    @staticmethod
    def _body_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _open_in_browser(self, path: Path) -> None:
        try:
            opened = self._opener(path.resolve().as_uri())
        except Exception as exc:  # noqa: BLE001 - browser backends raise platform-specific errors.
            self._logger.warning("HTML was saved, but the browser could not be opened: %s", exc)
            return
        if not opened:
            self._logger.warning("HTML was saved, but the browser did not accept the file URL.")


__all__ = ["HtmlFormatter"]
