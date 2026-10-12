import copy
import json
import threading
import time
import tkinter as tk
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from tests.ebook.test_ebook_core import FakeRecognizer
from tests.ebook.ui_helpers import wait_idle
from ui.ebook.ocr_page import EbookOcrPanel


def synthetic_detection(_image, layout):
    layout = copy.deepcopy(layout)
    layout["line_regions"] = {
        "right": [
            {"box": [150, 10, 170, 90], "kind": "body"},
            {"box": [120, 10, 140, 90], "kind": "body"},
        ],
        "left": [],
    }
    reports = {p: {"accepted": True, "annotations": [], "reason": ""} for p in ("right", "left")}
    return layout, reports


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr("core.ebook.ocr.session.SESSIONS", tmp_path)
    root = tk.Tk()
    root.withdraw()
    app = EbookOcrPanel(root, engine=FakeRecognizer())
    image = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((155, 20, 165, 35), fill="black")
    draw.rectangle((125, 20, 135, 35), fill="black")
    with patch("ui.ebook.ocr_page.detect_lines", side_effect=synthetic_detection):
        app.accept_image(image, "synthetic")
        wait_idle(app)
    yield app
    app.shutdown()
    root.destroy()


def test_recognition_remains_responsive_cancel_and_resume(panel):
    entered, release = threading.Event(), threading.Event()

    class SlowRecognizer(FakeRecognizer):
        def recognize(self, image):
            entered.set()
            assert release.wait(3)
            return super().recognize(image)

    panel.engine = SlowRecognizer()
    panel.start()
    assert panel.busy
    ticks = []
    panel.root.after(1, lambda: ticks.append(True))
    deadline = time.monotonic() + 2
    while (not entered.is_set() or not ticks) and time.monotonic() < deadline:
        panel.root.update()
        time.sleep(0.005)
    assert ticks and entered.is_set() and panel.busy
    panel.cancel.set()
    release.set()
    wait_idle(panel)
    assert panel.data["status"] == "cancelled"
    assert [r["status"] for r in panel.data["columns"]] == ["done", "pending"]
    panel.engine = FakeRecognizer()
    panel.start(resume=True)
    wait_idle(panel)
    assert panel.data["status"] == "complete"
    saved = json.loads((panel.folder / "manifest.json").read_text(encoding="utf-8"))
    assert saved == panel.data
    assert saved["timing"]["button_to_result_seconds"] >= 0


def test_preparation_failure_unlocks_gui_and_keeps_old_data(panel):
    original = panel.data
    with patch("ui.ebook.ocr_page.prepare_session", side_effect=OSError("disk full")):
        panel.start()
        with pytest.raises(OSError, match="disk full"):
            wait_idle(panel)
    assert panel.data is original
    assert panel.buttons["開始新辨識"].instate(["!disabled"])


def test_detection_failure_does_not_replace_current_image(panel):
    original = panel.image
    with patch("ui.ebook.ocr_page.detect_lines", side_effect=RuntimeError("analysis failed")):
        panel.accept_image(Image.new("RGB", (300, 150)), "replacement")
        with pytest.raises(RuntimeError, match="analysis failed"):
            wait_idle(panel)
    assert panel.image is original


def test_generic_job_does_not_enable_recognition_cancel(panel):
    gate = threading.Event()
    panel.background(lambda: gate.wait(2), lambda _: None)
    assert panel.stop_button.instate(["disabled"])
    gate.set()
    wait_idle(panel)
