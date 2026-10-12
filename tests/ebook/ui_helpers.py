import copy
import time
from unittest.mock import patch


def empty_detection(image, layout):
    """Test double for storage/UI tests; does not stand in for image-detection tests."""
    proposed = copy.deepcopy(layout)
    proposed['line_regions'] = {'right': [], 'left': []}
    return proposed, {p: {'accepted': True, 'annotations': [], 'reason': ''}
                      for p in ('right', 'left')}


def wait_idle(panel, timeout=15):
    """Pump real Tk events while background tasks run; never block on error dialogs."""
    deadline = time.monotonic() + timeout
    with patch('ui.ebook.ocr_page.messagebox.showerror'):
        while panel.busy and time.monotonic() < deadline:
            panel.root.update()
            time.sleep(0.005)
    assert not panel.busy, 'Background operation did not finish'
    if panel.last_error is not None:
        raise panel.last_error
