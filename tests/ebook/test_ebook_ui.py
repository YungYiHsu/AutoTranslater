from tests.ebook.ui_helpers import wait_idle

"""Hidden-window interaction smoke tests; screenshot API is mocked (no desktop capture)."""

import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from core.ebook.ocr import session as ebook_core
from tests.ebook.test_ebook_core import FakeRecognizer
from ui.ebook.ocr_page import EbookOcrPanel as App


def switch(app, index):
    app.notebook.select(index)
    app.on_tab_changed()


class TestUI(unittest.TestCase):
    def test_review_space_allocation(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            self.assertEqual(int(app.listbox.cget("height")), 30)
            self.assertFalse(app.listbox.pack_info()["expand"])
            self.assertTrue(app.merged.pack_info()["expand"])
            self.assertEqual(app.merged.pack_info()["fill"], "both")
            self.assertEqual(int(app.crop_canvas.cget("height")), 60)
            self.assertEqual(int(app.editor.cget("height")), 1)
            self.assertIs(app.crop_canvas.master, app.pages[1])
            self.assertIs(app.editor.master, app.pages[1])
            self.assertIs(app.merged.master, app.pages[1])
            self.assertIs(app.review_panel.master, app.line_panel.master)
            self.assertFalse(app.review_panel.master.pack_info()["expand"])
        finally:
            root.destroy()

    def test_action_availability(self):
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        try:
            for label in (
                "開始新辨識",
                "新增行",
                "刪除選取行",
                "還原自動定位前",
                "儲存校訂",
            ):
                self.assertTrue(app.buttons[label].instate(["disabled"]))
            app.image = Image.new("RGB", (1920, 1200), "white")
            app.refresh()
            self.assertFalse(app.buttons["新增行"].instate(["disabled"]))
            switch(app, 1)
            app.listbox.selection_set(0)
            app.select()
            self.assertFalse(app.buttons["刪除選取行"].instate(["disabled"]))
            self.assertTrue(app.buttons["儲存校訂"].instate(["disabled"]))
            app.set_busy(True)
            self.assertTrue(app.buttons["新增行"].instate(["disabled"]))
        finally:
            root.destroy()

    def test_tabs_and_single_page_coordinates(self):
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        try:
            self.assertEqual(
                [app.notebook.tab(t, "text") for t in app.notebook.tabs()],
                ["預覽", "辨識與校訂", "版面設定"],
            )
            app.image = Image.new("RGB", (1920, 1200), "white")
            app.refresh()
            with (
                patch.object(app.canvas, "winfo_width", return_value=480),
                patch.object(app.canvas, "winfo_height", return_value=600),
            ):
                app.view_mode.set("右頁")
                app.draw()
                self.assertEqual(app.scale, 0.5)
                box = app.items[0]["box"]
                app.canvas_select(SimpleNamespace(x=((box[0] + box[2]) / 2 - 960) * 0.5, y=100))
                self.assertEqual(app.selected, 0)
                app.editor.configure(state="normal")
                app.editor.insert("1.0", "unsaved")
                app.notebook.select(2)
                app.notebook.select(1)
                self.assertEqual(app.editor.get("1.0", "end-1c"), "unsaved")
                self.assertEqual(app.selected, 0)
        finally:
            root.destroy()

    def test_preview_and_enter_layout(self):
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        try:
            app.image = Image.new("RGB", (1920, 1200), "white")
            ImageDraw.Draw(app.image).rectangle((1808, 60, 1816, 100), fill="black")
            app.refresh()
            self.assertNotIn("width", app.fields)
            self.assertIn("1920", app.image_size.get())
            switch(app, 1)
            app.listbox.selection_set(0)
            app.select()
            self.assertEqual((app.crop_photo.width(), app.crop_photo.height()), (47, 34))
            for percent in (50, 75, 100, 125, 150):
                app.zoom.set(f"{percent}%")
                app.render_crop()
                self.assertEqual(app.crop_photo.width(), round(47 * percent / 100))
            switch(app, 2)
            app.fields["right_body_width"].set("32")
            app.apply_layout()
            switch(app, 1)
            self.assertEqual(app.selected, 0)
            self.assertEqual(app.crop_photo.height(), 48)
            app.box_value.set("1800,1832")
            app.custom_box()
            self.assertEqual(app.items[0]["box"], [1800, 38, 1832, 1168])
            switch(app, 2)
            app.fields["right_width"].set("invalid")
            old = app.layout.copy()
            with self.assertRaises(ValueError):
                app.apply_layout()
            self.assertEqual(app.layout, old)
            app.fields["right_width"].set("829")
            switch(app, 1)
            app.box_value.set("")
            app.custom_box()
            self.assertIsNone(app.selected)
            self.assertEqual(app.crop_canvas.find_all(), ())
        finally:
            root.destroy()

    def test_capture_recognize_edit_reopen(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(ebook_core, "SESSIONS", Path(temp)),
        ):
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            try:
                app.engine = FakeRecognizer()
                image = Image.new("RGB", (1920, 1200), "white")
                ImageDraw.Draw(image).rectangle((1808, 60, 1816, 100), fill="black")
                app.capture_started = time.perf_counter()
                with (
                    patch("ui.ebook.ocr_page.ImageGrab.grab", return_value=image) as grab,
                    patch.object(root, "deiconify"),
                ):
                    app.finish_capture()
                    wait_idle(app)
                    grab.assert_called_once_with(all_screens=False)
                self.assertEqual(app.image.size, (1920, 1200))
                # Legacy saved layouts remain readable without a fixed-mode UI command.
                app.set_layout(ebook_core.default_layout())
                app.refresh()
                with (
                    patch.object(app.canvas, "winfo_width", return_value=960),
                    patch.object(app.canvas, "winfo_height", return_value=600),
                ):
                    app.draw()
                    self.assertEqual(app.scale, 0.5)
                    self.assertEqual(len(app.canvas.find_withtag("page-frame")), 2)
                    self.assertEqual(len(app.canvas.find_withtag("page-handle")), 16)
                    for frame in app.canvas.find_withtag("page-frame"):
                        self.assertTrue(app.canvas.itemcget(frame, "dash"))
                    original_x = app.layout["right_x"]
                    app.drag_start(SimpleNamespace(x=original_x * 0.5, y=300))
                    app.drag_motion(SimpleNamespace(x=original_x * 0.5 + 5, y=300))
                    app.drag_end(SimpleNamespace(x=original_x * 0.5 + 5, y=300))
                    self.assertEqual(app.layout["right_x"], original_x + 10)
                    switch(app, 2)
                    self.assertEqual(float(app.fields["right_x"].get()), original_x + 10)
                    # Restore layout for the recognition test, without resizing the image.
                    app.set_layout(ebook_core.default_layout())
                    app.refresh()
                self.assertTrue((app.folder / "original.png").exists())
                switch(app, 1)
                app.start()
                deadline = time.monotonic() + 15
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app.busy)
                self.assertEqual(app.data["status"], "complete")
                self.assertEqual(len(app.data["columns"]), 34)
                app.listbox.selection_set(0)
                app.select()
                app.editor.delete("1.0", "end")
                app.editor.insert("1.0", "EDIT")
                app.save_correction()
                _, data, _ = ebook_core.load_session(app.folder / "manifest.json")
                self.assertEqual(data["columns"][0]["raw"], "TEST")
                self.assertEqual(data["columns"][0]["corrected"], "EDIT")
                self.assertTrue((app.folder / "corrected.txt").exists())
                # A changed layout must never edit previous results under the same IDs.
                app.layout["right_x"] += 1
                self.assertIsNone(app.current_row())
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
