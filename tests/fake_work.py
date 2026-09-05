"""Test-only deterministic work metadata translator."""

from __future__ import annotations

from core.models import NovelWork, TranslatedNovelWork
from translators.work_base import BaseWorkTranslator


class FakeWorkTranslator(BaseWorkTranslator):
    """Return visibly synthetic metadata without any network request."""

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "deterministic-work-v1"

    @property
    def prompt_identity(self) -> str:
        return "test-work-prompt-v1"

    def translate(self, work: NovelWork) -> TranslatedNovelWork:
        if not isinstance(work, NovelWork):
            raise TypeError("work must be a NovelWork")
        return TranslatedNovelWork(
            source_work=work,
            translated_title=f"[測試] {work.title}",
            translated_synopsis=f"[測試]\n{work.synopsis}",
            provider=self.provider,
            model=self.model,
            prompt_identity=self.prompt_identity,
        )


__all__ = ["FakeWorkTranslator"]
