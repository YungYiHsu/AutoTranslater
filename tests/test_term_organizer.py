"""Manual proper-noun organization service tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from core.exceptions import TermMemoryError
from core.term_organizer import (
    TermOrganizationBatch,
    TermOrganizationProposal,
    TermOrganizationService,
)
from translators.organizer_base import BaseTermOrganizer


class FakeOrganizer(BaseTermOrganizer):
    def __init__(
        self,
        responses: list[tuple[TermOrganizationProposal, ...]],
        *,
        prepared_batches: tuple[dict[str, str], ...] | None = None,
    ) -> None:
        self.responses = responses
        self.prepared_batches = prepared_batches
        self.calls: list[dict[str, str]] = []

    def batches(self, terms: Mapping[str, str]) -> tuple[dict[str, str], ...]:
        if self.prepared_batches is not None:
            return self.prepared_batches
        return (dict(terms),) if len(terms) >= 2 else ()

    def organize(
        self,
        terms: Mapping[str, str],
    ) -> tuple[TermOrganizationProposal, ...]:
        self.calls.append(dict(terms))
        return self.responses.pop(0)


def write_terms(path: Path, terms: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "terms.json").write_text(
        json.dumps(terms, ensure_ascii=False),
        encoding="utf-8",
    )


def redundant_terms() -> dict[str, str]:
    return {
        "アルベルト公爵家": "阿爾貝特公爵家",
        "アルベルト公爵様": "阿爾貝特公爵大人",
        "アルベルト家": "阿爾貝特家",
        "シンフォニア王国": "辛弗尼亞王國",
    }


def proposal() -> TermOrganizationProposal:
    return TermOrganizationProposal(
        "アルベルト",
        "阿爾貝特",
        ("アルベルト公爵家", "アルベルト公爵様", "アルベルト家"),
    )


def analyze_first(
    service: TermOrganizationService,
    directory: Path,
):
    batches = service.prepare_batches(directory)
    assert batches
    return service.analyze_batch(directory, batches[0])


def test_sends_one_raw_batch_and_applies_only_after_confirmation(tmp_path: Path) -> None:
    original = redundant_terms()
    write_terms(tmp_path, original)
    organizer = FakeOrganizer([(proposal(),)])
    service = TermOrganizationService(organizer)

    batches = service.prepare_batches(tmp_path)
    assert len(batches) == 1
    assert batches[0].number == 1
    assert batches[0].total == 1
    plan = service.analyze_batch(tmp_path, batches[0])

    assert organizer.calls == [original]
    assert plan.changed
    assert plan.added == {"アルベルト": "阿爾貝特"}
    assert len(plan.removed) == 3
    assert json.loads((tmp_path / "terms.json").read_text(encoding="utf-8")) == original

    service.apply(tmp_path, plan)
    saved = json.loads((tmp_path / "terms.json").read_text(encoding="utf-8"))
    assert saved == {
        "アルベルト": "阿爾貝特",
        "シンフォニア王国": "辛弗尼亞王國",
    }


def test_next_batch_uses_terms_file_after_previous_batch_was_applied(
    tmp_path: Path,
) -> None:
    original = redundant_terms()
    prepared = (
        {
            "アルベルト公爵家": "阿爾貝特公爵家",
            "アルベルト家": "阿爾貝特家",
        },
        {
            "アルベルト家": "阿爾貝特家",
            "シンフォニア王国": "辛弗尼亞王國",
        },
    )
    first = TermOrganizationProposal(
        "アルベルト",
        "阿爾貝特",
        ("アルベルト公爵家", "アルベルト家"),
    )
    organizer = FakeOrganizer([(first,)], prepared_batches=prepared)
    service = TermOrganizationService(organizer)
    write_terms(tmp_path, original)
    batches = service.prepare_batches(tmp_path)

    first_plan = service.analyze_batch(tmp_path, batches[0])
    service.apply(tmp_path, first_plan)
    second_plan = service.analyze_batch(tmp_path, batches[1])

    assert organizer.calls == [prepared[0]]
    assert second_plan.api_requests == 0
    assert not second_plan.changed


def test_rejects_fabricated_or_out_of_batch_removal(tmp_path: Path) -> None:
    write_terms(tmp_path, redundant_terms())
    unsafe = TermOrganizationProposal(
        "アルベルト",
        "阿爾貝特",
        ("アルベルト公爵家", "シンフォニア王国"),
    )
    batch = TermOrganizationBatch(
        1,
        1,
        {
            "アルベルト公爵家": "阿爾貝特公爵家",
            "アルベルト家": "阿爾貝特家",
        },
    )
    plan = TermOrganizationService(FakeOrganizer([(unsafe,)])).analyze_batch(
        tmp_path,
        batch,
    )
    assert not plan.changed
    assert plan.rejected_proposals == 1


def test_rejects_replacing_existing_translation(tmp_path: Path) -> None:
    original = {"アリス": "愛麗絲", "アリス様": "愛麗絲大人"}
    write_terms(tmp_path, original)
    unsafe = TermOrganizationProposal(
        "アリス",
        "艾莉絲",
        ("アリス", "アリス様"),
    )
    plan = analyze_first(
        TermOrganizationService(FakeOrganizer([(unsafe,)])),
        tmp_path,
    )
    assert not plan.changed
    assert plan.rejected_proposals == 1


def test_conflicting_advice_inside_one_response_is_rejected(tmp_path: Path) -> None:
    original = {
        "アルベルト公爵家": "阿爾貝特公爵家",
        "アルベルト家": "阿爾貝特家",
        "アルベルト様": "阿爾貝特大人",
    }
    write_terms(tmp_path, original)
    suggestions = (
        TermOrganizationProposal(
            "アルベルト",
            "阿爾貝特",
            ("アルベルト公爵家", "アルベルト家"),
        ),
        TermOrganizationProposal(
            "アルベルト",
            "亞爾貝特",
            ("アルベルト家", "アルベルト様"),
        ),
    )
    plan = analyze_first(
        TermOrganizationService(FakeOrganizer([suggestions])),
        tmp_path,
    )
    assert not plan.changed
    assert plan.rejected_proposals == 2


def test_identical_advice_is_deduplicated(tmp_path: Path) -> None:
    write_terms(tmp_path, redundant_terms())
    plan = analyze_first(
        TermOrganizationService(FakeOrganizer([(proposal(), proposal())])),
        tmp_path,
    )
    assert plan.changed
    assert plan.proposals == (proposal(),)
    assert plan.rejected_proposals == 0


def test_apply_refuses_file_edited_after_preview(tmp_path: Path) -> None:
    write_terms(tmp_path, redundant_terms())
    service = TermOrganizationService(FakeOrganizer([(proposal(),)]))
    plan = analyze_first(service, tmp_path)
    write_terms(tmp_path, {**redundant_terms(), "手動追加": "手動新增"})

    with pytest.raises(TermMemoryError, match="整理期間已被修改"):
        service.apply(tmp_path, plan)


@pytest.mark.parametrize("terms", [{}, {"アリス": "愛麗絲"}])
def test_less_than_two_terms_creates_no_batches(
    tmp_path: Path,
    terms: dict[str, str],
) -> None:
    write_terms(tmp_path, terms)
    organizer = FakeOrganizer([])
    service = TermOrganizationService(organizer)
    assert service.estimate_requests(tmp_path) == 0
    assert service.prepare_batches(tmp_path) == ()
    assert organizer.calls == []
