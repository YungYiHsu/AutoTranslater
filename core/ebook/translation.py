"""Local chapter adapter for the shared translation pipeline and reading outputs."""

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from core.shared.models import NovelChapter
from core.shared.reading_lines import split_for_reading
from core.shared.storage import atomic_text, write_json
from core.shared.translation_bootstrap import build_translation_controller
from core.shared.translation_controller import ChapterExecutionOptions
from extractors.base import BaseExtractor
from formatters.base import BaseFormatter
from formatters.html_formatter import HtmlFormatter
from formatters.html_navigation import HtmlNavigationManager
from formatters.txt_formatter import TxtFormatter
from formatters.utils import merge_translated_text


def source_hash(text):
    return hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChapterSource:
    directory: Path
    key: str
    metadata: dict
    chapter: NovelChapter
    digest: str
    has_title: bool

    def assert_current(self):
        current = load_source(self.directory, self.key)
        if current.chapter != self.chapter or current.metadata != self.metadata:
            raise ValueError("原文或頁碼資料已變更，請重新分析章節；已完成的分段仍保留。")


def load_source(directory, key):
    directory = Path(directory)
    key = str(int(key))
    if int(key) < 0:
        raise ValueError("章節不能小於 0。")
    index = json.loads((directory / "chapters.json").read_text(encoding="utf-8"))
    metadata = index[key]
    filename = metadata["filename"]
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".txt"):
        raise ValueError("章節檔名不合法。")
    text = (directory / "originals" / filename).read_text(encoding="utf-8-sig").rstrip()
    work = json.loads((directory / "work.json").read_text(encoding="utf-8"))
    title = str(metadata.get("title") or "").strip()
    chapter = NovelChapter(
        title=work["title"], chapter_title=title or f"第 {int(key):03d} 章",
        source_url=work["url"] + f"#chapter-{key}", original_text=text, chapter_number=int(key),
    )
    # Translation bookkeeping is not source data and must not invalidate a snapshot.
    source_metadata = {k: v for k, v in metadata.items() if not k.startswith("translation_")}
    return ChapterSource(directory, key, source_metadata, chapter, source_hash(text), bool(title))


class LocalChapterExtractor(BaseExtractor):
    def __init__(self, source):
        self.source = source

    def can_handle(self, url):
        return url == self.source.chapter.source_url

    def extract(self, url):
        if not self.can_handle(url):
            raise ValueError("所選章節已變更。")
        self.source.assert_current()
        return self.source.chapter


class EbookOutputFormatter(BaseFormatter):
    """Render both formats before committing; preserve previous outputs on failure."""

    def __init__(self, source, *, overwrite=False):
        self.source = source
        self.overwrite = overwrite
        self.paths = ()
        folder = source.directory / "translations"
        self.before_outputs = {
            folder / f"{int(source.key):03d}.{ext}":
            (folder / f"{int(source.key):03d}.{ext}").read_bytes()
            if (folder / f"{int(source.key):03d}.{ext}").exists() else None
            for ext in ("txt", "html")
        }

    def save(self, chapter, output_dir):
        source = self.source
        folder = source.directory / "translations"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{int(source.key):03d}"
        txt, html = folder / f"{stem}.txt", folder / f"{stem}.html"
        if not self.overwrite and (txt.exists() or html.exists()):
            raise FileExistsError("此章已有譯文，請先確認覆蓋。")
        before_outputs = self.before_outputs
        with TemporaryDirectory(prefix=".translation-", dir=folder) as temporary:
            staging = Path(temporary)
            body = split_for_reading(merge_translated_text(chapter)).text
            rendered_txt = TxtFormatter().save(
                chapter, staging, destination_path=staging / txt.name, body_text=body,
            )
            rendered_html = HtmlFormatter(auto_open=False).save(
                chapter, staging, txt_path=rendered_txt, destination_path=staging / html.name,
            )
            source.assert_current()
            index_path = source.directory / "chapters.json"
            index_before = index_path.read_text(encoding="utf-8")
            index = json.loads(index_before)
            entry = index[source.key]
            entry.update(translation_source_sha256=source.digest,
                         translation_provider=chapter.provider, translation_model=chapter.model,
                         translation_title=chapter.translated_chapter_title,
                         translation_html=html.name)
            if any((p.read_bytes() if p.exists() else None) != value for p, value in before_outputs.items()):
                raise ValueError("譯文檔案已變更，請重新確認覆蓋。")
            try:
                atomic_text(txt, rendered_txt.read_text(encoding="utf-8"))
                atomic_text(html, rendered_html.read_text(encoding="utf-8"))
                source.assert_current()
                if index_path.read_text(encoding="utf-8") != index_before:
                    raise ValueError("章節索引已變更，請重新匯出譯文。")
                write_json(index_path, index)
            except Exception:
                for path, old in before_outputs.items():
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_text(path, old.decode("utf-8"))
                raise
        self.paths = (txt, html)
        HtmlNavigationManager().refresh_neighbors(
            {int(p.stem): p for p in folder.glob("*.html") if p.stem.isascii() and p.stem.isdigit()},
            int(source.key),
        )
        return html


class EbookTranslation:
    """One immutable source/config/prompt plan; no website extraction or model calls in prepare."""

    def __init__(self, directory, key, *, config, mode, prompt_path, api_key=None,
                 usage=None, overwrite=False):
        self.source = load_source(directory, key)
        self.mode = mode
        self.config = config
        self.formatter = EbookOutputFormatter(self.source, overwrite=overwrite)
        self.controller = build_translation_controller(
            config=config, mode=mode, output_directory=self.source.directory,
            checkpoint_directory=self.source.directory / "translation-checkpoints",
            work_directory=self.source.directory,
            controller_work_directory=self.source.directory,
            chapter_extractor=LocalChapterExtractor(self.source),
            formatters=(self.formatter,), auto_open=False, overwrite_outputs=overwrite,
            api_key=api_key, usage=usage, prompt_path=prompt_path,
            use_term_memory=mode not in {"codex", "codex_analysis"},
        )
        self.plan = self.controller.prepare(self.source.chapter.source_url)

    def resegment(self, count):
        self.plan = self.controller.resegment(self.plan, count)
        return self.plan

    def request_estimate(self, *, force=False, translate_title=True, update_terms=True):
        title = (1 if force else self.plan.pending_title_count) if translate_title and self.source.has_title else 0
        body = self.plan.total_chunks if force else self.plan.pending_count
        terms = (self.plan.total_chunks if force else self.plan.pending_term_count) if update_terms and self.mode in {"analysis", "gemini"} else 0
        attempts = 1 if self.mode in {"codex", "codex_analysis"} else self.config.retry_attempts + 1
        return title, body, terms, (title + body + terms) * attempts

    def run(self, *, force=False, translate_title=True, update_terms=True,
            progress=None, should_cancel=None):
        if self.mode not in {"codex", "gemini"}:
            raise ValueError("分析模式不會發送翻譯。")
        self.source.assert_current()
        options = ChapterExecutionOptions(
            translate_title=translate_title and self.source.has_title,
            update_terms=update_terms,
        )
        if force:
            self.controller.clear_checkpoint_components(self.plan, options)
            self.plan = self.controller.resegment(self.plan, self.plan.total_chunks)
        result = self.controller.run(self.plan, options=options, progress=progress, should_cancel=should_cancel)
        return replace(result, output_paths=self.formatter.paths)
