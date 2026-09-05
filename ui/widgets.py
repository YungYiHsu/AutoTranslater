"""Small custom Tkinter widgets that remain PyInstaller-friendly."""

from __future__ import annotations

from tkinter import Canvas, Misc


class LoadingSpinner(Canvas):
    """A lightweight rotating arc driven by Tk's non-blocking event loop."""

    def __init__(
        self,
        master: Misc,
        *,
        size: int = 26,
        interval_ms: int = 70,
    ) -> None:
        background = master.winfo_toplevel().cget("background")
        super().__init__(
            master,
            width=size,
            height=size,
            background=background,
            highlightthickness=0,
            borderwidth=0,
        )
        self._size = size
        self._interval_ms = interval_ms
        self._angle = 0
        self._running = False
        self._after_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.grid()
        self._animate()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        self.delete("all")
        self.grid_remove()

    def _animate(self) -> None:
        if not self._running:
            return
        self.delete("all")
        margin = 4
        self.create_arc(
            margin,
            margin,
            self._size - margin,
            self._size - margin,
            start=self._angle,
            extent=265,
            style="arc",
            width=3,
            outline="#4776b4",
        )
        self._angle = (self._angle - 24) % 360
        self._after_id = self.after(self._interval_ms, self._animate)


__all__ = ["LoadingSpinner"]
