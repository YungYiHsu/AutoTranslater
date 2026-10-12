from tests.ebook.ui_helpers import wait_idle

"""Regressions for capture -> tab switch and deferred widget refresh."""

import time
import tkinter as tk
from unittest.mock import patch

from core.ebook.ocr import session
from tests.ebook.test_ebook_autoframe import sample
from ui.ebook.ocr_page import EbookOcrPanel


def switch(panel, index):
    panel.notebook.select(index)
    panel.on_tab_changed()


def test_real_capture_entry_updates_list_on_switch(tmp_path):
    root = tk.Tk()
    root.withdraw()
    panel = EbookOcrPanel(root)
    try:
        with (patch.object(session, "SESSIONS", tmp_path),
              patch("ui.ebook.ocr_page.ImageGrab.grab", return_value=sample()),
              patch.object(root, "deiconify")):
            panel.capture()
            assert panel.busy
            deadline = time.monotonic() + 5
            while panel.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            assert not panel.busy
            assert len(panel.items) == 34
            assert panel.listbox.size() == 0  # hidden page deliberately not refreshed
            assert panel.canvas.find_withtag("page-frame")
            switch(panel, 1)
            assert panel.listbox.size() == 34
            panel.listbox.selection_set(0)
            panel.select()
            assert panel.crop_photo is not None
            assert panel.selected == 0
            panel.set_busy(True)
            panel.refresh()
            assert panel.listbox.size() == 34  # updates also work when controls are disabled
            assert panel.listbox.cget("state") == "disabled"
            panel.set_busy(False)
            switch(panel, 0)
            panel.accept_image(sample(18), "synthetic")
            wait_idle(panel)
            assert panel.listbox.size() == 34  # previous view unchanged while hidden
            switch(panel, 1)
            assert panel.listbox.size() == 36
    finally:
        root.destroy()


def test_pending_parameters_and_correction_cancel():
    root = tk.Tk()
    root.withdraw()
    panel = EbookOcrPanel(root)
    try:
        switch(panel, 2)
        panel.fields["right_y"].set("50")
        switch(panel, 0)
        assert panel.layout["right_y"] == 50
        switch(panel, 2)
        panel.fields["right_y"].set("invalid")
        with patch("ui.ebook.ocr_page.messagebox.showerror") as error:
            switch(panel, 1)
            error.assert_called_once()
            assert panel._active_tab == 2
        panel.fields["right_y"].set("50")
        switch(panel, 1)
        panel.editor.configure(state="normal")
        panel.editor.insert("1.0", "unsaved")
        panel.dirty = True
        with patch("ui.ebook.ocr_page.messagebox.askyesnocancel", return_value=None):
            switch(panel, 0)
        assert panel._active_tab == 1
        assert panel.editor.get("1.0", "end-1c") == "unsaved"
    finally:
        root.destroy()
