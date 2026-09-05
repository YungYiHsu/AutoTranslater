"""Incremental previous/next navigation for translated chapter HTML files."""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from urllib.parse import quote

from formatters.utils import atomic_write_text

_NAV_START = "<!-- AUTOTRANSLATER_NAV_START -->"
_NAV_END = "<!-- AUTOTRANSLATER_NAV_END -->"
_NAV_PATTERN = re.compile(
    rf"{re.escape(_NAV_START)}.*?{re.escape(_NAV_END)}",
    re.DOTALL,
)
_ARTICLE_END_PATTERN = re.compile(r"</article\s*>", re.IGNORECASE)


def preserve_navigation_block(existing_html: str, rendered_html: str) -> str:
    """Carry the program-owned navigation block across正文-only HTML regeneration."""
    existing = _NAV_PATTERN.search(existing_html)
    if existing is None or _NAV_PATTERN.search(rendered_html) is None:
        return rendered_html
    return _NAV_PATTERN.sub(lambda _match: existing.group(0), rendered_html, count=1)


class HtmlNavigationManager:
    """Update only marked navigation blocks without rebuilding chapter正文."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("novel_translator.formatter.html_navigation")

    def refresh_all(self, chapters: dict[int, Path]) -> int:
        """Refresh every indexed translated HTML page after one work scan."""
        ordered = sorted(chapters)
        changed = 0
        for index, number in enumerate(ordered):
            previous = ordered[index - 1] if index > 0 else None
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            changed += self._update(chapters, number, previous, following)
        return changed

    def refresh_neighbors(self, chapters: dict[int, Path], chapter_number: int) -> int:
        """Refresh a newly completed page and only its nearest old neighbors."""
        ordered = sorted(chapters)
        try:
            index = ordered.index(chapter_number)
        except ValueError:
            return 0
        targets = {chapter_number}
        if index > 0:
            targets.add(ordered[index - 1])
        if index + 1 < len(ordered):
            targets.add(ordered[index + 1])

        changed = 0
        for number in targets:
            position = ordered.index(number)
            previous = ordered[position - 1] if position > 0 else None
            following = ordered[position + 1] if position + 1 < len(ordered) else None
            changed += self._update(chapters, number, previous, following)
        return changed

    def _update(
        self,
        chapters: dict[int, Path],
        number: int,
        previous: int | None,
        following: int | None,
    ) -> int:
        path = chapters[number]
        try:
            original = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            self._logger.warning("Skipped HTML navigation update for %s: %s", path, exc)
            return 0

        navigation = self._navigation_block(chapters, previous, following)
        if _NAV_PATTERN.search(original):
            updated = _NAV_PATTERN.sub(lambda _match: navigation, original, count=1)
        else:
            article_end = _ARTICLE_END_PATTERN.search(original)
            if article_end is None:
                self._logger.warning(
                    "Skipped HTML navigation update because </article> is missing: %s",
                    path,
                )
                return 0
            updated = (
                original[: article_end.end()]
                + "\n\n    "
                + navigation
                + original[article_end.end() :]
            )
        if updated == original:
            return 0
        try:
            atomic_write_text(path, updated, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self._logger.warning("Could not save HTML navigation for %s: %s", path, exc)
            return 0
        return 1

    @staticmethod
    def _navigation_block(
        chapters: dict[int, Path],
        previous: int | None,
        following: int | None,
    ) -> str:
        previous_item = HtmlNavigationManager._navigation_item(
            chapters, previous, "previous"
        )
        following_item = HtmlNavigationManager._navigation_item(
            chapters, following, "next"
        )
        return (
            f"{_NAV_START}\n"
            '<nav class="chapter-navigation" aria-label="翻譯章節導覽" '
            'style="display:flex;justify-content:space-between;gap:1rem;'
            'margin-top:3rem;padding-top:1.2rem;border-top:1px solid var(--border);'
            'font-family:system-ui,sans-serif;font-size:.9rem">\n'
            f"  {previous_item}\n"
            f"  {following_item}\n"
            "</nav>\n"
            f"{_NAV_END}"
        )

    @staticmethod
    def _navigation_item(
        chapters: dict[int, Path], number: int | None, direction: str
    ) -> str:
        if number is None:
            label = "沒有上一章" if direction == "previous" else "沒有下一章"
            return (
                '<span class="chapter-navigation-disabled" '
                f'style="color:var(--muted);opacity:.65">{label}</span>'
            )
        filename = quote(chapters[number].name)
        href = html.escape(filename, quote=True)
        label = f"← 第 {number} 章" if direction == "previous" else f"第 {number} 章 →"
        return f'<a rel="{direction}" href="{href}">{label}</a>'


__all__ = ["HtmlNavigationManager", "preserve_navigation_block"]
