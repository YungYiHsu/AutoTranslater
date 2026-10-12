import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.ebook.ocr import session as core


class FakeRecognizer:
    def __init__(self):
        self.versions = {"recognizer": "synthetic-test"}

    def load(self):
        return 0.01

    def recognize(self, image):
        return "TEST", 0.02, {"contents": ["TEST"]}


class TestCore(unittest.TestCase):
    def test_center_fixed_drag(self):
        layout = core.default_layout()
        original = core.page_bounds(layout, "right")
        a, b, c, d = original
        for handle in ("w", "e", "n", "s", "nw", "ne", "sw", "se"):
            for delta in (-9999, -10, 10, 9999):
                result = core.drag_page(
                    layout, "right", handle, delta, delta, (1920, 1200), center_fixed=True
                )
                x1, y1, x2, y2 = core.page_bounds(result, "right")
                self.assertEqual(x1 + x2, a + c)
                self.assertEqual(y1 + y2, b + d)
                self.assertGreaterEqual(x1, 0)
                self.assertGreaterEqual(y1, 0)
                self.assertLessEqual(x2, 1920)
                self.assertLessEqual(y2, 1200)
                self.assertGreaterEqual(
                    x2 - x1, layout["right_columns"] * layout["right_body_width"]
                )
                self.assertGreaterEqual(y2 - y1, 1)
        result = core.drag_page(layout, "right", "se", 10, 10, (1920, 1200), center_fixed=True)
        self.assertEqual(core.page_bounds(result, "right"), [a - 10, b - 10, c + 10, d + 10])
        self.assertEqual(
            core.drag_page(layout, "right", "move", 5, 5, (1920, 1200), center_fixed=True),
            core.drag_page(layout, "right", "move", 5, 5, (1920, 1200)),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mock = patch.object(core, "SESSIONS", Path(self.temp.name))
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.image = Image.new("RGB", (200, 100), "white")
        ImageDraw.Draw(self.image).rectangle((145, 15, 150, 30), fill="black")
        self.layout = {
            "width": 200,
            "height": 100,
            "columns": 1,
            "right_center": 150,
            "left_center": 50,
            "right_pitch": 20,
            "left_pitch": 20,
            "column_width": 20,
            "top": 10,
            "bottom": 90,
            "overrides": {},
        }

    def session(self):
        folder, data = core.create_session(self.image)
        core.prepare_session(folder, data, self.image, self.layout)
        return folder, data

    def test_reading_order(self):
        items = core.regions(self.layout, self.image.size)
        self.assertEqual([i["column"] for i in items], ["right_01", "left_01"])

    def test_equal_pages_mixed_counts(self):
        layout = core.default_layout()
        layout.update(layout_version=2, right_x=1837, left_x=938)
        layout["left_columns"] = 18
        items = core.regions(layout, (1920, 1200))
        self.assertEqual(len(items), 35)
        for page, n in [("right", 17), ("left", 18)]:
            boxes = [r["box"] for r in items if r["column"].startswith(page)]
            self.assertEqual(len(boxes), n)
            self.assertEqual(boxes[0][2], layout[page + "_x"])
            self.assertEqual(boxes[-1][0], layout[page + "_x"] - layout[page + "_width"])
            self.assertEqual(sum(b[2] - b[0] for b in boxes), layout[page + "_width"])
            self.assertTrue(all(boxes[i][0] == boxes[i + 1][2] for i in range(n - 1)))
            widths = [b[2] - b[0] for b in boxes]
            self.assertLessEqual(max(widths) - min(widths), 1)
            self.assertTrue(
                all(
                    b[1] == layout[page + "_y"]
                    and b[3] == layout[page + "_y"] + layout[page + "_height"]
                    for b in boxes
                )
            )

    def test_page_boundaries_and_invalid_counts(self):
        for key, value in [
            ("right_columns", 0),
            ("left_columns", 101),
            ("right_x", 1),
            ("right_height", 0),
            ("left_y", 1199),
            ("left_width", 3),
        ]:
            layout = core.default_layout()
            layout[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                core.regions(layout, (1920, 1200))

    def test_legacy_migration_preserves_boxes(self):
        old = core.regions(self.layout, self.image.size)
        upgraded = core.upgrade_layout(self.layout)
        self.assertEqual(core.regions(upgraded, self.image.size), old)
        self.assertEqual(upgraded["layout_version"], 3)

    def test_center_top_and_body_width(self):
        layout = core.default_layout()
        layout.update(right_x=1375, right_y=50, right_width=850, right_height=1100)
        self.assertEqual(core.page_bounds(layout, "right"), [950, 50, 1800, 1150])
        boxes = core.regions(layout, (1920, 1200))[:17]
        self.assertEqual(boxes[0]["box"], [1758, 50, 1792, 1150])
        self.assertTrue(all(r["box"][2] - r["box"][0] == 34 for r in boxes))
        layout["right_body_width"] = 51
        with self.assertRaises(ValueError):
            core.regions(layout, (1920, 1200))

    def test_version2_migration_preserves_pixels(self):
        layout = core.default_layout()
        layout.update(layout_version=2, right_x=1837, left_x=938)
        old = core.regions(layout, (1920, 1200))
        updated = core.upgrade_layout(layout)
        self.assertEqual(updated["right_x"], 1422.5)
        self.assertEqual(core.regions(updated, (1920, 1200)), old)

    def test_drag_move_clamp_and_resize(self):
        layout = core.default_layout()
        moved = core.drag_page(layout, "right", "move", 9999, -9999, (1920, 1200))
        self.assertEqual(core.page_bounds(moved, "right"), [1091, 0, 1920, 1130])
        self.assertEqual(moved["left_x"], layout["left_x"])
        small = core.drag_page(layout, "right", "nw", 9999, 9999, (1920, 1200))
        self.assertEqual(small["right_width"], 17 * 34)
        self.assertEqual(small["right_height"], 1)
        self.assertEqual(small["right_x"], (core.page_bounds(small, "right")[0] + 1837) / 2)

    def test_every_resize_handle_valid(self):
        layout = core.default_layout()
        for h in ["n", "s", "w", "e", "nw", "ne", "sw", "se"]:
            for delta in [-10000, 10000]:
                updated = core.drag_page(layout, "right", h, delta, delta, (1920, 1200))
                self.assertEqual(len(core.regions(updated, (1920, 1200))), 34)

    def test_new_format_roundtrip(self):
        image = Image.new("RGB", (1920, 1200), "white")
        folder, data = core.create_session(image)
        layout = core.default_layout()
        layout["left_columns"] = 18
        core.prepare_session(folder, data, image, layout)
        _, loaded, original = core.load_session(folder / "manifest.json")
        self.assertEqual(original.size, (1920, 1200))
        self.assertEqual(len(loaded["columns"]), 35)

    def test_size_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            core.regions(self.layout, (201, 100))

    def test_outside_rejected(self):
        self.layout["right_center"] = 200
        with self.assertRaises(ValueError):
            core.regions(self.layout, self.image.size)

    def test_empty_and_trim(self):
        crop, blank = core.crop_region(self.image, (140, 10, 160, 90))
        self.assertFalse(blank)
        self.assertEqual(crop.size, (20, 22))
        self.assertTrue(core.crop_region(self.image, (40, 10, 60, 90))[1])

    def test_split_and_skip(self):
        self.layout["overrides"] = {
            "right_01": [[140, 10, 160, 40], [140, 60, 160, 90]],
            "left_01": [],
        }
        self.assertEqual(len(core.regions(self.layout, self.image.size)), 2)
        self.layout["overrides"]["right_01"][1][1] = 30
        with self.assertRaises(ValueError):
            core.regions(self.layout, self.image.size)

    def test_save_load_and_correction(self):
        folder, data = self.session()
        data["columns"][0].update(raw="RAW", corrected="EDIT")
        core.save_session(folder, data)
        _, loaded, image = core.load_session(folder / "manifest.json")
        self.assertEqual(loaded["columns"][0]["raw"], "RAW")
        self.assertEqual(image.size, self.image.size)
        self.assertEqual((folder / "corrected.txt").read_text().strip(), "EDIT")
        self.assertEqual((folder / "raw-merged.txt").read_text().strip(), "RAW")

    def test_no_overwrite(self):
        first, _ = self.session()
        second, _ = self.session()
        self.assertNotEqual(first, second)

    def test_hash_detects_replaced_image(self):
        folder, _ = self.session()
        Image.new("RGB", self.image.size, "black").save(folder / "original.png")
        with self.assertRaises(ValueError):
            core.load_session(folder / "manifest.json")

    def test_cancel_and_resume(self):
        folder, data = self.session()
        cancel = threading.Event()
        cancel.set()
        core.run_recognition(folder, data, FakeRecognizer(), cancel, lambda *args: None)
        self.assertEqual(data["status"], "cancelled")
        cancel.clear()
        core.run_recognition(folder, data, FakeRecognizer(), cancel, lambda *args: None)
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["columns"][0]["raw"], "TEST")
        self.assertEqual(data["columns"][1]["status"], "blank")

    def test_retry_preserves_raw_and_correction(self):
        folder, data = self.session()
        data["columns"][0].update(status="done", raw="OLD", corrected="EDIT")
        core.run_recognition(
            folder, data, FakeRecognizer(), threading.Event(), lambda *args: None, [0]
        )
        self.assertEqual(data["columns"][0]["attempts"][0]["raw"], "OLD")
        self.assertEqual(data["columns"][0]["corrected"], "EDIT")

    def test_failure_checkpoints(self):
        class Bad(FakeRecognizer):
            def recognize(self, image):
                raise RuntimeError("synthetic error")

        folder, data = self.session()
        core.run_recognition(folder, data, Bad(), threading.Event(), lambda *args: None)
        loaded = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(loaded["status"], "partial")
        self.assertEqual(loaded["columns"][0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
