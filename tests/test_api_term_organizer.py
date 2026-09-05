"""Gemini term organizer tests without network access."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import GeminiFreeTierQuotaError, TermMemoryError
from translators.api_term_organizer import ApiTermOrganizer


class RecordingCall:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FreeTierFailure(Exception):
    status_code = 429

    def __init__(self) -> None:
        super().__init__(
            "Quota exceeded for generate_content_free_tier_requests. "
            "Please retry in 18.2s."
        )


def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "organize.txt"
    path.write_text("只輸出 JSON", encoding="utf-8")
    return path


def terms() -> dict[str, str]:
    return {
        "アルベルト公爵家": "阿爾貝特公爵家",
        "アルベルト家": "阿爾貝特家",
    }


def build_organizer(
    tmp_path: Path,
    call: RecordingCall,
    **kwargs: Any,
) -> ApiTermOrganizer:
    return ApiTermOrganizer(
        model="model",
        api_key="key",
        prompt_path=prompt_path(tmp_path),
        client=SimpleNamespace(models=SimpleNamespace(generate_content=call)),
        **kwargs,
    )


def test_api_receives_one_raw_batch_and_returns_structured_proposal(
    tmp_path: Path,
) -> None:
    call = RecordingCall(
        [
            SimpleNamespace(
                text=(
                    '{"groups":[{"source":"アルベルト",'
                    '"translation":"阿爾貝特",'
                    '"remove":["アルベルト公爵家","アルベルト家"]}]}'
                )
            )
        ]
    )
    organizer = build_organizer(tmp_path, call)

    result = organizer.organize(terms())

    assert result[0].source == "アルベルト"
    assert result[0].translation == "阿爾貝特"
    assert result[0].remove == ("アルベルト公爵家", "アルベルト家")
    assert json.loads(call.calls[0]["contents"]) == {"terms": terms()}
    assert call.calls[0]["config"]["response_mime_type"] == "application/json"


def test_default_batches_overlap_by_exactly_five_terms(tmp_path: Path) -> None:
    source = {f"名詞{i:04d}": f"譯名{i:04d}" for i in range(501)}
    organizer = build_organizer(tmp_path, RecordingCall([]))

    batches = organizer.batches(source)

    assert len(batches) == 2
    assert len(batches[0]) == 500
    assert len(batches[1]) == 6
    assert len(set(batches[0]) & set(batches[1])) == 5


def test_character_limit_also_creates_overlapping_batches(tmp_path: Path) -> None:
    source = {f"名詞{i}": "很長的翻譯" * 4 for i in range(8)}
    organizer = build_organizer(
        tmp_path,
        RecordingCall([]),
        max_batch_terms=20,
        max_batch_chars=85,
        overlap_terms=1,
    )

    batches = organizer.batches(source)

    assert len(batches) > 1
    assert all(
        len(set(left) & set(right)) == 1
        for left, right in pairwise(batches)
    )


@pytest.mark.parametrize("source", [{}, {"アリス": "愛麗絲"}])
def test_less_than_two_terms_do_not_call_api(
    tmp_path: Path,
    source: dict[str, str],
) -> None:
    call = RecordingCall([])
    organizer = build_organizer(tmp_path, call)
    assert organizer.batches(source) == ()
    assert organizer.organize(source) == ()
    assert call.calls == []


@pytest.mark.parametrize("response", ["not-json", "{}", '{"groups":{}}'])
def test_invalid_response_is_rejected(tmp_path: Path, response: str) -> None:
    call = RecordingCall([SimpleNamespace(text=response)])
    organizer = build_organizer(tmp_path, call)
    with pytest.raises(TermMemoryError):
        organizer.organize(terms())


def test_free_tier_quota_shows_specific_error_without_retry(tmp_path: Path) -> None:
    call = RecordingCall([FreeTierFailure()])
    organizer = build_organizer(
        tmp_path,
        call,
        retry_attempts=3,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(GeminiFreeTierQuotaError) as caught:
        organizer.organize(terms())

    assert caught.value.retry_after_seconds == 19
    assert len(call.calls) == 1
