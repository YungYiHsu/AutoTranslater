"""Independent vertical text-line proposals; no full-page column-count gate."""

import copy
from itertools import pairwise

import numpy as np

from .layout import analyze_pages
from .session import upgrade_layout


def scaled_layout(layout, size):
    """Scale saved pixel geometry to a new image; default Y is 38/1200..1168/1200."""
    result = upgrade_layout(layout)
    sx = size[0] / result["width"]
    for page in ("right", "left"):
        ratios = result.get("y_ratios", {}).get(page)
        old_top = result[page + "_y"]
        old_bottom = old_top + result[page + "_height"]
        if not ratios or [round(v * result["height"]) for v in ratios] != [old_top, old_bottom]:
            ratios = [old_top / result["height"], old_bottom / result["height"]]
        result.setdefault("y_ratios", {})[page] = ratios
        top, bottom = [round(v * size[1]) for v in ratios]
        top = max(0, min(size[1] - 1, top))
        bottom = max(top + 1, min(size[1], bottom))
        result[page + "_y"], result[page + "_height"] = top, bottom - top
        result[page + "_x"] = round(result[page + "_x"] * sx * 2) / 2
        result[page + "_width"] = max(
            result[page + "_columns"], round(result[page + "_width"] * sx)
        )
        result[page + "_body_width"] = max(
            1,
            min(
                result[page + "_width"] // result[page + "_columns"],
                round(result[page + "_body_width"] * sx),
            ),
        )
    result["width"], result["height"] = size
    result.pop("line_regions", None)
    result["overrides"] = {}
    return result


def detect_lines(image, layout):
    result = upgrade_layout(layout)
    # Reuse only special-block suggestions, never the full-page acceptance decision.
    _, old_reports = analyze_pages(image, result)
    result["line_regions"] = {}
    result["overrides"] = {}
    reports = {}
    for page in ("right", "left"):
        start, end = (image.width // 2, image.width) if page == "right" else (0, image.width // 2)
        y, h, body = (result[page + "_" + k] for k in ("y", "height", "body_width"))
        annotations = copy.deepcopy(old_reports[page].get("annotations", []))
        annotations = [a for a in annotations if a["kind"] != "filled"]
        rows = []
        result["line_regions"][page] = rows
        reports[page] = {"accepted": True, "annotations": annotations}
        if not 0 <= y < y + h <= image.height or "stats" not in old_reports[page]:
            reports[page]["reason"] = "Y 範圍無效，未建立文字行。"
            continue
        stats, centers = old_reports[page]["stats"], old_reports[page]["centers"]
        glyphs, tiny = [], []
        for stat, center in zip(stats[1:], centers[1:]):
            x, _top, w, gh, area = map(int, stat)
            cx = x + w / 2 + start
            cy = center[1] + y
            if any(
                a["kind"] == "illustration"
                and a["box"][0] + 2 * body < cx < a["box"][2] - 2 * body
                and a["box"][1] <= cy <= a["box"][3]
                for a in annotations
            ):
                continue
            item = (cx, x + start, x + w + start, gh, area)
            if (
                0.42 * body <= w <= 1.65 * body
                and 0.42 * body <= gh <= 1.85 * body
                and area >= body * body * 0.045
            ):
                glyphs.append(item)
            elif 2 <= w <= body and 2 <= gh <= body and area >= 3:
                tiny.append(item)
        groups = []
        for glyph in sorted(glyphs):
            if not groups or glyph[0] - np.median([g[0] for g in groups[-1]]) > body * 0.52:
                groups.append([])
            groups[-1].append(glyph)
        for group in groups:
            cx = float(np.median([g[0] for g in group]))
            near_art = any(
                a["kind"] == "illustration" and a["box"][0] <= cx <= a["box"][2]
                for a in annotations
            )
            # Irregular isolated fragments beside art are retained as uncertain,
            # never allowed to determine pitch or displace real text.
            width = max(body, round(float(np.percentile([g[2] - g[1] for g in group], 85))) + 4)
            title = any(
                a["kind"] == "title" and a["box"][0] <= cx <= a["box"][2] for a in annotations
            )
            kind = (
                "title"
                if title
                else ("uncertain" if len(group) < 2 or (near_art and len(group) < 4) else "body")
            )
            rows.append(
                {
                    "box": [
                        max(start, round(cx - width / 2)),
                        y,
                        min(end, round(cx + width / 2)),
                        y + h,
                    ],
                    "kind": kind,
                    "support": len(group),
                }
            )
        reliable = sorted((r["box"][0] + r["box"][2]) / 2 for r in rows if r["kind"] == "body")
        pitch = None
        if len(reliable) >= 2:
            gaps = np.diff(reliable)
            adjacent = gaps[(gaps >= body) & (gaps <= body * 2.5)]
            if len(adjacent):
                pitch = float(np.median(adjacent))
        if pitch:
            proposals = []
            for left, right in pairwise(reliable):
                steps = round((right - left) / pitch)
                if 2 <= steps <= 4 and abs(right - left - steps * pitch) < pitch * 0.25:
                    proposals.extend(left + i * pitch for i in range(1, steps))
            proposals.extend((reliable[0] - pitch, reliable[-1] + pitch))
            for cx in proposals:
                if not start + body / 2 <= cx <= end - body / 2 or any(
                    abs((r["box"][0] + r["box"][2]) / 2 - cx) < pitch * 0.35 for r in rows
                ):
                    continue
                if any(
                    a["kind"] == "illustration" and a["box"][0] <= cx <= a["box"][2]
                    for a in annotations
                ):
                    continue
                evidence = any(abs(g[0] - cx) < body * 0.42 for g in tiny)
                internal = reliable[0] < cx < reliable[-1]
                if evidence or internal:
                    rows.append(
                        {
                            "box": [round(cx - body / 2), y, round(cx + body / 2), y + h],
                            "kind": "uncertain" if evidence else "filled",
                            "support": 0,
                        }
                    )
        rows.sort(key=lambda r: -(r["box"][0] + r["box"][2]))
        for row in rows:
            annotations.append({"kind": row["kind"], "box": row["box"]})
        reports[page]["reason"] = (
            f"逐行找到 {len(rows)} 個區域（含待確認／補欄），不限制 17／18 行。"
        )
        reports[page]["pitch"] = pitch
    return result, reports
