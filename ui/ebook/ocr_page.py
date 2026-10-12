"""Embedded ebook OCR interface for the main desktop GUI."""

import copy
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageGrab, ImageTk

from core.ebook.exports import has_page_images, suggested_pages
from core.ebook.ocr import session as session_service
from core.ebook.ocr.lines import detect_lines, scaled_layout
from core.ebook.ocr.session import (
    PRIVATE,
    Recognizer,
    clear_sessions,
    create_session,
    crop_region,
    default_layout,
    drag_page,
    load_session,
    page_bounds,
    prepare_session,
    regions,
    remap_lines,
    run_recognition,
    save_session,
    upgrade_layout,
    write_json,
)
from ui.ebook.ocr_export import OcrExportMixin
from ui.shared.background import submit

logger = logging.getLogger(__name__)


class EbookOcrPanel(OcrExportMixin):
    def __init__(self, root, *, engine=None, require_work=False):
        self.root = root
        self.window = root.winfo_toplevel()
        self.require_work = require_work
        self.work = self.work_directory = None
        self.work_title = tk.StringVar(value="")
        self.page_suggestion = tk.StringVar(value="沒有")
        self.image = self.folder = self.data = None
        self.layout = default_layout()
        self.engine = engine if engine is not None else Recognizer()
        self.events = queue.Queue()
        self._job_callbacks = {}
        self._job_serial = 0
        self.cancel = threading.Event()
        self.busy = False
        self.last_error = None
        self.dirty = False
        self.selected = None
        self.drag = None
        self.capture_started = None
        self.auto_frame_undo = None
        self.analysis_overlay = None
        self.detected_baseline = None
        self.controls = []
        self.buttons = {}
        self.items = []
        self._active_tab = 0
        self._stale_pages = {0, 1, 2}
        self._box_snapshot = ""
        self.status = tk.StringVar(value="先將閱讀器切成全螢幕，再按「全螢幕截圖」。")
        self.build()
        self.set_layout(self.layout)
        self.sync_fields()
        self.update_actions()
        self.poll_id = root.after(100, self.poll)
        root.bind("<Destroy>", self._on_destroy, add="+")

    def button(self, parent, label, command):
        b = ttk.Button(parent, text=label, command=lambda: self.safe(command))
        b.pack(side="left", padx=3, pady=3)
        self.controls.append(b)
        self.buttons[label] = b
        return b

    def build(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)
        preview_page = ttk.Frame(self.notebook, padding=6)
        review_page = ttk.Frame(self.notebook, padding=6)
        settings_page = ttk.Frame(self.notebook, padding=6)
        self.pages = (preview_page, review_page, settings_page)
        for page, name in (
            (preview_page, "預覽"),
            (review_page, "辨識與校訂"),
            (settings_page, "版面設定"),
        ):
            self.notebook.add(page, text=name)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        top = ttk.Frame(preview_page)
        top.pack(fill="x")
        self.button(top, "全螢幕截圖", self.capture)
        self.button(top, "載入圖片", self.load_image)
        self.button(top, "開啟紀錄", self.open_record)
        self.button(top, "清空 OCR 紀錄", self.clear_all_records)
        ttk.Label(preview_page, textvariable=self.work_title, wraplength=680).pack(anchor="w")
        pages = ttk.Frame(preview_page)
        pages.pack(fill="x")
        ttk.Label(pages, text="建議頁碼").pack(side="left")
        ttk.Label(pages, textvariable=self.page_suggestion).pack(side="left", padx=6)
        top = ttk.Frame(review_page)
        top.pack(fill="x")
        self.button(top, "開始新辨識", self.start)
        self.button(top, "續跑未完成", lambda: self.start(resume=True))
        self.button(top, "重辨識選取區域", lambda: self.start(retry=True))
        self.stop_button = ttk.Button(top, text="取消（目前欄完成後）", command=self.cancel.set)
        self.stop_button.pack(side="left", padx=3)
        self.stop_button.configure(state="disabled")

        config = ttk.LabelFrame(settings_page, text="版面參數（原圖像素，Enter 套用）")
        config.pack(fill="x", padx=5)
        self.fields = {}
        self.field_widgets = {}
        self.image_size = tk.StringVar(value="原圖：—")
        ttk.Label(preview_page, textvariable=self.image_size).pack(anchor="w", pady=5)
        self.view_mode = tk.StringVar(value="雙頁")
        viewbar = ttk.Frame(preview_page)
        viewbar.pack(fill="x")
        for mode in ("雙頁", "右頁", "左頁"):
            ttk.Radiobutton(
                viewbar, text=mode, value=mode, variable=self.view_mode, command=self.draw
            ).pack(side="left")
        groups = [
            (
                "右頁",
                [
                    ("right_x", "中上 X"),
                    ("right_y", "中上 Y"),
                    ("right_width", "框寬 X"),
                    ("right_height", "框高 Y"),
                    ("right_body_width", "正文欄寬"),
                ],
            ),
            (
                "左頁",
                [
                    ("left_x", "中上 X"),
                    ("left_y", "中上 Y"),
                    ("left_width", "框寬 X"),
                    ("left_height", "框高 Y"),
                    ("left_body_width", "正文欄寬"),
                ],
            ),
        ]
        for row, (title, labels) in enumerate(groups, start=1):
            group = ttk.LabelFrame(config, text=title, padding=5)
            group.grid(row=row, column=0, sticky="ew", padx=5, pady=4)
            for i, (key, label) in enumerate(labels):
                ttk.Label(group, text=label).grid(
                    row=i // 2, column=(i % 2) * 2, sticky="w", padx=6
                )
                var = tk.StringVar()
                e = ttk.Entry(group, textvariable=var, width=12)
                e.grid(row=i // 2, column=(i % 2) * 2 + 1, padx=6, pady=4)
                self.fields[key] = var
                self.field_widgets[key] = e
                e.bind("<Return>", lambda event: self.safe(self.apply_layout))
                e.bind("<KP_Enter>", lambda event: self.safe(self.apply_layout))
                self.controls.append(e)
        ttk.Label(
            config,
            text="虛線＝頁框，實線＝文字行；逐行模式不按行數等分。拖框會同步移動／縮放文字行；修改頁參數會清除該頁自訂框。",
            wraplength=650,
        ).grid(row=3, column=0, columnspan=13, sticky="w")
        actions = ttk.Frame(preview_page)
        actions.pack(fill="x")
        ttk.Label(actions, text="拖動模式：").pack(side="left")
        self.drag_mode = tk.StringVar(value="對側固定")
        for mode in ("對側固定", "中心固定"):
            option = ttk.Radiobutton(actions, text=mode, value=mode, variable=self.drag_mode)
            option.pack(side="left")
            self.controls.append(option)
        actions = ttk.Frame(settings_page)
        actions.pack(fill="x")
        self.button(actions, "儲存版面設定", self.save_profile)
        self.button(actions, "載入版面設定", self.load_profile)
        auto_actions = ttk.Frame(preview_page)
        auto_actions.pack(fill="x")
        self.button(auto_actions, "重新逐行偵測", self.auto_frame_x)
        self.button(auto_actions, "還原自動定位前", self.undo_auto_frame)
        self.button(auto_actions, "還原偵測結果", self.restore_lines)

        self.canvas = tk.Canvas(preview_page, bg="#303030", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Button-1>", lambda e: self.safe(lambda: self.drag_start(e)))
        self.canvas.bind("<B1-Motion>", lambda e: self.safe(lambda: self.drag_motion(e)))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.safe(lambda: self.drag_end(e)))
        self.canvas.bind("<Motion>", self.hover)
        columns = ttk.Frame(review_page)
        columns.pack(fill="x")
        columns.columnconfigure((0, 1), weight=1, uniform="review-halves")
        columns.rowconfigure(0, weight=1)
        self.review_panel = ttk.LabelFrame(columns, text="逐行檢視", padding=5)
        self.review_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        self.line_panel = ttk.LabelFrame(columns, text="逐行調整", padding=5)
        self.line_panel.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        right = self.review_panel
        self.listbox = tk.Listbox(right, height=30, exportselection=False)
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.safe(self.select))
        self.controls.append(self.listbox)
        right = review_page
        preview_bar = ttk.Frame(right)
        preview_bar.pack(fill="x")
        self.preview_info = tk.StringVar(value="")
        ttk.Label(preview_bar, textvariable=self.preview_info).pack(side="left")
        self.zoom = tk.StringVar(value="100%")
        zoom = ttk.Combobox(
            preview_bar,
            textvariable=self.zoom,
            values=("50%", "75%", "100%", "125%", "150%"),
            state="readonly",
            width=6,
        )
        zoom.pack(side="right")
        zoom.bind("<<ComboboxSelected>>", lambda event: self.render_crop())
        self.crop_canvas = tk.Canvas(right, height=60, bg="white")
        self.crop_canvas.pack(fill="x", pady=3)
        scroll = ttk.Scrollbar(right, orient="horizontal", command=self.crop_canvas.xview)
        scroll.pack(fill="x")
        self.crop_canvas.configure(xscrollcommand=scroll.set)
        ttk.Label(self.line_panel, textvariable=self.preview_info, wraplength=300).pack(
            anchor="w", pady=(12, 3)
        )
        ttk.Label(
            self.line_panel,
            text="自訂框：x1,x2；Enter 套用\n多段用 ; 分隔，空白＝略過",
            wraplength=300,
        ).pack(anchor="w")
        boxbar = ttk.Frame(self.line_panel)
        boxbar.pack(fill="x")
        self.box_value = tk.StringVar()
        entry = ttk.Entry(boxbar, textvariable=self.box_value)
        entry.pack(side="left", fill="x", expand=True)
        self.controls.append(entry)
        entry.bind("<Return>", lambda event: self.safe(self.custom_box))
        entry.bind("<KP_Enter>", lambda event: self.safe(self.custom_box))
        row_actions = ttk.Frame(self.line_panel)
        row_actions.pack(fill="x", pady=5)
        self.button(row_actions, "還原選取行", self.reset_box)
        self.button(row_actions, "刪除選取行", self.delete_line)
        add_actions = ttk.Frame(self.line_panel)
        add_actions.pack(fill="x")
        self.button(add_actions, "新增行", self.add_line)
        self.button(add_actions, "清除全部自訂框", self.clear_boxes)
        ttk.Label(right, text="選取區域文字（可校訂）").pack(anchor="w")
        self.editor = tk.Text(right, width=1, height=1, wrap="word", undo=True)
        self.editor.pack(fill="x")
        self.editor.bind("<<Modified>>", self.modified)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.button(bar, "儲存校訂", self.save_correction)
        self.button(bar, "查看原始文字", self.show_raw)
        self.button(bar, "匯出章節原文", self.export_original)
        self.result_label = tk.StringVar(value="合併文字預覽（僅接合欄位）")
        ttk.Label(right, textvariable=self.result_label).pack(anchor="w")
        self.merged = tk.Text(right, width=1, wrap="word", height=4, state="disabled")
        self.merged.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(self.root)
        self.progress.pack(fill="x", padx=5)
        footer = ttk.Frame(self.root)
        footer.pack(fill="x")
        self.button(footer, "開啟存檔資料夾", self.open_folder)
        ttk.Label(self.root, textvariable=self.status, wraplength=680).pack(
            fill="x", padx=5, pady=4
        )

    def safe(self, fn):
        try:
            fn()
        except Exception as exc:
            logger.exception("UI operation failed")
            messagebox.showerror("無法完成", str(exc), parent=self.root)
        finally:
            self.update_actions()

    def set_layout(self, layout):
        self.layout = upgrade_layout(layout)
        self._stale_pages.update((0, 1, 2))
        if self._active_tab == 2:
            self.sync_fields()

    def sync_fields(self):
        for key, var in self.fields.items():
            var.set(str(self.layout[key]))

    def on_tab_changed(self, event=None):
        target = self.notebook.index(self.notebook.select())
        if target == self._active_tab:
            return
        previous = self._active_tab
        try:
            if previous == 1:
                if not self.confirm_dirty():
                    self.notebook.select(previous)
                    return
                if self.selected is not None and self.box_value.get() != self._box_snapshot:
                    self.custom_box()
            elif previous == 2 and not self.busy:
                layout = self.read_layout()
                if layout != self.layout:
                    self.set_layout(layout)
                    self.refresh()
        except Exception as exc:  # noqa: BLE001 - keep the original tab if committing edits fails.
            self.notebook.select(previous)
            messagebox.showerror("無法切換分頁", str(exc), parent=self.root)
            return
        self._active_tab = target
        self._stale_pages.add(target)
        self.refresh_visible()

    def refresh_visible(self):
        if self._active_tab not in self._stale_pages:
            return
        self._stale_pages.discard(self._active_tab)
        if self._active_tab == 0:
            self.image_size.set(
                f"原圖：{self.image.width} × {self.image.height} px" if self.image else "原圖：—"
            )
            self.draw()
        elif self._active_tab == 1:
            self.refresh_review()
        else:
            self.sync_fields()
        self.update_actions()

    def read_layout(self):
        result = copy.deepcopy(self.layout)
        if self._active_tab != 2:
            return result
        if self.image is not None:
            result["width"], result["height"] = self.image.size
        for key, var in self.fields.items():
            self.field_widgets[key].state(["!invalid"])
            try:
                result[key] = float(var.get()) if key in ("right_x", "left_x") else int(var.get())
            except ValueError as exc:
                self.field_widgets[key].state(["invalid"])
                self.field_widgets[key].focus_set()
                raise ValueError(f"{key}：請輸入有效數值。") from exc
        for page in ("right", "left"):
            if any(
                result[page + "_" + k] != self.layout[page + "_" + k]
                for k in ("x", "y", "columns", "width", "height", "body_width")
            ):
                result["overrides"] = {
                    k: v
                    for k, v in result.get("overrides", {}).items()
                    if not k.startswith(page + "_")
                }
                remap_lines(self.layout, result, page)
        if self.image is not None:
            regions(result, self.image.size)
        return result

    def apply_layout(self):
        if not self.confirm_dirty():
            return
        self.layout = self.read_layout()
        self.refresh()
        self.status.set("版面已更新")

    def save_profile(self):
        layout = self.read_layout()
        path = filedialog.asksaveasfilename(
            initialdir=PRIVATE, defaultextension=".json", initialfile="ebook-layout.json"
        )
        if path:
            write_json(path, layout)

    def auto_frame_x(self, automatic=False):
        if self.busy:
            return
        if self.image is None:
            raise ValueError("請先截圖或載入圖片。")
        if not self.confirm_dirty():
            return
        layout = copy.deepcopy(self.layout) if automatic else self.read_layout()
        image = self.image
        self.background(
            lambda: detect_lines(image, layout),
            lambda result: self.apply_detection(*result, automatic=automatic),
        )

    def apply_detection(self, proposed, reports, *, automatic=False):
        self.detected_baseline = (self.image, copy.deepcopy(proposed))
        self.analysis_overlay = (self.image, copy.deepcopy(proposed), reports)
        accepted = [page for page, report in reports.items() if report["accepted"]]
        details = "\n".join(
            ("右頁：" if page == "right" else "左頁：") + report["reason"]
            for page, report in reports.items()
        )
        if not accepted:
            self.analysis_overlay = (self.image, copy.deepcopy(self.layout), reports)
            self.draw()
            if not automatic:
                messagebox.showinfo("未套用自動定位", details, parent=self.root)
            self.status.set(details.replace("\n", "　"))
            return
        removed = set(self.layout.get("overrides", {})) - set(proposed.get("overrides", {}))
        if (
            removed
            and not automatic
            and not messagebox.askyesno(
                "清除受影響頁的自訂框？",
                details + "\n套用會清除受影響頁的自訂框；可用還原按鈕復原。",
                parent=self.root,
            )
        ):
            return
        self.auto_frame_undo = (self.image, copy.deepcopy(self.layout))
        self.set_layout(proposed)
        self.refresh()
        counts = [len(proposed.get("line_regions", {}).get(p, [])) for p in ("right", "left")]
        self.status.set(
            f"右頁 {counts[0]} 行｜左頁 {counts[1]} 行"
            + ("｜未找到文字行" if not any(counts) else "")
            + ("｜Y 範圍無效" if any("Y 範圍無效" in r["reason"] for r in reports.values()) else "")
        )

    def undo_auto_frame(self):
        if not self.auto_frame_undo or self.auto_frame_undo[0] is not self.image:
            raise ValueError("目前圖片沒有可還原的自動定位。")
        if not self.confirm_dirty():
            return
        self.set_layout(self.auto_frame_undo[1])
        self.auto_frame_undo = None
        self.refresh()
        self.status.set("已還原")

    def apply_title_boxes(self):
        if "line_regions" in self.layout:
            messagebox.showinfo(
                "逐行模式",
                "逐行偵測已將標題作為獨立文字行；可選取該行修改 X 範圍。",
                parent=self.root,
            )
            return
        overlay = self.analysis_overlay
        if not overlay or overlay[0] is not self.image or overlay[1] != self.layout:
            raise ValueError("請先重新偵測，再確認紫色標題框。")
        if not self.confirm_dirty():
            return
        proposed = copy.deepcopy(self.layout)
        total = 0
        for page, report in overlay[2].items():
            left, _, right, _ = page_bounds(proposed, page)
            count = proposed[page + "_columns"]
            pitch = (right - left) / count
            for annotation in report.get("annotations", []):
                if annotation["kind"] != "title":
                    continue
                box = annotation["box"]
                center = (box[0] + box[2]) / 2
                index = round((right - center) / pitch - 0.5)
                if (
                    not 0 <= index < count
                    or abs(right - (index + 0.5) * pitch - center) > pitch * 0.5
                ):
                    continue
                proposed["overrides"][f"{page}_{index + 1:02}"] = [box]
                total += 1
        if not total:
            raise ValueError("沒有可套用的標題框；請手動調整或重新偵測。")
        regions(proposed, self.image.size)
        if not messagebox.askyesno(
            "確認標題框",
            f"將以 {total} 個紫色建議框取代對應欄的裁切範圍，保留標題供 OCR 辨識。請確認未涵蓋其他正文。",
            parent=self.root,
        ):
            return
        self.auto_frame_undo = (self.image, copy.deepcopy(self.layout))
        self.set_layout(proposed)
        self.analysis_overlay = (self.image, copy.deepcopy(proposed), overlay[2])
        self.refresh()
        self.status.set("標題框已套用")

    def load_profile(self):
        if not self.confirm_dirty():
            return
        path = filedialog.askopenfilename(initialdir=PRIVATE, filetypes=[("版面", "*.json")])
        if path:
            with open(path, encoding="utf-8") as f:
                layout = json.load(f)
            regions(layout, (layout["width"], layout["height"]))
            if self.image is not None:
                regions(layout, self.image.size)
            self.set_layout(layout)
            self.auto_frame_undo = None
            self.refresh()

    def accept_image(self, image, source):
        self.require_selected_work()
        metadata = {"work_id": self.work["id"]} if self.work else None
        image = image.convert("RGB")
        layout = scaled_layout(self.layout, image.size)
        session_root = self.session_root
        capture_started = self.capture_started if source == "primary-screen" else None

        def work():
            folder, data = create_session(
                image, source, session_root=session_root, metadata=metadata
            )
            if capture_started is not None:
                data["capture_seconds"] = time.perf_counter() - capture_started
                save_session(folder, data, derived=False)
            return folder, data, detect_lines(image, layout)

        def done(value):
            self.folder, self.data, detection = value
            self.image = image
            self.set_layout(layout)
            self.selected = None
            self.auto_frame_undo = None
            self.apply_detection(*detection, automatic=True)

        self.background(work, done)
        return True

    def background(self, work, done):
        self.last_error = None
        self.set_busy(True)
        self.stop_button.configure(state="disabled")
        self._job_serial += 1
        token = self._job_serial
        self._job_callbacks[token] = done
        submit(self.events, work, token)

    @property
    def session_root(self):
        return self.work_directory / "ocr" if self.work_directory else session_service.SESSIONS

    def bind_work(self, work, directory):
        if self.work and self.work["id"] == work["id"]:
            return True
        if self.busy:
            messagebox.showwarning("辨識進行中", "請等目前工作完成再切換作品。", parent=self.root)
            return False
        if not self.confirm_dirty():
            return False
        self.work, self.work_directory = work, directory
        self.work_title.set(work["title"])
        self.image = self.folder = self.data = None
        self.selected = self.drag = None
        self.auto_frame_undo = self.analysis_overlay = self.detected_baseline = None
        self.update_page_suggestion()
        self.refresh()
        session_root = self.session_root
        def restore():
            for path in sorted(session_root.glob('*/manifest.json'), reverse=True):
                try:
                    return load_session(path, session_root=session_root)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            return None
        self.background(restore, lambda result: self._record_opened(result) if result else None)
        return True

    def require_selected_work(self):
        if self.require_work and self.work is None:
            raise ValueError("請先在作品設定選取作品。")

    def update_page_suggestion(self):
        first, last = suggested_pages(self.work_directory)
        self.page_suggestion.set(
            f"{first:03d}–{last:03d}" if has_page_images(self.work_directory) else "沒有"
        )

    def clear_all_records(self):
        if getattr(self, "external_busy", lambda: False)():
            self.status.set("請先停止 Codex 提取，再清空 OCR 紀錄。")
            return
        if self.busy or self.work_directory is None:
            return
        if not messagebox.askyesno(
            "清空 OCR 紀錄",
            "永久清除目前作品的 OCR 備份與校訂紀錄？未儲存的校訂也會捨棄。",
            parent=self.root,
            default="no",
        ):
            return
        directory = self.work_directory
        self.background(lambda: clear_sessions(directory), self._records_cleared)

    def _records_cleared(self, result):
        _, failures = result
        self.image = self.folder = self.data = None
        self.photo = self.photo_key = None
        self.selected = self.drag = None
        self.auto_frame_undo = self.analysis_overlay = self.detected_baseline = None
        self.capture_started = None
        self.dirty = False
        self.set_layout(default_layout())
        self.image_size.set("原圖：—")
        self.progress.configure(value=0, maximum=1)
        self.refresh()
        self.status.set("部分紀錄未能清除。" if failures else "OCR 紀錄已清空。")
        if failures:
            messagebox.showerror("部分紀錄未能清除", "\n".join(failures), parent=self.root)

    def restore_lines(self):
        if not self.detected_baseline or self.detected_baseline[0] is not self.image:
            raise ValueError("目前圖片沒有可還原的逐行結果。")
        if self.confirm_dirty():
            self.set_layout(self.detected_baseline[1])
            self.selected = None
            self.refresh()

    def editable_lines(self):
        layout = copy.deepcopy(self.layout)
        rows = regions(layout, self.image.size)
        layout["line_regions"] = {p: [] for p in ("right", "left")}
        for row in rows:
            layout["line_regions"][row["column"].split("_")[0]].append(
                {"box": list(row["box"]), "kind": row.get("kind", "body")}
            )
        layout["overrides"] = {}
        return layout

    def add_line(self):
        if self.image is None or not self.confirm_dirty():
            return
        text = simpledialog.askstring(
            "新增文字行", "輸入原圖 X 範圍：x1,x2（自動判斷左右頁，Y 沿用頁框）", parent=self.root
        )
        if text is None:
            return
        a, b = [int(v.strip()) for v in text.split(",")]
        if not 0 <= a < b <= self.image.width:
            raise ValueError("X 範圍必須在原圖內。")
        page = "right" if (a + b) / 2 >= self.image.width / 2 else "left"
        layout = self.editable_lines()
        y = layout[page + "_y"]
        layout["line_regions"][page].append(
            {"box": [a, y, b, y + layout[page + "_height"]], "kind": "manual"}
        )
        layout["line_regions"][page].sort(key=lambda r: -(r["box"][0] + r["box"][2]))
        regions(layout, self.image.size)
        self.set_layout(layout)
        self.selected = None
        self.refresh()

    def delete_line(self):
        if self.selected is None or not self.confirm_dirty():
            return
        index = self.selected
        layout = self.editable_lines()
        for page in ("right", "left"):
            if index < len(layout["line_regions"][page]):
                del layout["line_regions"][page][index]
                break
            index -= len(layout["line_regions"][page])
        self.set_layout(layout)
        self.selected = None
        self.refresh()

    def capture(self):
        if not self.confirm_dirty():
            return
        self.require_selected_work()
        self.capture_started = time.perf_counter()
        self.set_busy(True)
        self.window.withdraw()
        self.root.after(450, self.finish_capture)

    def finish_capture(self):
        try:
            image = ImageGrab.grab(all_screens=False)
            self.window.deiconify()
            if not self.accept_image(image, "primary-screen"):
                return
        except Exception as exc:
            logger.exception("Screen capture failed")
            self.status.set(f"截圖失敗：{exc}")
            self.set_busy(False)
        finally:
            self.window.deiconify()

    def load_image(self):
        if not self.confirm_dirty():
            return
        self.require_selected_work()
        path = filedialog.askopenfilename(filetypes=[("圖片", "*.png *.jpg *.jpeg *.bmp")])
        if path:

            def load():
                with Image.open(path) as im:
                    return im.convert("RGB")

            self.background(load, lambda image: self.accept_image(image, "loaded-image"))

    def open_record(self):
        if not self.confirm_dirty():
            return
        path = filedialog.askopenfilename(
            initialdir=self.session_root, filetypes=[("紀錄", "manifest.json")]
        )
        if path:
            session_root = self.session_root
            self.background(
                lambda: load_session(path, session_root=session_root), self._record_opened
            )

    def _record_opened(self, result):
        self.folder, self.data, self.image = result
        self.auto_frame_undo = self.analysis_overlay = self.detected_baseline = None
        if self.data.get("layout"):
            self.set_layout(self.data["layout"])
        self.selected = None
        self.refresh()
        self.status.set(f"已開啟 {self.folder.name}；狀態：{self.data['status']}")

    def open_folder(self):
        folder = self.folder or self.session_root
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def display_regions(self):
        if self.image is None:
            return []
        try:
            return regions(self.layout, self.image.size)
        except ValueError:
            return []

    def refresh(self):
        previous = self.items[self.selected]["id"] if self.selected is not None else None
        self.items = self.display_regions()
        self.selected = next((i for i, row in enumerate(self.items) if row["id"] == previous), None)
        self._stale_pages.update((0, 1, 2))
        self.refresh_visible()
        self.update_actions()

    def refresh_review(self):
        # Tk Listbox ignores inserts/deletes while disabled, including programmatic updates.
        self.listbox.configure(state="normal")
        self.listbox.delete(0, "end")
        rows = {r["id"]: r for r in self.data.get("columns", [])} if self.data else {}
        for item in self.items:
            row = rows.get(item["id"], {})
            self.listbox.insert(
                "end",
                f"{item['id']}  {item.get('kind', '')}  {row.get('status', '待辨識')}"
                + ("  已校訂" if "corrected" in row else ""),
            )
        self.set_editor("")
        self.box_value.set("")
        self._box_snapshot = ""
        self.crop_canvas.delete("all")
        self.crop_canvas.configure(scrollregion=(0, 0, 0, 0))
        self.crop_photo = None
        self.preview_info.set("")
        self.update_merged()
        if self.selected is not None:
            self.listbox.selection_set(self.selected)
            self.show_selected()
        self.listbox.configure(state="disabled" if self.busy else "normal")
        self.update_actions()

    def update_merged(self):
        stale = bool(
            self.data
            and self.data.get("layout")
            and self.layout != upgrade_layout(self.data["layout"])
        )
        self.result_label.set(
            "合併文字預覽（舊版面結果，需重新辨識）" if stale else "合併文字預覽（僅接合欄位）"
        )
        text = (
            "".join(r.get("corrected", r.get("raw", "")) for r in self.data.get("columns", []))
            if self.data
            else ""
        )
        self.merged.configure(state="normal")
        self.merged.delete("1.0", "end")
        self.merged.insert("1.0", text)
        self.merged.configure(state="disabled")

    def draw(self):
        if self._active_tab != 0:
            self._stale_pages.add(0)
            return
        self.canvas.delete("all")
        if self.image is None:
            return
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        single = self.view_mode.get() != "雙頁"
        self.view_offset = self.image.width // 2 if self.view_mode.get() == "右頁" else 0
        visible_width = (
            self.image.width - self.view_offset
            if self.view_mode.get() == "右頁"
            else (self.image.width // 2 if single else self.image.width)
        )
        self.scale = min(w / visible_width, h / self.image.height)
        size = (
            max(1, round(self.image.width * self.scale)),
            max(1, round(self.image.height * self.scale)),
        )
        cache_key = (id(self.image), size)
        if getattr(self, "photo_key", None) != cache_key:
            self.photo = ImageTk.PhotoImage(self.image.resize(size))
            self.photo_key = cache_key
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        overlay = self.analysis_overlay
        if overlay and overlay[0] is self.image and overlay[1] == self.layout:
            for report in overlay[2].values():
                for annotation in report.get("annotations", []):
                    color, label = {
                        "body": ("#00b050", ""),
                        "uncertain": ("#d08000", ""),
                        "illustration": ("#e040b0", "插圖"),
                        "title": ("#9050e0", "標題"),
                        "filled": ("#00a0c0", "補欄"),
                    }[annotation["kind"]]
                    box = [v * self.scale for v in annotation["box"]]
                    self.canvas.create_rectangle(*box, outline=color, width=2, dash=(5, 3))
                    self.canvas.create_text(box[0], box[1], text=label, fill=color, anchor="nw")
        for i, item in enumerate(self.display_regions()):
            color = "red" if i == self.selected else "#00b050"
            self.canvas.create_rectangle(
                *[v * self.scale for v in item["box"]], outline=color, width=2
            )

        for page, color in [("right", "#ffb347"), ("left", "#52d4ff")]:
            a, b, c, d = page_bounds(self.layout, page)
            n = self.layout[page + "_columns"]
            for i in range(1, n):
                if page in self.layout.get("line_regions", {}):
                    break
                x = c - round(i * (c - a) / n)
                self.canvas.create_line(
                    x * self.scale,
                    b * self.scale,
                    x * self.scale,
                    d * self.scale,
                    fill="#999999",
                    dash=(2, 5),
                )
            self.canvas.create_rectangle(
                a * self.scale,
                b * self.scale,
                c * self.scale,
                d * self.scale,
                outline=color,
                width=2,
                dash=(7, 4),
                tags="page-frame",
            )
            self.canvas.create_text(
                (a + c) / 2 * self.scale,
                b * self.scale,
                anchor="s",
                text="右頁" if page == "right" else "左頁",
                fill=color,
            )
            for _, x, y in self.handles([a, b, c, d]):
                px, py = x * self.scale, y * self.scale
                self.canvas.create_rectangle(
                    px - 4, py - 4, px + 4, py + 4, fill=color, outline=color, tags="page-handle"
                )
        self.canvas.move("all", -self.view_offset * self.scale, 0)
        if single:
            boundary = visible_width * self.scale
            self.canvas.create_rectangle(
                boundary, 0, max(boundary, w), h, fill="#303030", outline="#303030"
            )

    @staticmethod
    def handles(box):
        a, b, c, d = box
        return [
            ("nw", a, b),
            ("n", (a + c) / 2, b),
            ("ne", c, b),
            ("e", c, (b + d) / 2),
            ("se", c, d),
            ("s", (a + c) / 2, d),
            ("sw", a, d),
            ("w", a, (b + d) / 2),
        ]

    def hit_page(self, event):
        if self.image is None or not self.display_regions():
            return None
        x, y = event.x / self.scale + self.view_offset, event.y / self.scale
        tolerance = 8 / self.scale
        for page in ("right", "left"):
            bounds = page_bounds(self.layout, page)
            for handle, hx, hy in self.handles(bounds):
                if abs(x - hx) <= tolerance and abs(y - hy) <= tolerance:
                    return page, handle
            a, b, c, d = bounds
            if b <= y <= d:
                if abs(x - a) <= tolerance:
                    return page, "w"
                if abs(x - c) <= tolerance:
                    return page, "e"
            if a <= x <= c:
                if abs(y - b) <= tolerance:
                    return page, "n"
                if abs(y - d) <= tolerance:
                    return page, "s"
        for page in ("right", "left"):
            a, b, c, d = page_bounds(self.layout, page)
            if a <= x <= c and b <= y <= d:
                return page, "move"
        return None

    def hover(self, event):
        if self.busy or self.drag:
            return
        hit = self.hit_page(event)
        cursors = {
            "move": "fleur",
            "n": "sb_v_double_arrow",
            "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow",
            "w": "sb_h_double_arrow",
        }
        self.canvas.configure(cursor=cursors.get(hit[1], "sizing") if hit else "")

    def drag_start(self, event):
        if self.busy or self.image is None or not self.confirm_dirty():
            return
        # Honor any typed values before starting a pointer operation.
        layout = self.read_layout()
        if layout != self.layout:
            self.set_layout(layout)
            self.refresh()
        hit = self.hit_page(event)
        if hit:
            self.drag = {
                "page": hit[0],
                "handle": hit[1],
                "x": event.x,
                "y": event.y,
                "layout": copy.deepcopy(self.layout),
                "scale": self.scale,
                "center_fixed": self.drag_mode.get() == "中心固定",
                "moved": False,
            }

    def drag_motion(self, event):
        if not self.drag or self.busy:
            return
        dx, dy = event.x - self.drag["x"], event.y - self.drag["y"]
        if not self.drag["moved"] and abs(dx) + abs(dy) < 3:
            return
        self.drag["moved"] = True
        scale = self.drag["scale"]
        layout = drag_page(
            self.drag["layout"],
            self.drag["page"],
            self.drag["handle"],
            dx / scale,
            dy / scale,
            self.image.size,
            center_fixed=self.drag["center_fixed"],
        )
        self.set_layout(layout)
        self.draw()

    def drag_end(self, event):
        drag = self.drag
        self.drag = None
        if drag and drag["moved"]:
            self.refresh()
            self.status.set("版面已更新")
        else:
            self.canvas_select(event)

    def canvas_select(self, event):
        if self.busy or self.image is None:
            return
        x, y = event.x / self.scale + self.view_offset, event.y / self.scale
        for i, item in enumerate(self.items):
            a, b, c, d = item["box"]
            if a <= x <= c and b <= y <= d:
                self.selected = i
                self._stale_pages.add(1)
                self.draw()
                self.update_actions()
                return

    def current_row(self):
        if self.selected is None or not self.data:
            return None
        if not self.data.get("layout") or self.layout != upgrade_layout(self.data["layout"]):
            return None
        key = self.items[self.selected]["id"]
        return next((r for r in self.data["columns"] if r["id"] == key), None)

    def select(self, event=None):
        choice = self.listbox.curselection()
        if not choice or self.busy:
            return
        if not self.confirm_dirty():
            return
        self.selected = choice[0]
        self.show_selected()
        self.draw()
        self.update_actions()

    def show_selected(self):
        item = self.items[self.selected]
        siblings = [r for r in self.items if r["column"] == item["column"]]
        self.box_value.set("; ".join(f"{r['box'][0]},{r['box'][2]}" for r in siblings))
        self._box_snapshot = self.box_value.get()
        row = self.current_row()
        self.set_editor(row.get("corrected", row.get("raw", "")) if row else "")
        self.editor.configure(state="normal" if row else "disabled")
        self.render_crop()

    def render_crop(self):
        if self.selected is None or self.image is None:
            return
        item = self.items[self.selected]
        crop, _ = crop_region(self.image, item["box"])
        crop = crop.transpose(Image.Transpose.ROTATE_90)
        ratio = int(self.zoom.get().rstrip("%")) / 100
        self.preview_info.set(f"{item['id']} · {crop.width} × {crop.height} px · {self.zoom.get()}")
        crop = crop.resize(
            (max(1, round(crop.width * ratio)), max(1, round(crop.height * ratio))),
            Image.Resampling.LANCZOS,
        )
        self.crop_photo = ImageTk.PhotoImage(crop)
        self.crop_canvas.delete("all")
        self.crop_canvas.create_image(0, 0, anchor="nw", image=self.crop_photo)
        self.crop_canvas.configure(scrollregion=(0, 0, crop.width, crop.height), height=60)
        self.crop_canvas.xview_moveto(0)

    def custom_box(self):
        if self.selected is None:
            raise ValueError("請選擇一個區域。")
        if not self.confirm_dirty():
            return
        text = self.box_value.get().strip()
        boxes = (
            [[int(v.strip()) for v in segment.split(",")] for segment in text.split(";")]
            if text
            else []
        )
        layout = self.read_layout()
        page = self.items[self.selected]["column"].split("_")[0]
        y = layout[page + "_y"]
        if any(len(box) != 2 for box in boxes):
            raise ValueError("自訂框格式為 x1,x2；多段用 ; 分隔。")
        boxes = [[a, y, b, y + layout[page + "_height"]] for a, b in boxes]
        layout["overrides"][self.items[self.selected]["column"]] = boxes
        regions(layout, self.image.size)
        self.set_layout(layout)
        self.refresh()

    def reset_box(self):
        if self.selected is None:
            raise ValueError("請選擇一個區域；完全略過的欄可用載入設定還原。")
        if not self.confirm_dirty():
            return
        self.layout["overrides"].pop(self.items[self.selected]["column"], None)
        self.refresh()

    def clear_boxes(self):
        if self.confirm_dirty():
            self.layout["overrides"] = {}
            self.refresh()

    def modified(self, event):
        if self.editor.edit_modified():
            self.dirty = True
            self.editor.edit_modified(False)

    def set_editor(self, text):
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self.dirty = False
        if self.selected is None:
            self.editor.configure(state="disabled")

    def confirm_dirty(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "未儲存校訂", "是否先儲存目前區域的校訂？", parent=self.root
        )
        if answer is None:
            return False
        if answer:
            self.save_correction()
        else:
            self.dirty = False
        return True

    def save_correction(self):
        row = self.current_row()
        if row is None:
            raise ValueError("請先辨識，再選取需要校訂的區域。")
        row["corrected"] = self.editor.get("1.0", "end-1c")
        save_session(self.folder, self.data)
        self.editor.edit_modified(False)
        self.dirty = False
        self.update_merged()
        self.status.set("校訂已儲存")

    def show_raw(self):
        row = self.current_row()
        if row and self.confirm_dirty():
            messagebox.showinfo("原始 OCR（唯讀）", row.get("raw", ""), parent=self.root)

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.controls:
            widget.configure(state="disabled" if busy else "normal")
        self.editor.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.update_actions()

    def update_actions(self):
        image = self.image is not None
        selected = self.selected is not None
        row = self.current_row() if selected else None
        matching = bool(
            self.data
            and self.data.get("layout")
            and self.layout == upgrade_layout(self.data["layout"])
        )
        overlay = self.analysis_overlay
        titles = bool(
            overlay
            and overlay[0] is self.image
            and overlay[1] == self.layout
            and any(
                a["kind"] == "title" for r in overlay[2].values() for a in r.get("annotations", [])
            )
        )
        enabled = {
            "清空 OCR 紀錄": self.work_directory is not None,
            "全螢幕截圖": not self.require_work or self.work is not None,
            "載入圖片": not self.require_work or self.work is not None,
            "匯出章節原文": bool(self.work_directory and self.data and self.data.get("columns")),
            "開始新辨識": image and bool(self.display_regions()),
            "續跑未完成": matching
            and any(r["status"] not in ("done", "blank") for r in self.data.get("columns", [])),
            "重辨識選取區域": bool(row and row["status"] != "blank"),
            "重新逐行偵測": image,
            "還原自動定位前": bool(self.auto_frame_undo and self.auto_frame_undo[0] is self.image),
            "還原偵測結果": bool(
                self.detected_baseline and self.detected_baseline[0] is self.image
            ),
            "套用標題框": titles and "line_regions" not in self.layout,
            "新增行": image,
            "刪除選取行": selected,
            "還原選取行": selected
            and bool(
                self.layout.get("overrides", {}).get(self.items[self.selected]["column"])
                is not None
            ),
            "清除全部自訂框": bool(self.layout.get("overrides")),
            "儲存版面設定": image,
            "儲存校訂": bool(row),
            "查看原始文字": bool(row),
        }
        for name, button in self.buttons.items():
            button.configure(
                state="normal" if not self.busy and enabled.get(name, True) else "disabled"
            )
        self.editor.configure(state="normal" if not self.busy and row else "disabled")

    def start(self, resume=False, retry=False):
        if self.busy:
            return
        if self.image is None:
            raise ValueError("請先截圖或載入圖片。")
        if not self.confirm_dirty():
            return
        layout = self.read_layout()
        indices = None
        if resume or retry:
            if not self.data.get("columns") or layout != upgrade_layout(self.data["layout"]):
                raise ValueError("需有同版面的既有結果；版面改動請開始新辨識。")
            if retry:
                row = self.current_row()
                if row is None or row["status"] == "blank":
                    raise ValueError("請選取非空白的已建立區域。")
                indices = [self.data["columns"].index(row)]
        self.operation_started = time.perf_counter()
        self.last_error = None
        self.set_busy(True)
        self.cancel.clear()
        folder, data, image = self.folder, copy.deepcopy(self.data), self.image
        session_root, engine = self.session_root, self.engine
        started, cancel, events = self.operation_started, self.cancel, self.events
        self.progress.configure(value=0, maximum=max(1, len(self.items)))

        def work():
            nonlocal folder, data
            try:
                if not (resume or retry):
                    if data.get("columns"):
                        folder, data = create_session(
                            image,
                            "rerun",
                            parent=folder.name,
                            session_root=session_root,
                            metadata=data.get("work_capture"),
                        )
                    prepare_session(folder, data, image, layout)
                events.put(("prepared", (folder, copy.deepcopy(data), layout)))
                run_recognition(
                    folder,
                    data,
                    engine,
                    cancel,
                    lambda kind, value: events.put((kind, value)),
                    indices,
                    operation_started=started,
                )
            except Exception as exc:  # noqa: BLE001 - worker errors return to Tk through the queue.
                events.put(("job_error", exc))

        threading.Thread(target=work, daemon=True, name="ebook-recognition").start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "job_error":
                    if isinstance(value, tuple):
                        token, value = value
                        self._job_callbacks.pop(token, None)
                    self.last_error = value
                    self.set_busy(False)
                    self.status.set(f"處理失敗：{value}")
                    messagebox.showerror("無法完成", str(value), parent=self.root)
                elif kind == "job_done":
                    token, result = value
                    done = self._job_callbacks.pop(token)
                    self.set_busy(False)
                    self.safe(lambda done=done, result=result: done(result))
                elif kind == "prepared":
                    self.folder, self.data, layout = value
                    self.set_layout(layout)
                    self.refresh()
                elif kind == "status":
                    self.status.set(value)
                elif kind == "progress":
                    self.progress.configure(
                        value=value["completed"], maximum=max(1, value["total"])
                    )
                    self.status.set(f"辨識 {value['completed']}/{value['total']}")
                elif kind == "finished":
                    self.data = value
                    self.set_busy(False)
                    self.refresh()
                    t = self.data["timing"]
                    self.status.set(
                        f"{self.data['status']}｜載入 {t['model_load']:.1f}s｜OCR {t['recognition']:.1f}s｜"
                        f"按下辨識至結果 {t['button_to_result_seconds']:.1f}s｜{self.folder.name}"
                        + ("｜" + self.data["error"] if self.data.get("error") else "")
                    )
        except queue.Empty:
            pass
        self.poll_id = self.root.after(100, self.poll)

    def can_close(self):
        if self.busy:
            if messagebox.askyesno("辨識進行中", "要求停止？目前欄完成後可再關閉。"):
                self.cancel.set()
            return False
        return self.confirm_dirty()

    def shutdown(self):
        self._job_callbacks.clear()
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        if hasattr(self.engine, "close"):
            self.engine.close()

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.shutdown()
