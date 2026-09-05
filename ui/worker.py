"""Background controller execution that never touches Tk widgets."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from core.chapter_progress import ChapterCompletionTracker
from core.config import MissingApiKeyError
from core.controller import TranslationController, TranslationPlan
from core.exceptions import TranslationCancelled, WorkSetupCancelled
from core.models import NovelWork
from core.term_organizer import (
    TermOrganizationBatch,
    TermOrganizationPlan,
    TermOrganizationService,
)
from core.work_setup import WorkSetupResult, WorkSetupService
from extractors.work_base import BaseWorkExtractor
from ui.messages import WorkerMessage


class TranslationWorker:
    """Run prepare/translation work on one daemon thread and publish messages."""

    def __init__(self) -> None:
        self.messages: queue.Queue[WorkerMessage] = queue.Queue()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_prepare(self, controller: TranslationController, url: str) -> None:
        self._start(self._prepare, controller, url)

    def start_select_work(
        self,
        extractor: BaseWorkExtractor,
        setup_service: WorkSetupService,
        url: str,
        requested_chapter: int | None = None,
        *,
        force_refresh: bool = False,
    ) -> None:
        self._start(
            self._select_work, extractor, setup_service, url, requested_chapter, force_refresh
        )

    def start_setup_work(
        self,
        setup_service: WorkSetupService,
        work: NovelWork,
        *,
        allow_overwrite: bool = False,
    ) -> None:
        self._start(self._setup_work, setup_service, work, allow_overwrite)

    def start_run(
        self,
        controller: TranslationController,
        plan: TranslationPlan,
        completion_tracker: ChapterCompletionTracker | None = None,
        work_setup: WorkSetupResult | None = None,
    ) -> None:
        self._start(self._run, controller, plan, completion_tracker, work_setup)

    def start_organize_terms(
        self,
        service: TermOrganizationService,
        work_directory: Path,
        batch: TermOrganizationBatch,
    ) -> None:
        self._start(self._organize_terms, service, work_directory, batch)

    def start_apply_term_organization(
        self,
        service: TermOrganizationService,
        work_directory: Path,
        plan: TermOrganizationPlan,
    ) -> None:
        self._start(self._apply_term_organization, service, work_directory, plan)

    def cancel(self) -> None:
        self._cancel_event.set()

    def _start(self, target: object, *args: object) -> None:
        if self.is_running:
            raise RuntimeError("A translation task is already running.")
        self._cancel_event.clear()
        self._thread = threading.Thread(
            target=target,  # type: ignore[arg-type]
            args=args,
            daemon=True,
            name="novel-translator-worker",
        )
        self._thread.start()

    def _prepare(self, controller: TranslationController, url: str) -> None:
        try:
            plan = controller.prepare(url)
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))
            return
        self.messages.put(WorkerMessage("prepare_done", plan))

    def _select_work(
        self,
        extractor: BaseWorkExtractor,
        setup_service: WorkSetupService,
        url: str,
        requested_chapter: int | None,
        force_refresh: bool,
    ) -> None:
        try:
            self.messages.put(WorkerMessage("work_progress", "fetching_work"))
            work = extractor.extract_with_progress(
                url,
                progress=lambda completed, total, source: self.messages.put(
                    WorkerMessage("catalog_progress", (completed, total, source))
                ),
                force_refresh=force_refresh,
            )
            if requested_chapter is not None:
                work = extractor.resolve_chapter(work, requested_chapter)
            if self._cancel_event.is_set():
                self.messages.put(WorkerMessage("cancelled"))
                return
            self.messages.put(WorkerMessage("work_found", work))
            self._prepare_work_memory(setup_service, work, allow_overwrite=False)
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))

    def _setup_work(
        self,
        setup_service: WorkSetupService,
        work: NovelWork,
        allow_overwrite: bool,
    ) -> None:
        try:
            self._prepare_work_memory(setup_service, work, allow_overwrite)
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))

    def _prepare_work_memory(
        self,
        setup_service: WorkSetupService,
        work: NovelWork,
        allow_overwrite: bool,
    ) -> None:
        if self._cancel_event.is_set():
            self.messages.put(WorkerMessage("cancelled"))
            return
        try:
            result = setup_service.prepare(
                work,
                progress=lambda stage: self.messages.put(WorkerMessage("work_progress", stage)),
                confirm_overwrite=(lambda _path: True) if allow_overwrite else None,
            )
        except MissingApiKeyError:
            self.messages.put(WorkerMessage("api_key_required", work))
            return
        except WorkSetupCancelled:
            self.messages.put(WorkerMessage("work_overwrite_required", work))
            return
        if self._cancel_event.is_set():
            self.messages.put(WorkerMessage("cancelled"))
            return
        self.messages.put(WorkerMessage("work_setup_done", result))

    def _run(
        self,
        controller: TranslationController,
        plan: TranslationPlan,
        completion_tracker: ChapterCompletionTracker | None,
        work_setup: WorkSetupResult | None,
    ) -> None:
        try:
            result = controller.run(
                plan,
                progress=lambda update: self.messages.put(WorkerMessage("progress", update)),
                should_cancel=self._cancel_event.is_set,
            )
        except TranslationCancelled:
            self.messages.put(WorkerMessage("cancelled"))
            return
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))
            return
        if completion_tracker is not None and work_setup is not None:
            try:
                self.messages.put(WorkerMessage("completion_progress", "validating_outputs"))
                chapter_progress = completion_tracker.record_completed(work_setup, result)
                self.messages.put(WorkerMessage("chapter_progress", chapter_progress))
            except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
                self.messages.put(WorkerMessage("error", exc))
                return
        self.messages.put(WorkerMessage("run_done", result))

    def _organize_terms(
        self,
        service: TermOrganizationService,
        work_directory: Path,
        batch: TermOrganizationBatch,
    ) -> None:
        try:
            plan = service.analyze_batch(
                work_directory,
                batch,
                progress=lambda stage: self.messages.put(
                    WorkerMessage("term_organization_progress", stage)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))
            return
        if self._cancel_event.is_set():
            self.messages.put(WorkerMessage("cancelled"))
            return
        self.messages.put(WorkerMessage("term_organization_done", plan))

    def _apply_term_organization(
        self,
        service: TermOrganizationService,
        work_directory: Path,
        plan: TermOrganizationPlan,
    ) -> None:
        try:
            path = service.apply(work_directory, plan)
        except Exception as exc:  # noqa: BLE001 - forwarded safely to the GUI thread.
            self.messages.put(WorkerMessage("error", exc))
            return
        self.messages.put(WorkerMessage("term_organization_applied", path))


__all__ = ["TranslationWorker"]
