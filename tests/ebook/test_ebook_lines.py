import copy
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.ebook.ocr.lines import detect_lines, scaled_layout
from core.ebook.ocr.session import (
    create_session,
    default_layout,
    load_session,
    prepare_session,
    regions,
)
from tests.ebook.test_ebook_autoframe import sample
from tests.ebook.ui_helpers import wait_idle
from ui.ebook.ocr_page import EbookOcrPanel as App


class TestLines(unittest.TestCase):
    def test_ratio_rounding(self):
        original = default_layout()
        current = original
        for height in (1080, 1440, 1200, 1199):
            current = scaled_layout(current, (1920, height))
            self.assertEqual(current["left_y"], round(height * 38 / 1200))
            self.assertEqual(
                current["left_y"] + current["left_height"], round(height * 1168 / 1200)
            )
        self.assertEqual(original, default_layout())

    def test_two_lines_short_and_blank(self):
        image = sample(2, left=False)
        layout, _reports = detect_lines(image, default_layout())
        self.assertEqual(len(layout["line_regions"]["right"]), 2)
        self.assertEqual(len(layout["line_regions"]["left"]), 0)
        self.assertEqual(len(regions(layout, image.size)), 2)
        image = Image.new("RGB", (1920, 1200), "white")
        ImageDraw.Draw(image).rectangle((1802, 60, 1824, 84), fill="black")
        layout, _ = detect_lines(image, default_layout())
        self.assertEqual(len(regions(layout, image.size)), 1)
        self.assertEqual(regions(layout, image.size)[0]["kind"], "uncertain")

    def test_session_and_editing(self):
        from core.ebook.ocr import session as ebook_core

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                tempfile.TemporaryDirectory() as temp,
                patch.object(ebook_core, "SESSIONS", Path(temp)),
            ):
                app = App(root)
                app.accept_image(sample(2, left=False), "test")
                wait_idle(app)
                self.assertEqual(len(app.items), 2)
                base = copy.deepcopy(app.layout)
                with patch("ui.ebook.ocr_page.simpledialog.askstring", return_value="100,132"):
                    app.add_line()
                self.assertEqual(len(app.items), 3)
                app.selected = 2
                app.delete_line()
                self.assertEqual(len(app.items), 2)
                app.restore_lines()
                self.assertEqual(app.layout, base)
                folder, data = create_session(app.image, "test")
                prepare_session(folder, data, app.image, app.layout)
                _, loaded, _ = load_session(folder / "manifest.json")
                self.assertEqual(loaded["layout"], app.layout)
                self.assertEqual(len(loaded["columns"]), 2)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
