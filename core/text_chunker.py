"""Lossless, boundary-aware splitting of novel text for translation APIs."""

from __future__ import annotations

import re
from collections.abc import Iterator
from re import Pattern

from core.exceptions import ChunkingError
from core.models import TextChunk

_PARAGRAPH_BOUNDARY = re.compile(r"\n{2,}")
_LINE_BOUNDARY = re.compile(r"\n+")
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?]+[」』）》】〕〗〙〛”’]*")
_SPACE_BOUNDARY = re.compile(r"[ \t]+")


class TextChunker:
    """Split text without changing characters, whitespace, or ordering."""

    def __init__(self, max_chars: int) -> None:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        self._max_chars = max_chars

    @property
    def max_chars(self) -> int:
        """Return the hard character limit applied to every chunk."""
        return self._max_chars

    def split(self, text: str) -> tuple[TextChunk, ...]:
        """Return ordered chunks whose concatenation exactly equals the input text."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text.strip():
            raise ChunkingError("text must contain at least one non-whitespace character")

        semantic_units: list[str] = []
        for paragraph in self._split_at_boundaries(text, _PARAGRAPH_BOUNDARY):
            semantic_units.extend(self._fit_unit(paragraph))

        chunk_texts = self._pack_units(semantic_units)
        chunks = tuple(
            TextChunk(index=index, text=value) for index, value in enumerate(chunk_texts)
        )
        self._verify_result(text, chunks)
        return chunks

    def _fit_unit(self, unit: str) -> list[str]:
        if len(unit) <= self._max_chars:
            return [unit]

        parts = [unit]
        for boundary in (_LINE_BOUNDARY, _SENTENCE_BOUNDARY, _SPACE_BOUNDARY):
            next_parts: list[str] = []
            for part in parts:
                if len(part) <= self._max_chars:
                    next_parts.append(part)
                else:
                    next_parts.extend(self._split_at_boundaries(part, boundary))
            parts = next_parts

        fitted: list[str] = []
        for part in parts:
            if len(part) <= self._max_chars:
                fitted.append(part)
            else:
                fitted.extend(
                    part[offset : offset + self._max_chars]
                    for offset in range(0, len(part), self._max_chars)
                )
        return fitted

    def _pack_units(self, units: list[str]) -> list[str]:
        packed: list[str] = []
        current = ""

        for unit in units:
            if not unit:
                continue
            if len(unit) > self._max_chars:
                raise ChunkingError("internal chunking error: a unit exceeded max_chars")

            if len(current) + len(unit) <= self._max_chars:
                current += unit
                continue

            if current:
                packed.append(current)
            current = unit

        if current:
            packed.append(current)
        return self._rebalance_whitespace_parts(packed)

    def _rebalance_whitespace_parts(self, parts: list[str]) -> list[str]:
        """Attach formatting-only parts to nearby text without changing order."""
        index = 0
        while index < len(parts):
            if parts[index].strip():
                index += 1
                continue

            if index > 0 and len(parts[index - 1]) + len(parts[index]) <= self._max_chars:
                parts[index - 1] += parts.pop(index)
                continue
            if (
                index + 1 < len(parts)
                and len(parts[index]) + len(parts[index + 1]) <= self._max_chars
            ):
                parts[index : index + 2] = [parts[index] + parts[index + 1]]
                continue
            if index > 0 and self._borrow_from_previous(parts, index):
                index += 1
                continue
            if index + 1 < len(parts) and self._borrow_from_next(parts, index):
                index += 1
                continue
            raise ChunkingError(
                "text contains a whitespace-only region too large to attach to a valid chunk"
            )
        return parts

    def _borrow_from_previous(self, parts: list[str], index: int) -> bool:
        previous = parts[index - 1]
        capacity = self._max_chars - len(parts[index])
        for size in range(1, min(capacity, len(previous) - 1) + 1):
            remaining = previous[:-size]
            moved = previous[-size:]
            if remaining.strip() and moved.strip():
                parts[index - 1] = remaining
                parts[index] = moved + parts[index]
                return True
        return False

    def _borrow_from_next(self, parts: list[str], index: int) -> bool:
        following = parts[index + 1]
        capacity = self._max_chars - len(parts[index])
        for size in range(1, min(capacity, len(following) - 1) + 1):
            moved = following[:size]
            remaining = following[size:]
            if moved.strip() and remaining.strip():
                parts[index] += moved
                parts[index + 1] = remaining
                return True
        return False

    @staticmethod
    def _split_at_boundaries(text: str, boundary: Pattern[str]) -> Iterator[str]:
        start = 0
        for match in boundary.finditer(text):
            end = match.end()
            if end > start:
                yield text[start:end]
            start = end
        if start < len(text):
            yield text[start:]

    def _verify_result(self, source: str, chunks: tuple[TextChunk, ...]) -> None:
        if not chunks:
            raise ChunkingError("chunking produced no output")
        if any(chunk.char_count > self._max_chars for chunk in chunks):
            raise ChunkingError("chunking produced a chunk larger than max_chars")
        if "".join(chunk.text for chunk in chunks) != source:
            raise ChunkingError("chunking changed the source text")


__all__ = ["TextChunker"]
