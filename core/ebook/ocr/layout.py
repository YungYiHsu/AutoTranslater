"""Conservative, local-only page X estimation. Never runs OCR or changes Y."""

import copy

import numpy as np

from .session import upgrade_layout


def analyze_pages(image, layout):
    """Compute grayscale, components, artwork and title proposals once per image."""
    import cv2

    result = upgrade_layout(layout)
    gray = np.asarray(image.convert("L"))
    reports = {}
    for page in ("right", "left"):
        annotations = []

        def reject(reason, page=page, annotations=annotations):
            reports[page] = {"accepted": False, "reason": reason, "annotations": annotations}

        y = result[page + "_y"]
        height = result[page + "_height"]
        body = result[page + "_body_width"]
        if y < 0 or height < 1 or y + height > image.height or body < 1:
            reject("Y 範圍、行數或正文欄寬不適合自動定位，請先調整。")
            continue
        start, end = (image.width // 2, image.width) if page == "right" else (0, image.width // 2)
        mask = (gray[y : y + height, start:end] < 180).astype(np.uint8)
        # Broad gray-tone regions are illustration candidates, not automatic deletions.
        patch = gray[y : y + height, start:end]
        tones = ((patch > 35) & (patch < 235)).astype(np.uint8)
        tile = max(12, round(body))
        density = cv2.blur(tones.astype(np.float32), (tile, tile))
        art_mask = (density > 0.28).astype(np.uint8)
        art_mask = cv2.morphologyEx(art_mask, cv2.MORPH_CLOSE, np.ones((tile, tile), np.uint8))
        _, _, art_stats, _ = cv2.connectedComponentsWithStats(art_mask, connectivity=8)
        artwork = []
        for ax, ay, aw, ah, area in art_stats[1:]:
            if aw > body * 4 and ah > body * 6 and area > body * body * 20:
                box = [int(ax + start), int(ay + y), int(ax + aw + start), int(ay + ah + y)]
                artwork.append(box)
                annotations.append({"kind": "illustration", "box": box})
        _, _, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
        reports[page] = {
            "annotations": annotations,
            "stats": stats,
            "centers": centers,
            "groups": [],
            "titles": [],
        }
        glyphs = []
        for stat, center in zip(stats[1:], centers[1:]):
            _x, top, w, h, area = map(int, stat)
            if any(
                a + body * 2 <= center[0] + start <= c - body * 2 and b <= center[1] + y <= d
                for a, b, c, d in artwork
            ):
                continue
            if (
                0.45 * body <= w <= 1.6 * body
                and 0.45 * body <= h <= 1.8 * body
                and area >= body * body * 0.06
            ):
                glyphs.append((float(center[0]), top, top + h, w, h))
        if not glyphs:
            continue
        groups = []
        for glyph in sorted(glyphs):
            if not groups or glyph[0] - np.median([g[0] for g in groups[-1]]) > body * 0.55:
                groups.append([])
            groups[-1].append(glyph)
        typical_height = float(np.median([g[4] for g in glyphs]))
        titles = [
            group
            for group in groups
            if len(group) >= 4 and np.median([g[4] for g in group]) > typical_height * 1.12
        ]
        for group in titles:
            center = float(np.median([g[0] for g in group])) + start
            width = max(g[3] for g in group) + 6
            annotations.append(
                {
                    "kind": "title",
                    "box": [
                        max(start, round(center - width / 2)),
                        y + min(g[1] for g in group),
                        min(end, round(center + width / 2)),
                        y + max(g[2] for g in group),
                    ],
                }
            )
        reports[page].update(groups=groups, titles=titles)
    return result, reports


def estimate_page_x(image, layout, infer_columns=False):
    """Legacy grid proposal for saved fixed-column layouts."""
    result, analysis = analyze_pages(image, layout)
    reports = {}
    for page in ("right", "left"):
        content = analysis[page]
        annotations = copy.deepcopy(content["annotations"])

        def reject(reason, page=page, annotations=annotations):
            reports[page] = {"accepted": False, "reason": reason, "annotations": annotations}

        count, body = result[page + "_columns"], result[page + "_body_width"]
        y, height = result[page + "_y"], result[page + "_height"]
        if "stats" not in content or count < 3:
            reject("Y 範圍、行數或正文欄寬不適合自動定位，請先調整。")
            continue
        groups, titles = content["groups"], content["titles"]
        if not groups:
            reject("找不到足夠的正文文字，保留原框。")
            continue
        start, end = (image.width // 2, image.width) if page == "right" else (0, image.width // 2)
        columns = [
            float(np.median([g[0] for g in group]))
            for group in groups
            if group not in titles
            and len(group) >= 2
            and max(g[2] for g in group) - min(g[1] for g in group) >= body
        ]
        if (infer_columns and len(columns) < 6) or (
            not infer_columns and not max(3, int(np.ceil(count * 0.65))) <= len(columns) <= count
        ):
            reject(
                f"找到 {len(columns)} 條候選正文，設定為 {count} 行；可能有短欄、插圖或標題，請手動微調。"
            )
            continue
        values = np.asarray(columns)
        gaps = np.diff(values)
        basic_pitch = float(np.median(gaps))
        # Once pitch is supported by substantial columns, a single glyph is enough
        # to recover an outer short column. Never extrapolate into pure whitespace.
        weak = [
            float(np.median([g[0] for g in group]))
            for group in groups
            if group not in titles and len(group) == 1
        ]
        for _ in range(2):
            for edge in (values[0] - basic_pitch, values[-1] + basic_pitch):
                matches = [x for x in weak if abs(x - edge) < basic_pitch * 0.18]
                if matches:
                    values = np.sort(np.append(values, min(matches, key=lambda x: abs(x - edge))))
        gaps = np.diff(values)
        steps = np.maximum(1, np.rint(gaps / basic_pitch)).astype(int)
        indices = np.concatenate(([0], np.cumsum(steps)))
        if infer_columns:
            count = int(indices[-1]) + 1
            if count not in (17, 18):
                reject(f"正文可見跨度為 {count} 欄，不能確定 17／18 行；保留原框與行數。")
                continue
            if len(columns) < count * 0.65 or np.count_nonzero(steps == 1) < len(steps) * 0.6:
                reject("可見文字太稀疏，無法可靠估計行數，保留原設定。")
                continue
        if indices[-1] != count - 1:
            reject("可見文字未涵蓋設定行數的完整跨度，無法確認外側邊界，保留原框。")
            continue
        pitch, first = np.polyfit(indices, values, 1)
        residual = float(np.max(np.abs(values - (first + indices * pitch))))
        if (
            pitch < body
            or residual > pitch * 0.16
            or np.any(np.abs(gaps - steps * pitch) > pitch * 0.3)
        ):
            reject("文字帶間距不夠規律，保留原框。")
            continue
        left = round(start + first - pitch / 2)
        right = round(start + first + (count - 0.5) * pitch)
        if left < start or right > end or right - left < count * body:
            reject("估計頁框超出半頁或不足以容納正文欄寬，保留原框。")
            continue
        proposal = copy.deepcopy(result)
        proposal.update(
            {
                page + "_x": (left + right) / 2,
                page + "_width": right - left,
                page + "_columns": count,
            }
        )
        proposal["overrides"] = {
            key: value
            for key, value in proposal.get("overrides", {}).items()
            if not key.startswith(page + "_")
        }
        filled = set(map(int, indices))
        for index in range(count):
            if index not in filled:
                center = start + first + index * pitch
                annotations.append(
                    {
                        "kind": "filled",
                        "box": [round(center - body / 2), y, round(center + body / 2), y + height],
                    }
                )
        # Title candidates remain visible for manual confirmation; never discard
        # or silently run picture pixels through OCR based on this heuristic.
        result = proposal
        reports[page] = {
            "accepted": True,
            "reason": f"以 {len(columns)} 條文字帶定位 {count} 行；X 範圍 {left}–{right}，估計欄距 {pitch:.1f} px。",
            "bounds": [left, right],
            "residual": residual,
            "columns": count,
            "annotations": annotations,
        }
    # Each accepted page has validated bounds; OCR validates the whole layout.
    return result, reports
