"""Execute data-only jobs off Tk; deliver their callbacks through the owner's queue."""

import threading


def submit(events, work, token):
    """Only plain data and an opaque token cross the thread boundary, never Tk callbacks."""
    def run():
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - forward failures to the GUI thread.
            events.put(("job_error", (token, exc)))
        else:
            events.put(("job_done", (token, result)))

    threading.Thread(target=run, daemon=True, name="ebook-background").start()
