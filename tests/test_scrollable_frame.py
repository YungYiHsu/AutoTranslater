"""Real Tk layout and scoped wheel routing checks."""

from tkinter import Text, Tk, ttk
from types import SimpleNamespace
from typing import Any

from ui.widgets import ScrollableFrame


def test_scrolling_resize_and_nested_editor() -> None:
    root = Tk()
    root.attributes("-alpha", 0.01)
    try:
        root.geometry("500x300")
        panel = ScrollableFrame(root)
        panel.pack(fill="both", expand=True)
        block = ttk.Frame(panel.content, height=900, width=200)
        block.pack()
        editor = Text(panel.content, height=2)
        editor.pack()
        root.after(150, root.quit)
        root.mainloop()
        root.update()
        assert panel.scrollbar.winfo_ismapped()
        assert int(panel.canvas.itemcget(panel._window, "width")) == panel.canvas.winfo_width()
        event: Any = SimpleNamespace(
            x_root=block.winfo_rootx() + 5,
            y_root=block.winfo_rooty() + 5,
            num=0,
            delta=-120,
        )
        panel.winfo_containing = lambda *_args: block  # type: ignore[method-assign]
        assert panel._on_wheel(event) == "break"
        assert panel.canvas.yview()[0] > 0
        panel.canvas.yview_moveto(1)
        root.update()
        event.x_root = editor.winfo_rootx() + 5
        event.y_root = editor.winfo_rooty() + 5
        before = panel.canvas.yview()
        panel.winfo_containing = lambda *_args: editor  # type: ignore[method-assign]
        assert panel._on_wheel(event) is None
        assert panel.canvas.yview() == before
        block.pack_forget()
        root.update()
        panel._update_region()
        root.update()
        assert not panel.scrollbar.winfo_ismapped()
        assert panel.canvas.yview()[0] == 0
        bindings = tuple(panel._wheel_bindings.values())
        panel.destroy()
        for binding in bindings:
            assert not root.tk.call("info", "commands", binding)
    finally:
        root.destroy()
