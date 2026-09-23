"""Sequential chapter scheduling without independent batch checkpoints."""

from collections.abc import Callable
from dataclasses import replace

from core.controller import ChapterExecutionOptions, TranslationController
from core.exceptions import TranslationCancelled
from core.models import NovelChapterEntry, NovelWork


def select_batch(work: NovelWork, start: int, end: int,
                 completed: frozenset[int], redo: bool) -> tuple[NovelChapterEntry, ...]:
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ValueError("請輸入有效的起始與結束章節。")
    entries = {entry.number: entry for entry in work.chapters}
    if end > max(entries) or start < min(entries):
        raise ValueError("章節範圍超出作品目錄。")
    missing = [n for n in range(start, end + 1) if n not in entries]
    if missing:
        raise ValueError(f"目錄缺少章節：{missing}")
    return tuple(entries[n] for n in range(start, end + 1) if redo or n not in completed)


def run_batch(entries, *, factory: Callable[[NovelChapterEntry], TranslationController],
              options: ChapterExecutionOptions, force_numbers: frozenset[int],
              tracker, setup, emit, cancelled, attempts: int) -> None:
    """Emit ordered events; errors propagate and never start the next chapter."""
    for index, entry in enumerate(entries, 1):
        if cancelled():
            raise TranslationCancelled()
        emit("chapter", (index, entry))
        controller = factory(entry)
        plan = controller.prepare(entry.source_url)
        if cancelled():
            raise TranslationCancelled()
        if entry.number in force_numbers:
            controller.clear_checkpoint_components(plan, options)
            plan = replace(plan,
                           completed_chunks=() if options.translate_body else plan.completed_chunks,
                           translated_chapter_title=None if options.translate_title else plan.translated_chapter_title,
                           completed_term_indexes=frozenset() if options.update_terms else plan.completed_term_indexes)
        jobs = ((plan.pending_title_count if options.translate_title else 0)
                + (plan.pending_count if options.translate_body else 0)
                + (plan.pending_term_count if options.update_terms else 0))
        emit("prepared", (controller, plan, jobs * attempts))
        result = controller.run(plan, options=options,
                                progress=lambda update: emit("progress", update),
                                should_cancel=cancelled)
        if result.term_memory_error:
            raise ValueError(result.term_memory_error)
        progress = tracker.record_completed(setup, result)
        emit("completed", (result, progress))
