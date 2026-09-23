"""Safety overrides reach every endpoint and cannot affect frozen builds."""

import sys
from types import SimpleNamespace

import pytest
from google.genai import types

from core.gemini_settings import request_config
from developer.settings import CATEGORIES, THRESHOLDS, get_overrides, set_overrides
from translators.api_llm import ApiLlmTranslator
from translators.api_term_organizer import ApiTermOrganizer
from translators.api_terms import ApiTermAnalyzer
from translators.api_work import ApiWorkTranslator


@pytest.fixture(autouse=True)
def restore_settings():
    previous = get_overrides()
    set_overrides({})
    yield
    set_overrides(previous)


@pytest.mark.parametrize(("factory", "method", "args"), [
    (ApiLlmTranslator, "_translate_title_once", ("標題",)),
    (ApiLlmTranslator, "_translate_once", ("正文", {})),
    (ApiWorkTranslator, "_translate_once", ("作品",)),
    (ApiTermAnalyzer, "_request_once", ("名詞",)),
    (ApiTermOrganizer, "_request_once", ("整理",)),
])
def test_safety_reaches_every_endpoint(factory, method, args):
    settings = {category: "OFF" for category in CATEGORIES}
    set_overrides(settings)
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="譯文")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=request))
    service = factory(model="test", api_key="test", client=client)
    getattr(service, method)(*args)
    actual = calls[0]["config"]
    assert actual["safety_settings"] == [
        {"category": category, "threshold": "OFF"} for category in CATEGORIES
    ]
    assert actual["temperature"] in {0.1, 0.2}
    if factory != ApiLlmTranslator:
        assert actual["response_mime_type"] == "application/json"
    if factory == ApiTermAnalyzer:
        assert actual["system_instruction"]


@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_choices_are_valid_sdk_settings(threshold):
    set_overrides({category: threshold for category in CATEGORIES})
    config = types.GenerateContentConfig(**request_config({"temperature": 0.2}))
    if threshold == "模型預設":
        assert not config.safety_settings
    else:
        assert len(config.safety_settings) == 4
        assert all(setting.threshold == threshold for setting in config.safety_settings)


def test_frozen_build_ignores_developer_settings(monkeypatch):
    set_overrides({category: "BLOCK_NONE" for category in CATEGORIES})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert request_config({"temperature": 0.2}) == {"temperature": 0.2}


def test_reset_and_invalid_setting_preserve_valid_state():
    category = next(iter(CATEGORIES))
    set_overrides({category: "BLOCK_NONE"})
    with pytest.raises(ValueError):
        set_overrides({category: "invalid"})
    assert get_overrides() == {category: "BLOCK_NONE"}
    set_overrides({category: "模型預設"})
    assert request_config({}) == {}
