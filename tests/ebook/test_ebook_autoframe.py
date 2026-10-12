import copy
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.ebook.ocr.layout import estimate_page_x
from core.ebook.ocr.session import default_layout
from tests.ebook.ui_helpers import wait_idle
from ui.ebook.ocr_page import EbookOcrPanel as App


def sample(count=17, left=True):
    image = Image.new("RGB", (1920, 1200), "white")
    draw = ImageDraw.Draw(image)
    for first in [120, 1040] if left else [1040]:
        for i in range(count):
            center = first + 48 * i
            for y in range(60, 1000, 40):
                draw.rectangle((center - 11, y, center + 11, y + 25), fill="black")
                draw.rectangle((center + 16, y, center + 22, y + 8), fill="black")
    return image


class TestAutoFrame(unittest.TestCase):
    def test_short_outer_column_and_reject_partial_count(self):
        image = sample(17)
        draw = ImageDraw.Draw(image)
        draw.rectangle((1020, 38, 1065, 1168), fill="white")
        draw.rectangle((1029, 60, 1051, 85), fill="black")
        result, report = estimate_page_x(image, default_layout(), True)
        self.assertTrue(report["right"]["accepted"])
        self.assertEqual(result["right_columns"], 17)
        result, report = estimate_page_x(sample(12), default_layout(), True)
        self.assertFalse(report["right"]["accepted"])
        self.assertEqual(result["right_columns"], 17)

    def test_filled_columns_retained_and_illustration_flagged(self):
        image = sample(18)
        draw = ImageDraw.Draw(image)
        draw.rectangle((1215, 38, 1255, 1168), fill="white")
        result, report = estimate_page_x(image, default_layout(), True)
        self.assertTrue(report["right"]["accepted"])
        self.assertTrue(any(a["kind"] == "filled" for a in report["right"]["annotations"]))
        self.assertFalse(any(value == [] for value in result["overrides"].values()))
        draw.rectangle((1100, 100, 1800, 1050), fill=128)
        _, report = estimate_page_x(image, default_layout(), True)
        self.assertTrue(any(a["kind"] == "illustration" for a in report["right"]["annotations"]))

    def test_infer_counts_and_invalid_y(self):
        for count in (17, 18):
            layout = default_layout()
            layout["right_columns"] = layout["left_columns"] = 12
            result, reports = estimate_page_x(sample(count), layout, infer_columns=True)
            for page in ("right", "left"):
                self.assertTrue(reports[page]["accepted"])
                self.assertEqual(result[page + "_columns"], count)
                for key in ("y", "height", "body_width"):
                    self.assertEqual(result[page + "_" + key], layout[page + "_" + key])
        layout["left_height"] = 2000
        result, reports = estimate_page_x(sample(), layout, infer_columns=True)
        self.assertFalse(reports["left"]["accepted"])
        self.assertTrue(reports["right"]["accepted"])
        self.assertEqual(result["left_height"], 2000)

    def test_load_automatically_applies_and_undoes(self):
        import tempfile
        from pathlib import Path

        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            before = copy.deepcopy(app.layout)
            with (
                tempfile.TemporaryDirectory() as folder,
                patch("core.ebook.ocr.session.SESSIONS", Path(folder)),
                patch("ui.ebook.ocr_page.messagebox.showinfo") as info,
            ):
                app.accept_image(sample(18), "test")
                wait_idle(app)
                self.assertEqual(len(app.layout["line_regions"]["right"]), 18)
                self.assertEqual(len(app.layout["line_regions"]["left"]), 18)
                self.assertFalse(app.busy)
                info.assert_not_called()
                app.undo_auto_frame()
                self.assertEqual({k: app.layout[k] for k in before}, before)
                app.accept_image(Image.new("RGB", (1920, 1200), "white"), "blank")
                wait_idle(app)
                self.assertEqual(app.layout["line_regions"], {"right": [], "left": []})
                info.assert_not_called()
        finally:
            root.destroy()

    def test_regular_ruby_and_invariants(self):
        for count in (17, 18):
            layout = default_layout()
            layout["left_columns"] = layout["right_columns"] = count
            original = copy.deepcopy(layout)
            result, reports = estimate_page_x(sample(count), layout)
            self.assertEqual(layout, original)
            for page, first in (("left", 120), ("right", 1040)):
                self.assertTrue(reports[page]["accepted"])
                self.assertEqual(
                    reports[page]["bounds"], [first - 24, first + (count - 1) * 48 + 24]
                )
                for key in ("y", "height", "columns", "body_width"):
                    self.assertEqual(result[page + "_" + key], layout[page + "_" + key])

    def test_blank_artwork_and_partial(self):
        layout = default_layout()
        for fill in ("white", "black"):
            result, reports = estimate_page_x(Image.new("RGB", (1920, 1200), fill), layout)
            self.assertEqual(result, layout)
            self.assertFalse(any(r["accepted"] for r in reports.values()))
        result, reports = estimate_page_x(sample(left=False), layout)
        self.assertTrue(reports["right"]["accepted"])
        self.assertFalse(reports["left"]["accepted"])
        self.assertEqual(result["left_x"], layout["left_x"])
        _, reports = estimate_page_x(sample(16), layout)
        self.assertFalse(any(r["accepted"] for r in reports.values()))

    def test_internal_blank_columns(self):
        image = sample()
        draw = ImageDraw.Draw(image)
        for i in (4, 10):
            center = 1040 + i * 48
            draw.rectangle((center - 15, 38, center + 24, 1168), fill="white")
        result, reports = estimate_page_x(image, default_layout())
        self.assertTrue(reports["right"]["accepted"])
        self.assertEqual(result["right_width"], 17 * 48)

    def test_ui_confirmation_undo_and_no_ocr(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            app.image = sample()
            app.layout["overrides"] = {"right_01": [[1795, 60, 1829, 300]]}
            app.refresh()
            original = copy.deepcopy(app.layout)
            with patch("ui.ebook.ocr_page.messagebox.askyesno", return_value=False):
                app.auto_frame_x()
                wait_idle(app)
            self.assertEqual(app.layout, original)
            with patch("ui.ebook.ocr_page.messagebox.askyesno", return_value=True):
                app.auto_frame_x()
                wait_idle(app)
            self.assertNotEqual(app.layout, original)
            self.assertFalse(app.busy)
            self.assertIsNone(app.data)
            app.undo_auto_frame()
            self.assertEqual(app.layout, original)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
