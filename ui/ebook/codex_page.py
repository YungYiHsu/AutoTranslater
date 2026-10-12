"""Independent screenshot-to-Codex workspace; Tk stays on its owning thread."""

import copy
import json
import queue
import threading
import time
import tkinter as tk
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageGrab

from core.ebook import usage
from core.ebook.chapters import commit_export, prepare_export
from core.ebook.codex_extract import extract
from core.ebook.exports import export_image, page_stem, suggested_pages
from core.ebook.ocr.session import clear_sessions
from core.shared.codex_models import model_choices, supported_efforts
from core.shared.codex_usage import usage_summary
from core.shared.config import load_config
from core.shared.prompt_templates import PROMPT_TEMPLATES, PromptTemplateStore
from core.shared.storage import write_json
from translators.codex_client import check_login
from ui.ebook.image_preview import ImagePreview
from ui.ebook.ocr_export import PageRangeDialog
from ui.shared.background import submit
from ui.shared.widgets import CollapsibleSection


class CodexExtractPage(ttk.Frame):
    def __init__(self, parent, *, app_directory, config_provider=None):
        super().__init__(parent, padding=8)
        self.app_directory = app_directory
        self.config_provider = config_provider or (lambda: load_config(app_directory / "config.json"))
        self.work = self.directory = self.folder = self.image = None
        self.data = {}
        self.saved = {"text": ""}
        self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.callbacks = {}
        self.serial = 0
        self.status = tk.StringVar(self)
        self.suggestion = tk.StringVar(self, "沒有")
        self.buttons = {}
        self.model_path = app_directory / 'runtime/ebook/model.json'
        try:
            model_settings = json.loads(self.model_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            model_settings = {}
        initial = self.config_provider()
        self.model = tk.StringVar(self, model_settings.get('model', initial.codex_model or 'default'))
        self.effort = tk.StringVar(self, model_settings.get('effort', initial.codex_reasoning_effort))
        self.model_catalog = []
        self.model_efforts = model_settings.get('efforts', {})
        if self.model.get() not in ('', 'default'):
            self.model_efforts.setdefault(self.model.get(), self.effort.get())
        self.models_requested = False
        self.sections = ttk.Notebook(self)
        self.sections.pack(fill="both", expand=True)
        preview_page = ttk.Frame(self.sections)
        edit_page = ttk.Frame(self.sections)
        self.sections.add(preview_page, text="圖片預覽")
        self.sections.add(edit_page, text="校訂與匯出")
        toolbar = ttk.Frame(preview_page)
        toolbar.pack(fill="x")
        for label, command in (("全螢幕截圖", self.capture), ("開啟圖片", self.open_image), ("提取內文", self.start),
                               ("取消", self.cancel.set)):
            button = ttk.Button(toolbar, text=label, command=lambda c=command: self.guard(c))
            button.pack(side="left", padx=2)
            self.buttons[label] = button
        ttk.Label(preview_page, textvariable=self.suggestion).pack(anchor="w")
        model_bar = ttk.Frame(preview_page)
        model_bar.pack(fill='x')
        ttk.Label(model_bar, text='模型').pack(side='left')
        self.model_combo = ttk.Combobox(model_bar, textvariable=self.model, state='readonly', width=25)
        self.model_combo.pack(side='left')
        effort_bar = ttk.Frame(preview_page)
        effort_bar.pack(fill='x', pady=4)
        self.effort_combo = ttk.Combobox(effort_bar, textvariable=self.effort, state='readonly', width=12)
        ttk.Label(effort_bar, text='推理強度：').pack(side='left')
        self.effort_combo.pack(side='left')
        self.model_combo.bind('<<ComboboxSelected>>', lambda e: self.guard(self.model_changed))
        self.effort_combo.bind('<<ComboboxSelected>>', lambda e: self.guard(self.save_model))
        ttk.Label(effort_bar, text='選項依模型支援程度提供，並分別記住各模型的設定。').pack(side='left')
        button = ttk.Button(model_bar, text='更新模型／檢查 Codex 登入', command=lambda: self.guard(self.refresh_models))
        button.pack(side='left')
        self.buttons['更新模型清單'] = button
        self.update_model_choices()
        self.preview_side = tk.StringVar(self, "雙頁")
        sides = ttk.Frame(preview_page)
        sides.pack(fill="x")
        for side in ("雙頁", "右頁", "左頁"):
            ttk.Radiobutton(sides, text=side, value=side, variable=self.preview_side,
                            command=self.draw).pack(side="left")
        self.full_preview = ImagePreview(preview_page, fit_width_limit=0.6)
        self.full_preview.pack(fill="both", expand=True, pady=5)
        edit_actions = ttk.Frame(edit_page)
        edit_actions.pack(fill="x", pady=4)
        export_actions = ttk.Frame(edit_page)
        export_actions.pack(side="bottom", fill="x", pady=4)
        self.splitter = ttk.Panedwindow(edit_page, orient="vertical")
        self.splitter.pack(fill="both", expand=True)
        self.edit_preview = ImagePreview(self.splitter, fit_width_limit=0.6,
                                         on_resize=self._fit_edit_preview)
        self.splitter.add(self.edit_preview, weight=1)
        self.tabs = ttk.Notebook(self.splitter)
        self.splitter.add(self.tabs, weight=1)
        self.splitter.bind("<Configure>", self._fit_edit_preview)
        self.editors = {}
        for side, title in (("text", "完整內文"),):
            frame = ttk.Frame(self.tabs)
            text = tk.Text(frame, wrap="word", undo=True, maxundo=1, height=12)
            bar = ttk.Scrollbar(frame, command=text.yview)
            text.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            text.pack(fill="both", expand=True)
            self.tabs.add(frame, text=title)
            self.editors[side] = text
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.draw())
        for label, command in (("儲存校訂", self.save), ("還原上次校訂", self.undo),
                               ("原始回覆", self.show_raw), ("匯出內文", self.export),
                               ("匯出選取文字", lambda: self.export(selection=True))):
            parent = export_actions if label.startswith("匯出") else edit_actions
            button = ttk.Button(parent, text=label, command=lambda c=command: self.guard(c))
            button.pack(side="left", padx=2)
            self.buttons[label] = button
        ttk.Label(self, textvariable=self.status, wraplength=700).pack(anchor="w")
        clear = ttk.Button(self, text="清空 OCR 紀錄", command=lambda: self.guard(self.clear))
        clear.pack(anchor="w", pady=4)
        self.buttons["清空 OCR 紀錄"] = clear
        section = CollapsibleSection(self, text="Codex 用量", padding=4)
        section.pack(fill="x")
        self.usage_text = tk.StringVar(self)
        ttk.Label(section.content, textvariable=self.usage_text, wraplength=700).pack(anchor="w")
        for label, command in (("重新整理額度", self.refresh_limits), ("作品提取紀錄", self.show_usage)):
            button = ttk.Button(section.content, text=label, command=lambda c=command: self.guard(c))
            button.pack(side="left")
            self.buttons[label] = button
        self.poll_id = self.after(100, self.poll)
        self.capture_id = None
        self.sections.bind("<<NotebookTabChanged>>", lambda e: self.draw())
        self.set_busy(False)

    def update_model_choices(self):
        self.model_combo.configure(values=model_choices(self.model_catalog))
        choices = supported_efforts(self.model_catalog, self.model.get())
        self.effort_combo.configure(values=choices, state='disabled' if self.busy or not choices else 'readonly')
        if self.effort.get() == 'default':
            self.effort.set('')

    def validate_model(self):
        if self.model.get() not in model_choices(self.model_catalog):
            raise ValueError('請先更新模型清單，再從下拉選單選擇 Codex 模型。')
        choices = supported_efforts(self.model_catalog, self.model.get())
        if not choices:
            raise ValueError('請先更新模型清單，並選擇有回報推理強度的模型。')
        if self.effort.get() not in choices:
            info = next(m for m in self.model_catalog if m['model'] == self.model.get())
            fallback = 'low' if 'low' in choices else info.get('defaultReasoningEffort')
            self.effort.set(fallback if fallback in choices else choices[0])

    def save_model(self):
        self.validate_model()
        self.model_efforts[self.model.get()] = self.effort.get()
        write_json(self.model_path, {'model': self.model.get(), 'effort': self.effort.get(), 'efforts': self.model_efforts})

    def model_changed(self):
        self.effort.set(self.model_efforts.get(self.model.get(), ''))
        self.update_model_choices()
        self.save_model()

    def refresh_models(self):
        if self.busy:
            return
        self.models_requested = True
        self.background(lambda: check_login(self.app_directory, include_models=True), self.models_loaded)

    def auto_load_models(self):
        if not self.models_requested and not self.busy:
            self.refresh_models()

    def models_loaded(self, account):
        self.model_catalog = account.get('models', [])
        choices = model_choices(self.model_catalog)
        if self.model.get() not in choices:
            self.model.set(choices[0] if choices else '')
        self.effort.set(self.model_efforts.get(self.model.get(), ''))
        self.update_model_choices()
        if choices:
            self.save_model()
        self.status.set(f"Codex 已登入；已載入 {len(choices)} 個模型。")

    def _fit_edit_preview(self, event=None):
        height = self.splitter.winfo_height()
        if height <= 50:
            return
        preview = self.edit_preview
        image_height = preview.photo.height() if preview.photo else 0
        chrome = max(0, preview.winfo_height() - preview.canvas.winfo_height())
        # Keep the editor usable in short windows; the image remains scrollable.
        target = min(image_height + chrome, max(0, height - 120))
        if self.splitter.sashpos(0) != target:
            self.splitter.sashpos(0, target)

    def guard(self, action):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - present callback failures on the UI thread.
            self.status.set(str(exc))
            messagebox.showerror("Codex 提取", str(exc), parent=self)

    def values(self):
        return {side: text.get("1.0", "end-1c") for side, text in self.editors.items()}

    def can_leave(self):
        if self.busy:
            self.status.set("請先取消並等待目前工作結束。")
            return False
        if self.values() == self.saved:
            return True
        answer = messagebox.askyesnocancel("校訂", "是否儲存目前校訂？", parent=self)
        if answer is None:
            return False
        if answer:
            self.save()
        else:
            self.fill(self.saved)
        return True

    def bind_work(self, work, directory):
        self.work, self.directory = work, directory
        self.folder = self.image = None
        self.data = {}
        self.fill({"text": ""})
        records = sorted(directory.glob("ocr/*-codex-*/codex.json"))
        for path in reversed(records):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("engine") != "codex":
                    continue
                with Image.open(path.parent / "original.png") as image:
                    self.image = image.copy()
                if "text" not in data:
                    old = data.get("pages", {})
                    data["text"] = old.get("right", "").replace("\n", "") + old.get("left", "").replace("\n", "")
                self.folder, self.data = path.parent, data
                self.fill({"text": data["text"]})
                break
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        self.load_usage()
        self.refresh()

    def load_usage(self):
        records = [r for r in usage.history(self.directory) if r.get('component') == 'ebook-extract'] if self.directory else []
        if records:
            last = records[-1]
            self.usage_text.set(usage.limits_text(last.get('after')) + '\n' + usage.model_summary(last) + '\n' + usage_summary([last])
                                + f"\n耗時 {last.get('elapsed_seconds', 0):.1f} 秒（帳號觀測值，可能包含其他任務）")
        else:
            self.usage_text.set('')

    def refresh_limits(self):
        if not self.directory or self.busy:
            return
        directory = self.directory
        self.background(lambda: usage.query(directory),
                        lambda data: self.usage_text.set(usage.limits_text(data)))

    def show_usage(self):
        if not self.directory:
            return
        window = tk.Toplevel(self)
        window.title('作品提取紀錄')
        editor = tk.Text(window, wrap='word')
        editor.pack(fill='both', expand=True)
        for entry in usage.history(self.directory):
            if entry.get('component') != 'ebook-extract':
                continue
            editor.insert('end', f"{entry.get('time', '')}\n{usage.model_summary(entry)}\n{usage_summary([entry])}\n耗時 {entry.get('elapsed_seconds', 0):.1f} 秒\n\n")
        editor.configure(state='disabled')

    def refresh(self):
        if self.folder and not self.folder.exists() and not self.busy:
            self.folder = self.image = None
            self.data = {}
            self.fill({"text": ""})
        a, b = suggested_pages(self.directory)
        self.suggestion.set(f"建議頁碼：{a:03d}–{b:03d}" if self.directory else "沒有")
        self.draw()

    def fill(self, pages):
        for side, text in self.editors.items():
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", pages.get(side, ""))
            text.edit_reset()
        self.saved = self.values()

    def draw(self):
        self.full_preview.set_image(self.image, self.preview_side.get())
        self.edit_preview.set_image(self.image, self.preview_side.get())

    def set_busy(self, value):
        self.busy = value
        for combo in (self.model_combo, self.effort_combo):
            combo.configure(state='disabled' if value else 'readonly')
        self.update_model_choices()
        for name, button in self.buttons.items():
            enabled = value if name == "取消" else not value
            button.configure(state="normal" if enabled else "disabled")
        for editor in self.editors.values():
            editor.configure(state="disabled" if value else "normal")

    def background(self, work, done):
        self.serial += 1
        self.callbacks[self.serial] = done
        self.set_busy(True)
        submit(self.events, work, self.serial)

    def poll(self):
        try:
            kind, (token, result) = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.set_busy(False)
            done = self.callbacks.pop(token)
            if kind == "job_error":
                self.status.set(str(result))
                self.load_usage()
            else:
                self.guard(lambda: done(result))
        self.poll_id = self.after(100, self.poll)

    def open_image(self):
        if self.busy:
            return
        if not self.directory:
            raise ValueError("請先選擇作品。")
        if getattr(self, "external_busy", lambda: False)():
            raise ValueError("請先等待 OCR 工作結束。")
        if not self.can_leave():
            return
        path = filedialog.askopenfilename(
            parent=self, title="開啟圖片",
            filetypes=[("圖片", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"),
                       ("所有檔案", "*.*")],
        )
        if not path:
            return
        with Image.open(path) as source:
            image = source.convert("RGB")
        self.accept_image(image)

    def capture(self):
        if not self.directory:
            raise ValueError("請先選擇作品。")
        if not self.can_leave():
            return
        if getattr(self, "external_busy", lambda: False)():
            raise ValueError("請先等待 OCR 工作結束。")
        self.set_busy(True)
        self.winfo_toplevel().withdraw()
        self.capture_id = self.after(450, self.finish_capture)

    def finish_capture(self):
        self.capture_id = None
        try:
            image = ImageGrab.grab(all_screens=False)
            self.accept_image(image)
        except Exception as exc:  # noqa: BLE001 - capture backend failures are user-visible.
            self.status.set(str(exc))
        finally:
            self.winfo_toplevel().deiconify()
            self.set_busy(False)

    def accept_image(self, image):
        folder = self.directory / "ocr" / (datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S") + "-codex-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        image.save(folder / "original.png")
        self.folder, self.image = folder, image.copy()
        self.data = {"engine": "codex", "text": ""}
        write_json(folder / "codex.json", self.data)
        self.fill({"text": self.data["text"]})
        self.refresh()
        self.status.set(f"原圖：{image.width} × {image.height}")

    def start(self):
        if self.busy or self.folder is None:
            return
        self.validate_model()
        if getattr(self, "external_busy", lambda: False)():
            raise ValueError("請先等待 OCR 工作結束。")
        if not self.can_leave():
            return
        consent = self.app_directory / "runtime/ebook/codex-consent.json"
        if not consent.exists():
            if not messagebox.askyesno("Codex 提取", "將整張截圖傳送至 Codex 提取日文內文？", parent=self):
                return
            write_json(consent, {"accepted": True})
        if (self.data.get("text")
                and not messagebox.askyesno("重新提取", "成功後取代目前提取與校訂文字？", parent=self)):
            return
        self.save_model()
        config = replace(self.config_provider(), codex_model=self.model.get(),
                         codex_reasoning_effort=self.effort.get())
        folder, cancel = self.folder, self.cancel
        cancel.clear()
        self.started = time.monotonic()
        self.status.set("Codex 提取中…")
        store = PromptTemplateStore(self.app_directory)
        store.ensure()
        contract = next(t.required_text for t in PROMPT_TEMPLATES if t.key == 'ebook_extract')
        prompt = store.read('ebook_extract').rstrip() + '\n\n' + contract
        directory = self.directory
        self.background(lambda: extract(folder, config, cancel, prompt=prompt,
                                        usage_directory=directory), self.extracted)

    def extracted(self, text):
        self.load_usage()
        self.data.update(text=text, raw_text=text)
        write_json(self.folder / "codex.json", self.data)
        self.fill({"text": text})
        self.status.set(f"提取完成，耗時 {time.monotonic() - self.started:.1f} 秒")

    def save(self):
        if self.folder is None:
            return
        data = {**self.data, **self.values()}
        write_json(self.folder / "codex.json", data)
        self.data, self.saved = data, self.values()
        self.status.set("校訂已儲存。")

    def undo(self):
        editor = list(self.editors.values())[self.tabs.index(self.tabs.select())]
        try:
            editor.edit_undo()
        except tk.TclError:
            pass

    def show_raw(self):
        if self.folder is None:
            return
        path = self.folder / "raw-response.txt"
        window = tk.Toplevel(self)
        window.title("Codex 原始回覆")
        text = tk.Text(window, wrap="word")
        text.pack(fill="both", expand=True)
        text.insert("1.0", path.read_text(encoding="utf-8") if path.exists() else "")
        text.configure(state="disabled")

    def export(self, selection=False):
        if self.busy or self.folder is None:
            return
        pages = self.values()
        if selection:
            side = "text"
            editor = self.editors[side]
            if not editor.tag_ranges("sel"):
                raise ValueError("請先選取要匯出的文字。")
            pages = {side: editor.get("sel.first", "sel.last")}
        if not any(text.strip() for text in pages.values()):
            raise ValueError("沒有可匯出的文字。")
        capture = self.data.get("work_capture", {})
        if capture.get("image") and ("/" in capture["image"] or "\\" in capture["image"]
                                     or capture["image"] in (".", "..")):
            raise ValueError("圖片檔名不合法。")
        first = capture.get("right", suggested_pages(self.directory)[0])
        chosen = PageRangeDialog(self, first, capture.get("left")).result
        if chosen is None:
            return
        if capture.get("image") and chosen != (capture["right"], capture["left"]):
            raise ValueError("這張圖片已匯出，請使用原頁碼。")
        index = self.directory / "chapters.json"
        chapters = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        number = simpledialog.askinteger("匯出原文", "章節編號", initialvalue=max(map(int, chapters), default=0), minvalue=0, parent=self)
        if number is None:
            return
        data = {"engine": "codex", "work_capture": {"work_id": self.directory.name, "right": chosen[0], "left": chosen[1]},
                "text": pages["text"], "selection_only": selection}
        plan = prepare_export(self.directory, data, self.folder.name, number)
        summary = f"第 {number:03d} 章；頁碼 {chosen[0]:03d}–{chosen[1]:03d}。"
        if plan.conflict:
            summary += "\n將取代本章這些頁面的全部舊文字（包含其他引擎的結果）。"
        if not messagebox.askyesno("確認匯出", summary, parent=self, default="no"):
            return
        directory, folder, image = self.directory, self.folder, self.image
        target = directory / 'images' / (page_stem(*chosen) + '.png')
        before = target.read_bytes() if target.exists() else None
        if before is not None and not messagebox.askyesno(
            '覆蓋圖片', f'{target.name} 已存在，是否覆蓋？', parent=self, default='no'
        ):
            return
        saved_data = {**copy.deepcopy(self.data), **self.values(), "work_capture": data["work_capture"]}
        def work():
            with export_image(directory, image, *chosen, before) as path:
                output = commit_export(plan, replace=True)
            saved_data["work_capture"]["image"] = path.name
            write_json(folder / "codex.json", saved_data)
            return output, saved_data
        self.background(work, self.exported)

    def exported(self, result):
        output, self.data = result
        self.saved = self.values()
        self.refresh()
        self.status.set(f"匯出完成：{output.name}")

    def clear(self):
        if self.busy or not self.directory:
            return
        if getattr(self, "external_busy", lambda: False)():
            raise ValueError("請先等待 OCR 工作結束。")
        if not messagebox.askyesno("清空 OCR 紀錄", "永久清除本作品兩種模式的 OCR 備份與校訂紀錄？已匯出的圖片和章節會保留。", parent=self, default="no"):
            return
        directory = self.directory
        self.background(lambda: clear_sessions(directory), self.cleared)

    def cleared(self, result):
        _, failures = result
        self.folder = self.image = None
        self.data = {}
        self.fill({"text": ""})
        self.refresh()
        self.status.set("\n".join(failures) if failures else "OCR 紀錄已清空。")

    def shutdown(self):
        self.cancel.set()
        self.after_cancel(self.poll_id)
        if self.capture_id is not None:
            self.after_cancel(self.capture_id)
            self.winfo_toplevel().deiconify()
