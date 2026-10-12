"""Small custom Tkinter widgets that remain PyInstaller-friendly."""

from __future__ import annotations

from tkinter import Canvas, Event, Misc, ttk


class ScrollableFrame(ttk.Frame):
    """Vertically scrolling content with wheel handling limited to this subtree."""

    def __init__(self, master: Misc) -> None:
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = Canvas(self, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ttk.Frame(self.canvas, padding=20)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._resize)
        self._top = self.winfo_toplevel()
        self._wheel_bindings = {
            sequence: self._top.bind(sequence, self._on_wheel, add="+")
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>")
        }
        self.bind("<Destroy>", self._cleanup, add="+")

    def _resize(self, event: Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update_region()

    def _update_region(self, event: Event | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.content.winfo_reqheight() > self.canvas.winfo_height():
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event: Event) -> str | None:
        # Check the pointer, not keyboard focus; never scroll a different tab/dialog.
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self:
            if widget.winfo_class() in {"Text", "Listbox", "TCombobox", "TSpinbox", "Spinbox"}:
                return None
            widget = widget.master
        if widget is None or self.content.winfo_reqheight() <= self.canvas.winfo_height():
            return None
        if event.num in (4, 5):
            steps = -3 if event.num == 4 else 3
        elif event.delta:
            steps = -int(event.delta / 120) * 3
            if not steps:
                steps = -1 if event.delta > 0 else 1
        else:
            return None
        self.canvas.yview_scroll(steps, "units")
        return "break"

    def _cleanup(self, event: Event) -> None:
        if event.widget is self:
            for sequence, binding in self._wheel_bindings.items():
                if binding:
                    self._top.unbind(sequence, binding)


class CollapsibleSection(ttk.Frame):
    """A labelled content frame that can be hidden without losing widget state."""

    def __init__(
        self,
        master: Misc,
        *,
        text: str,
        padding: int | tuple[int, int] | tuple[int, int, int, int] = 12,
        expanded: bool = True,
    ) -> None:
        super().__init__(master)
        self._title = text
        self._expanded = expanded
        self._toggle_button = ttk.Button(self, command=self.toggle, style="Toolbutton")
        self._toggle_button.grid(row=0, column=0, sticky="w")
        self.content = ttk.Frame(self, padding=padding, relief="groove", borderwidth=1)
        self.content.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        if not expanded:
            self.content.grid_remove()
        self._render()

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_title(self, text: str) -> None:
        self._title = text
        self._render()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self.content.grid()
        else:
            self.content.grid_remove()
        self._render()

    def _render(self) -> None:
        marker = "▼" if self._expanded else "▶"
        self._toggle_button.configure(text=f"{marker} {self._title}")


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


__all__ = ["CollapsibleSection", "LoadingSpinner", "ScrollableFrame"]
