"""Visible Tkinter entry point used by the future Windows executable."""

from __future__ import annotations

from tkinter import Tk, messagebox

from core.logging_config import setup_logging
from core.paths import get_app_dir
from ui.app import DesktopApp


def main() -> None:
    app_dir = get_app_dir()
    setup_logging(app_dir / "logs")
    root = Tk()
    try:
        DesktopApp(root, app_directory=app_dir)
    except Exception as exc:  # noqa: BLE001 - startup failures must be visible in windowed mode.
        messagebox.showerror("啟動失敗", str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
