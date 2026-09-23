"""Daily persistence and accounting at real provider call boundaries."""

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.api_usage import DailyApiUsage, quota_date
from core.exceptions import InvalidLlmResponseError
from core.models import TextChunk
from tests.test_api_llm import ProviderFailure, SequenceCallable, gemini_client
from translators.api_llm import ApiLlmTranslator
from translators.api_term_organizer import ApiTermOrganizer
from translators.api_terms import ApiTermAnalyzer
from translators.api_work import ApiWorkTranslator


@pytest.mark.parametrize(("timestamp", "expected"), [
    ("2026-09-21T14:59:59+08:00", date(2026, 9, 20)),
    ("2026-09-21T15:00:00+08:00", date(2026, 9, 21)),
    ("2026-12-21T15:59:59+08:00", date(2026, 12, 20)),
    ("2026-12-21T16:00:00+08:00", date(2026, 12, 21)),
    ("2026-03-09T15:00:00+08:00", date(2026, 3, 9)),
    ("2026-11-02T15:59:59+08:00", date(2026, 11, 1)),
])
def test_pacific_quota_boundary(timestamp: str, expected: date) -> None:
    assert quota_date(datetime.fromisoformat(timestamp)) == expected


def test_reset_on_pacific_midnight_not_taiwan_midnight(tmp_path: Path) -> None:
    now = [datetime.fromisoformat("2026-09-21T23:59:59+08:00")]
    usage = DailyApiUsage(tmp_path / "usage.json", today=lambda: quota_date(now[0]))
    usage.record("flash")
    now[0] = datetime.fromisoformat("2026-09-22T00:00:00+08:00")
    assert usage.count("flash") == 1
    now[0] = datetime.fromisoformat("2026-09-22T15:00:00+08:00")
    assert usage.count("flash") == 0


def test_models_survive_restart_and_old_day_is_discarded(tmp_path: Path) -> None:
    day = [date(2026, 9, 15)]
    path = tmp_path / "usage.json"
    usage = DailyApiUsage(path, today=lambda: day[0])
    usage.record("flash")
    usage.record("flash")
    usage.record("lite")
    restarted = DailyApiUsage(path, today=lambda: day[0])
    assert restarted.count("flash") == 2
    assert restarted.count("lite") == 1
    day[0] = date(2026, 9, 16)
    assert restarted.count("flash") == 0
    assert '"models": {}' in path.read_text(encoding="utf-8")
    restarted.record("lite")
    assert restarted.count("lite") == 1


def test_retries_and_invalid_response_are_counted(tmp_path: Path) -> None:
    usage = DailyApiUsage(tmp_path / "usage.json")
    call = SequenceCallable([ProviderFailure(503), SimpleNamespace(text="譯文"),
                             SimpleNamespace(text="")])
    translator = ApiLlmTranslator(
        model="flash", api_key="test", client=gemini_client(call),
        usage=usage, retry_attempts=1, sleeper=lambda _seconds: None,
    )
    translator.translate(TextChunk(0, "原文"))
    assert usage.count("flash") == 2
    with pytest.raises(InvalidLlmResponseError):
        translator.translate(TextChunk(0, "原文"))
    assert usage.count("flash") == 3


@pytest.mark.parametrize(("factory", "method", "args"), [
    (ApiLlmTranslator, "_translate_title_once", ("標題",)),
    (ApiLlmTranslator, "_translate_once", ("正文", {})),
    (ApiWorkTranslator, "_translate_once", ("作品",)),
    (ApiTermAnalyzer, "_request_once", ("名詞",)),
    (ApiTermOrganizer, "_request_once", ("整理",)),
])
def test_every_endpoint_counts_before_provider_call(
    tmp_path: Path, factory: Any, method: str, args: tuple[Any, ...],
) -> None:
    usage = DailyApiUsage(tmp_path / "usage.json")

    def request(**_kwargs: Any) -> Any:
        assert usage.count("flash") == 1
        return SimpleNamespace(text="譯文")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=request))
    service = factory(model="flash", api_key="test", client=client, usage=usage)
    getattr(service, method)(*args)
    assert usage.count("flash") == 1


def test_broken_usage_file_does_not_stop_api(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text("broken", encoding="utf-8")
    usage = DailyApiUsage(path)
    call = SequenceCallable([SimpleNamespace(text="譯文")])
    translator = ApiLlmTranslator(
        model="flash", api_key="test", client=gemini_client(call), usage=usage,
    )
    assert translator.translate(TextChunk(0, "原文")).translated_text == "譯文"
    assert usage.count("flash") is None
    assert path.read_text(encoding="utf-8") == "broken"
