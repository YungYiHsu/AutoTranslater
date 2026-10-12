"""Scrollable image view; zoom never changes the source image."""

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk


class ImagePreview(ttk.Frame):
    def __init__(self, parent, *, fit_width_limit=None, on_resize=None):
        super().__init__(parent)
        self.fit_width_limit = fit_width_limit
        self.on_resize = on_resize
        self.source = None
        self.photo = None
        self.render_key = None
        self.zoom = tk.StringVar(self, "符合寬度")
        controls = ttk.Frame(self)
        controls.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(controls, text="縮放").pack(side="left")
        combo = ttk.Combobox(controls, textvariable=self.zoom, state="readonly", width=12,
                            values=("符合視窗", "符合寬度", "50%", "75%", "100%", "125%", "150%"))
        combo.pack(side="left", padx=5)
        combo.bind("<<ComboboxSelected>>", lambda e: self.render())
        if fit_width_limit is not None:
            controls.grid_remove()
        self.canvas = tk.Canvas(self, width=1, height=1, highlightthickness=0, background="#eeeeee")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.wheel(e, horizontal=True))

    def wheel(self, event, horizontal=False):
        if event.delta:
            units = -max(1, abs(event.delta) // 120) * (1 if event.delta > 0 else -1)
            (self.canvas.xview_scroll if horizontal else self.canvas.yview_scroll)(units, "units")
        return "break"

    def set_image(self, image, side="雙頁"):
        key = (id(image), side)
        if getattr(self, "source_key", None) == key:
            self.render()
            return
        self.source_key = key
        self.source = image
        if image is not None and side != "雙頁":
            w, h = image.size
            self.source = image.crop((w // 2, 0, w, h) if side == "右頁" else (0, 0, w // 2, h))
        self.render_key = None
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.render()

    def render(self):
        if self.source is None:
            self.canvas.delete("all")
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            self.photo = None
            if self.on_resize:
                self.on_resize()
            return
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        sw, sh = self.source.size
        mode = self.zoom.get()
        scale = (width / sw if mode == "符合寬度" else min(width / sw, height / sh)
                 if mode == "符合視窗" else int(mode.rstrip("%")) / 100)
        if self.fit_width_limit is not None:
            scale = min(width / sw, self.fit_width_limit)
        size = (max(1, round(sw * scale)), max(1, round(sh * scale)))
        key = (self.source_key, size, width, height)
        if key == self.render_key:
            return
        self.render_key = key
        self.photo = ImageTk.PhotoImage(self.source.resize(size, Image.Resampling.LANCZOS), master=self)
        self.canvas.delete("all")
        self.canvas.create_image(max(0, (width - size[0]) // 2), 0, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, max(width, size[0]), max(height, size[1])))
        if self.on_resize:
            self.on_resize()
