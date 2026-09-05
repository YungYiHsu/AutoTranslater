"""Tests for abstract strategy contracts."""

from __future__ import annotations

import inspect

import pytest

from extractors.base import BaseExtractor
from formatters.base import BaseFormatter
from translators.base import BaseTranslator


@pytest.mark.parametrize("strategy", [BaseExtractor, BaseTranslator, BaseFormatter])
def test_strategy_contracts_are_abstract(strategy: type[object]) -> None:
    assert inspect.isabstract(strategy)
    with pytest.raises(TypeError):
        strategy()
