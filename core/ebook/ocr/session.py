"""Ebook capture sessions, crop geometry and local text recognition."""

import hashlib
import importlib.metadata
import json
import logging
import math
import os
import shutil
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from core.shared.storage import atomic_text, write_json

APP_DIRECTORY = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[3]
)
PRIVATE = APP_DIRECTORY / "runtime" / "ebook"
MODEL_CACHE = APP_DIRECTORY / "cache" / "ocr"
SESSIONS = PRIVATE / "ebook-sessions"
logger = logging.getLogger(__name__)


def clear_sessions(work_directory=None):
    """Delete only children of the exact session directory; reject redirected paths."""
    target = (Path(work_directory) / "ocr").absolute() if work_directory else SESSIONS.absolute()
    expected = (
        Path(work_directory).absolute() / "ocr"
        if work_directory
        else PRIVATE.absolute() / "ebook-sessions"
    )
    if work_directory and not (Path(work_directory) / "work.json").is_file():
        raise ValueError("作品資料夾不存在。")
    if target != expected or target.resolve() != target:
        raise ValueError("紀錄目錄路徑不符，已取消清除。")
    if not target.exists():
        return 0, []
    # Validate the complete tree before deleting anything, including Windows junctions.
    for directory, folders, files in os.walk(target, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in folders + files)]:
            attributes = path.lstat()
            if path.is_symlink() or getattr(attributes, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise ValueError("紀錄內含連結或重新導向目錄，已取消清除。")
    removed, failures = 0, []
    for child in list(target.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError as exc:
            failures.append(f"{child.name}：{exc}")
    return removed, failures


def default_layout():
    return {
        "layout_version": 3,
        "width": 1920,
        "height": 1200,
        "right_x": 1422.5,
        "right_y": 38,
        "right_columns": 17,
        "right_width": 829,
        "right_height": 1130,
        "right_body_width": 34,
        "left_x": 520,
        "left_y": 38,
        "left_columns": 17,
        "left_width": 836,
        "left_height": 1130,
        "left_body_width": 34,
        "overrides": {},
    }


def upgrade_layout(layout):
    """Expose old profiles in the new UI without moving their existing crop boxes."""
    if layout.get("layout_version") == 3:
        return json.loads(json.dumps(layout))
    old_regions = regions(layout, (layout["width"], layout["height"]))
    result = default_layout()
    result.update(width=layout["width"], height=layout["height"])
    result["overrides"] = {}
    for page in ("right", "left"):
        if layout.get("layout_version") == 2:
            n = layout[page + "_columns"]
            for key in ("y", "columns", "width", "height"):
                result[page + "_" + key] = layout[page + "_" + key]
            result[page + "_x"] = layout[page + "_x"] - layout[page + "_width"] / 2
            result[page + "_body_width"] = layout[page + "_width"] // n
            result["overrides"].update({f"{page}_{i + 1:02}": [] for i in range(n)})
            continue
        pitch, n = layout[page + "_pitch"], layout["columns"]
        result["overrides"].update({f"{page}_{i + 1:02}": [] for i in range(n)})
        result.update(
            {
                page + "_x": round(layout[page + "_center"] + pitch / 2) - round(pitch * n) / 2,
                page + "_y": round(layout["top"]),
                page + "_columns": n,
                page + "_width": round(pitch * n),
                page + "_height": round(layout["bottom"] - layout["top"]),
                page + "_body_width": min(round(layout["column_width"]), round(pitch * n) // n),
            }
        )
    for item in old_regions:
        result["overrides"][item["column"]].append(item["box"])
    return result


def page_bounds(layout, page):
    x, y, w, h = [layout[page + "_" + k] for k in ("x", "y", "width", "height")]
    left = round(x - w / 2) if layout.get("layout_version") == 3 else x - w
    return [left, y, left + w, y + h]


def remap_lines(previous, result, page):
    if page not in previous.get("line_regions", {}):
        return
    a, _b, c, _d = page_bounds(previous, page)
    x, y, z, bottom = page_bounds(result, page)
    for row in result["line_regions"][page]:
        l, _t, r, _end = row["box"]
        row["box"] = [
            round(x + (l - a) * (z - x) / (c - a)),
            y,
            round(x + (r - a) * (z - x) / (c - a)),
            bottom,
        ]


def drag_page(layout, page, handle, dx, dy, size, center_fixed=False):
    """Apply an original-pixel drag, bounded by image and body-width constraints."""
    result = upgrade_layout(layout)
    a, b, c, d = page_bounds(result, page)
    dx, dy = round(dx), round(dy)
    minimum = result[page + "_columns"] * result[page + "_body_width"]
    if handle == "move":
        dx = max(-a, min(dx, size[0] - c))
        dy = max(-b, min(dy, size[1] - d))
        a, c, b, d = a + dx, c + dx, b + dy, d + dy
    elif center_fixed:
        # Integer symmetric offsets preserve both the exact center and pixel edges.
        if "w" in handle or "e" in handle:
            offset = -dx if "w" in handle else dx
            lower = -((c - a - minimum) // 2)
            offset = max(lower, min(offset, a, size[0] - c))
            a, c = a - offset, c + offset
        if "n" in handle or "s" in handle:
            offset = -dy if "n" in handle else dy
            lower = -((d - b - 1) // 2)
            offset = max(lower, min(offset, b, size[1] - d))
            b, d = b - offset, d + offset
    else:
        if "w" in handle:
            a = max(0, min(a + dx, c - minimum))
        if "e" in handle:
            c = min(size[0], max(c + dx, a + minimum))
        if "n" in handle:
            b = max(0, min(b + dy, d - 1))
        if "s" in handle:
            d = min(size[1], max(d + dy, b + 1))
    result.update(
        {page + "_x": (a + c) / 2, page + "_y": b, page + "_width": c - a, page + "_height": d - b}
    )
    result["overrides"] = {
        k: v for k, v in result.get("overrides", {}).items() if not k.startswith(page + "_")
    }
    remap_lines(layout, result, page)
    regions(result, size)
    return result


def regions(layout, size):
    if layout.get("layout_version") not in (2, 3):
        return _legacy_regions(layout, size)
    if (layout["width"], layout["height"]) != tuple(size):
        raise ValueError("圖片尺寸與版面設定不同，請更新尺寸並檢查裁切框。")
    result = []
    for page in ("right", "left"):
        if page in layout.get("line_regions", {}):
            for index, row in enumerate(layout["line_regions"][page]):
                name = f"{page}_{index + 1:02}"
                boxes = layout.get("overrides", {}).get(name, [row["box"]])
                if not isinstance(boxes, list) or len(boxes) > 20:
                    raise ValueError("無效的逐行裁切框。")
                for j, box in enumerate(boxes):
                    if len(box) != 4 or any(type(v) is not int for v in box):
                        raise ValueError("逐行座標必須為四個整數。")
                    a, b, c, d = box
                    if not 0 <= a < c <= size[0] or not 0 <= b < d <= size[1]:
                        raise ValueError("逐行裁切框超出原圖。")
                    result.append(
                        {
                            "id": f"{name}_{j + 1:02}",
                            "column": name,
                            "box": box,
                            "kind": row.get("kind", "body"),
                        }
                    )
            continue
        x, y, n, w, h = [
            layout[page + "_" + key] for key in ("x", "y", "columns", "width", "height")
        ]
        if (
            any(type(v) is not int for v in (y, n, w, h))
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
        ):
            raise ValueError("X 必須為有限數值；Y、行數及框寬高必須為整數。")
        if not 1 <= n <= 100 or w < n or h < 1:
            raise ValueError("行數必須為 1–100，框寬至少等於行數，框高必須大於零。")
        if layout.get("layout_version") == 3 and x * 2 != round(x * 2):
            raise ValueError("中上 X 請使用整數或 0.5 像素。")
        left, _, right, _ = page_bounds(layout, page)
        if not (0 <= left < right <= size[0] and 0 <= y < y + h <= size[1]):
            raise ValueError(f"{page}: 頁面框超出原圖。")
        body = layout.get(page + "_body_width") if layout.get("layout_version") == 3 else None
        if body is not None and (type(body) is not int or not 1 <= body <= w // n):
            raise ValueError(f"{page}: 正文欄寬必須為 1–{w // n} 像素。")
        if layout.get("layout_version") == 3 and body is None:
            raise ValueError("缺少正文欄寬。")
        # Shared integer boundaries guarantee exhaustive coverage without gaps/overlap.
        edges = [right - round(i * w / n) for i in range(n + 1)]
        for index in range(n):
            name = f"{page}_{index + 1:02}"
            box = [edges[index + 1], y, edges[index], y + h]
            if body is not None:
                a = box[0] + (box[2] - box[0] - body) // 2
                box = [a, y, a + body, y + h]
            boxes = layout.get("overrides", {}).get(name, [box])
            if not isinstance(boxes, list) or len(boxes) > 20:
                raise ValueError(f"{name}: 無效的分段範圍。")
            bottom = -1
            for j, b in enumerate(boxes):
                if len(b) != 4 or any(type(v) is not int for v in b):
                    raise ValueError(f"{name}: 座標必須是四個整數。")
                a, t, c, d = b
                if not (0 <= a < c <= size[0] and 0 <= t < d <= size[1]) or t < bottom:
                    raise ValueError(f"{name}: 自訂框超出原圖或分段重疊。")
                bottom = d
                result.append({"id": f"{name}_{j + 1:02}", "column": name, "box": b})
    if not result and "line_regions" not in layout:
        raise ValueError("沒有可辨識區域。")
    return result


def _legacy_regions(layout, size):
    if (layout["width"], layout["height"]) != tuple(size):
        raise ValueError("圖片尺寸與版面設定不同，請更新尺寸並檢查裁切框。")
    n = int(layout["columns"])
    if n != layout["columns"] or not 1 <= n <= 100:
        raise ValueError("每頁欄數必須為 1–100 的整數。")
    if not 4 <= layout["column_width"] <= size[0]:
        raise ValueError("欄寬必須至少 4 像素。")
    if layout["right_pitch"] <= 0 or layout["left_pitch"] <= 0:
        raise ValueError("欄距必須大於零。")
    result = []
    for page in ("right", "left"):
        for index in range(n):
            name = f"{page}_{index + 1:02}"
            cx = layout[page + "_center"] - index * layout[page + "_pitch"]
            left = round(cx - layout["column_width"] / 2)
            box = [
                left,
                round(layout["top"]),
                left + round(layout["column_width"]),
                round(layout["bottom"]),
            ]
            boxes = layout.get("overrides", {}).get(name, [box])
            if not isinstance(boxes, list) or len(boxes) > 20:
                raise ValueError(f"{name}: 無效的分段範圍。")
            previous_bottom = -1
            for j, b in enumerate(boxes):
                if len(b) != 4 or any(type(v) is not int for v in b):
                    raise ValueError(f"{name}: 座標必須是四個整數。")
                x1, y1, x2, y2 = b
                if not (0 <= x1 < x2 <= size[0] and 0 <= y1 < y2 <= size[1]):
                    raise ValueError(f"{name}: 裁切範圍超出圖片。")
                if y1 < previous_bottom:
                    raise ValueError(f"{name}: 分段必須由上至下且不能重疊。")
                previous_bottom = y2
                result.append({"id": f"{name}_{j + 1:02}", "column": name, "box": b})
    if not result:
        raise ValueError("沒有可辨識區域。")
    return result


def crop_region(image, box):
    crop = image.crop(box).convert("RGB")
    ink = np.asarray(crop.convert("L")) < 180
    ys = np.flatnonzero(ink.any(axis=1))
    if not len(ys):
        return crop, True
    return crop.crop(
        (0, max(0, int(ys[0]) - 3), crop.width, min(crop.height, int(ys[-1]) + 4))
    ), False


def create_session(image, source="capture", parent=None, *, session_root=None, metadata=None):
    root = Path(session_root) if session_root is not None else SESSIONS
    root.mkdir(parents=True, exist_ok=True)
    folder = root / (
        datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    folder.mkdir()
    image.convert("RGB").save(folder / "original.png")
    data = {
        "schema": 1,
        "created": datetime.now().astimezone().isoformat(),
        "source": source,
        "parent": parent,
        "size": list(image.size),
        "status": "captured",
        "columns": [],
        "source_sha256": hashlib.sha256((folder / "original.png").read_bytes()).hexdigest(),
        "timing": {},
        "history": [],
    }
    if metadata:
        data["work_capture"] = metadata
    write_json(folder / "manifest.json", data)
    return folder, data


def save_session(folder, data, *, derived=True):
    # Derived TXT files are rebuildable; manifest is the authoritative checkpoint.
    write_json(folder / "manifest.json", data)
    if not derived:
        return
    rows = data["columns"]
    atomic_text(folder / "raw-columns.txt", "\n".join(r.get("raw", "") for r in rows) + "\n")
    atomic_text(folder / "raw-merged.txt", "".join(r.get("raw", "") for r in rows) + "\n")
    if any("corrected" in r for r in rows):
        atomic_text(
            folder / "corrected.txt",
            "".join(r.get("corrected", r.get("raw", "")) for r in rows) + "\n",
        )


def prepare_session(folder, data, image, layout):
    items = regions(layout, image.size)
    if not items:
        raise ValueError("沒有可辨識文字行，請重新偵測或新增行。")
    (folder / "crops").mkdir(exist_ok=True)
    data["layout"] = layout
    data["columns"] = []
    for item in items:
        crop, blank = crop_region(image, item["box"])
        name = item["id"] + ".png"
        crop.save(folder / "crops" / name)
        data["columns"].append(
            dict(**item, crop=name, status="blank" if blank else "pending", raw="", seconds=0)
        )
    data["status"] = "ready"
    write_json(folder / "layout.json", layout)
    save_session(folder, data)


def load_session(path, *, session_root=None):
    path = Path(path).resolve()
    # Only our local private session files are accepted; no arbitrary output paths.
    root = Path(session_root) if session_root is not None else SESSIONS
    if not path.is_relative_to(root.resolve()) or path.name != "manifest.json":
        raise ValueError("請開啟目前紀錄資料夾內的 manifest.json。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError("不支援的紀錄版本。")
    folder = path.parent
    if hashlib.sha256((folder / "original.png").read_bytes()).hexdigest() != data["source_sha256"]:
        raise ValueError("原始圖片雜湊不符，請勿覆寫原圖。")
    with Image.open(folder / "original.png") as im:
        image = im.convert("RGB")
    if list(image.size) != data["size"]:
        raise ValueError("圖片尺寸不符。")
    if data.get("layout"):
        expected = regions(data["layout"], image.size)
        if [(r["id"], r["box"]) for r in data["columns"]] != [
            (r["id"], r["box"]) for r in expected
        ]:
            raise ValueError("欄位與版面設定不符。")
        for row in data["columns"]:
            if row["crop"] != row["id"] + ".png":
                raise ValueError("無效的裁切檔名。")
    return folder, data, image


class Recognizer:
    def __init__(self):
        self.model = None

    def load(self):
        if self.model is not None:
            return 0.0
        start = time.perf_counter()
        os.environ["HF_HOME"] = str(MODEL_CACHE / "huggingface")
        os.environ["TORCH_HOME"] = str(MODEL_CACHE / "torch")
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        import torch
        from yomitoku.text_recognizer import TextRecognizer

        torch.set_num_threads(4)
        self.model = TextRecognizer(
            device="cpu", visualize=False, model_name="parseq-middle-dynw-v5"
        )
        return time.perf_counter() - start

    def recognize(self, crop):
        import cv2

        array = cv2.rotate(
            np.asarray(crop.convert("RGB"))[:, :, ::-1].copy(), cv2.ROTATE_90_COUNTERCLOCKWISE
        )
        start = time.perf_counter()
        result, _ = self.model(array)
        raw = result.model_dump()
        return "".join(raw["contents"]), time.perf_counter() - start, raw


def run_recognition(folder, data, engine, cancel, emit, indices=None, *, operation_started=None):
    """Worker owns data while running; UI consumes progress events, never mutates it."""
    start = time.perf_counter()
    targets = (
        indices
        if indices is not None
        else [i for i, r in enumerate(data["columns"]) if r["status"] not in ("done", "blank")]
    )
    data["status"] = "running"
    data.pop("error", None)
    run = {
        "started": datetime.now().astimezone().isoformat(),
        "model_load": 0,
        "recognition": 0,
        "total": 0,
        "requested": len(targets),
        "completed": 0,
    }
    data.setdefault("history", []).append(run)
    try:
        emit("status", "載入模型（首次可能較久）")
        if targets and not cancel.is_set():
            run["model_load"] = engine.load()
            data["versions"] = (
                engine.versions
                if hasattr(engine, "versions")
                else {p: importlib.metadata.version(p) for p in ("yomitoku", "torch")}
            )
        for index in targets:
            if cancel.is_set():
                break
            row = data["columns"][index]
            if row["status"] == "blank":
                continue
            # Preserve every previous result before a single-column retry.
            if row["status"] == "done":
                row.setdefault("attempts", []).append(
                    {k: row.get(k) for k in ("raw", "result", "seconds")}
                )
            fatal_error = False
            try:
                with Image.open(folder / "crops" / row["crop"]) as crop:
                    text, elapsed, result = engine.recognize(crop)
                row.update(raw=text, seconds=elapsed, result=result, status="done")
                row.pop("error", None)
                run["recognition"] += elapsed
                run["completed"] += 1
            except Exception as exc:
                logger.exception("Column recognition failed: %s", row["id"])
                row.update(status="error", error=str(exc))
                fatal_error = isinstance(exc, TimeoutError) or (
                    hasattr(engine, "process")
                    and (engine.process is None or engine.process.poll() is not None)
                )
                if fatal_error:
                    data["error"] = str(exc)
            save_session(folder, data, derived=False)
            emit("progress", {"index": index, "completed": run["completed"], "total": len(targets)})
            if fatal_error:
                break
        incomplete = any(r["status"] not in ("done", "blank") for r in data["columns"])
        data["status"] = (
            "cancelled" if cancel.is_set() else ("partial" if incomplete else "complete")
        )
    except Exception as exc:
        logger.exception("Recognition session failed")
        data.update(status="error", error=str(exc))
    finally:
        run["total"] = time.perf_counter() - start
        data["timing"] = run.copy()
        if operation_started is not None:
            data["timing"]["button_to_result_seconds"] = time.perf_counter() - operation_started
        try:
            save_session(folder, data)
        except Exception as exc:
            logger.exception("Checkpoint save failed")
            data.update(status="error", error=f"存檔失敗：{exc}")
        emit("finished", data)
