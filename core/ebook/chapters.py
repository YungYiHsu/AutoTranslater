"""Ebook chapter source and translation storage."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from core.ebook.exports import page_stem
from core.shared.storage import atomic_text, write_json


def chapter_pages(chapter):
    """Read both page records and legacy rows without changing files."""
    if "pages" in chapter:
        return chapter["pages"]
    grouped = {}
    rows = sorted(chapter.get("rows", []), key=lambda r: (
        r["page"], 0 if r["line"].startswith("right_") else 1,
        tuple(int(n) for n in r["line"].split("_")[1:]),
    ))
    for row in rows:
        page = grouped.setdefault(row["page"], {"start_page": row["page"],
            "end_page": row["page"], "text": "", "records": [], "engine": row.get("engine", "yomitoku")})
        page["text"] += row["text"]
        if row["record"] not in page["records"]:
            page["records"].append(row["record"])
        if page["engine"] != row.get("engine", "yomitoku"):
            page["engine"] = "mixed"
    return list(grouped.values())


def chapter_text(chapter):
    return "".join(p["text"] for p in sorted(chapter_pages(chapter), key=lambda p: p["start_page"]))


def covers(start, end, incoming):
    cursor = start
    for page in sorted(incoming, key=lambda p: p["start_page"]):
        if page["end_page"] < cursor:
            continue
        if page["start_page"] > cursor:
            return False
        cursor = max(cursor, page["end_page"] + 1)
        if cursor > end:
            return True
    return False


def export_chapter(directory, data, record, number, first, last, *, replace=False, dry_run=False):
    plan = prepare_export(directory, data, record, number, first, last)
    if plan.conflict and not replace:
        raise FileExistsError("所選頁碼已有內容，請確認取代完整重疊範圍。")
    return plan.output if dry_run else commit_export(plan, replace=replace)


@dataclass
class ExportPlan:
    directory: Path
    chapters: dict
    index_before: bytes | None
    output: Path
    text: str
    old_translation: Path
    new_translation: Path
    copy_translation: bool
    conflict: bool


def prepare_export(directory, data, record, number, first=None, last=None):
    """Aggregate OCR rows or a Codex range, then replace only fully covered ranges."""
    directory = Path(directory)
    capture = data.get("work_capture")
    if not capture or capture["work_id"] != directory.name:
        raise ValueError("這份 OCR 紀錄尚未歸屬目前作品。")
    page_stem(capture.get("right"), capture.get("left"))
    if type(number) is not int or number < 0:
        raise ValueError("章節編號須為非負整數。")
    if data.get("engine") == "codex":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("沒有可匯出的文字。")
        incoming = [{"start_page": capture["right"],
                     "end_page": capture["left"] if capture["left"] is not None else capture["right"],
                     "text": text, "records": [record], "engine": "codex"}]
        complete = incoming if not data.get("selection_only") else []
    else:
        rows = data.get("columns", [])
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(rows):
            raise ValueError("請輸入有效的起訖行號。")
        chosen = rows[first - 1 : last]
        if any(row["status"] not in ("done", "blank") for row in chosen):
            raise ValueError("所選範圍含尚未完成或失敗的行，請先完成辨識。")
        def page_number(row):
            return capture["left"] if capture["left"] is not None and row["id"].startswith("left_") else capture["right"]
        grouped = {}
        for row in chosen:
            page = page_number(row)
            item = grouped.setdefault(page, {"start_page": page, "end_page": page,
                "text": "", "records": [record], "engine": "yomitoku"})
            item["text"] += row.get("corrected", row.get("raw", ""))
        incoming = list(grouped.values())
        complete = [p for p in incoming if all(
            first - 1 <= i < last for i, row in enumerate(rows) if page_number(row) == p["start_page"]
        )]
    path = directory / "chapters.json"
    index_before = path.read_bytes() if path.exists() else None
    chapters = json.loads(index_before.decode("utf-8")) if index_before is not None else {}
    key = str(number)
    for value in chapters.values():
        value["pages"] = chapter_pages(value)
        value.pop("rows", None)
    for other_key, other in chapters.items():
        if other_key == key:
            continue
        for old in other["pages"]:
            for page in incoming:
                start = max(old["start_page"], page["start_page"])
                end = min(old["end_page"], page["end_page"])
                if start <= end:
                    raise ValueError(
                        f"頁碼 {start:03d}–{end:03d} 已屬於第 {int(other_key):03d} 章，"
                        "同一作品的頁碼只能歸屬一個章節，無法匯出。"
                    )
    chapter = chapters.get(key, {"pages": []})
    kept, conflict = [], False
    for old in chapter["pages"]:
        overlaps = [p for p in incoming if p["start_page"] <= old["end_page"]
                    and old["start_page"] <= p["end_page"]]
        if not overlaps:
            kept.append(old)
            continue
        if not covers(old["start_page"], old["end_page"], incoming):
            raise ValueError(f"既有紀錄涵蓋 {old['start_page']:03d}–{old['end_page']:03d} 頁，請一次提供完整範圍，不能只替換其中一頁。")
        if old["start_page"] != old["end_page"] and not covers(old["start_page"], old["end_page"], complete):
            raise ValueError("替換既有多頁紀錄須提供每頁全部文字，不能只選取部分行或文字。")
        conflict = True
    chapter["pages"] = sorted(kept + incoming, key=lambda p: (p["start_page"], p["end_page"]))
    text = chapter_text(chapter)
    filename = f"{number:03d}.txt"
    old_filename = chapter.get("filename", filename)
    if Path(old_filename).name != old_filename:
        raise ValueError("章節檔名不合法。")
    old_translation = directory / "translations" / old_filename
    new_translation = directory / "translations" / filename
    if (
        old_filename != filename
        and new_translation.exists()
        and (
            not old_translation.exists()
            or new_translation.read_bytes() != old_translation.read_bytes()
        )
    ):
        raise ValueError("新章節檔名已有不同譯文，請先整理檔案。")
    if old_filename != filename and (directory / "originals" / filename).exists():
        raise ValueError("新章節檔名已有原文，請先整理檔案。")
    chapter.update(filename=filename, source_sha256=hashlib.sha256(text.encode()).hexdigest())
    chapters[key] = chapter
    return ExportPlan(
        directory,
        chapters,
        index_before,
        directory / "originals" / filename,
        text,
        old_translation,
        new_translation,
        old_filename != filename and old_translation.exists() and not new_translation.exists(),
        conflict,
    )


def commit_export(plan, *, replace=False):
    """Commit the already validated merge, rejecting changes while confirmation was open."""
    if plan.conflict and not replace:
        raise FileExistsError("所選頁碼已有內容，請確認取代完整重疊範圍。")
    directory, original = plan.directory, plan.output
    path = directory / "chapters.json"
    if (path.read_bytes() if path.exists() else None) != plan.index_before:
        raise ValueError("章節資料已變更，請重新匯出。")
    (directory / "originals").mkdir(exist_ok=True)
    targets = [original, path]
    copy_translation = plan.copy_translation
    old_translation, new_translation = plan.old_translation, plan.new_translation
    if copy_translation:
        if new_translation.exists():
            raise ValueError("新章節譯文已變更，請重新匯出。")
        targets.append(new_translation)
    backups = {p: p.read_text(encoding="utf-8") if p.exists() else None for p in targets}
    if plan.index_before is not None and any(
        "rows" in value for value in json.loads(plan.index_before.decode("utf-8")).values()
    ):
        backup = directory / ("chapters.legacy-" + hashlib.sha256(plan.index_before).hexdigest() + ".json")
        if not backup.exists():
            atomic_text(backup, plan.index_before.decode("utf-8"))
    try:
        if copy_translation:
            atomic_text(new_translation, old_translation.read_text(encoding="utf-8"))
        atomic_text(original, plan.text + "\n")
        write_json(path, plan.chapters)
    except Exception:
        for target, previous in backups.items():
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                atomic_text(target, previous)
        raise
    return original


def save_translation(directory, number, text):
    """Save one reviewed translation per chapter and record its source version."""
    directory = Path(directory)
    path = directory / "chapters.json"
    chapters = json.loads(path.read_text(encoding="utf-8"))
    chapter = chapters[str(number)]
    if not text.strip():
        raise ValueError("譯文不能為空白。")
    filename = chapter["filename"]
    if Path(filename).name != filename:
        raise ValueError("章節檔名不合法。")
    output = directory / "translations" / filename
    output.parent.mkdir(exist_ok=True)
    atomic_text(output, text.rstrip() + "\n")
    chapter["translation_source_sha256"] = chapter["source_sha256"]
    write_json(path, chapters)
    return output
