"""Interchangeable translation strategies."""

from translators.api_llm import ApiLlmTranslator
from translators.api_term_organizer import ApiTermOrganizer
from translators.api_terms import ApiTermAnalyzer
from translators.api_work import ApiWorkTranslator
from translators.base import BaseTranslator
from translators.term_base import BaseTermAnalyzer
from translators.work_base import BaseWorkTranslator

__all__ = [
    "ApiLlmTranslator",
    "ApiTermAnalyzer",
    "ApiTermOrganizer",
    "ApiWorkTranslator",
    "BaseTermAnalyzer",
    "BaseTranslator",
    "BaseWorkTranslator",
]
