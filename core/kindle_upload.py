"""User-confirmed Send to Kindle browser workflow; never retries a submission.

Selectors were inspected on the official US page. Login and CAPTCHA remain manual.
Each upload uses a fresh browser context; login state is not reused.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

from core.epub_export import SEND_TO_KINDLE_URL

SELECT_FILES = "#s2k-dnd-add-your-files-button"
READY = "#ready-2-send"
FILE_LIST = "#s2k-r2s-file-list"
SEND = "#s2k-r2s-send-button"
SUCCESS = "#s2k-dnd-files-on-the-way"
ERRORS = ("#s2k-dnd-modal-content", "#s2k-r2s-error-overview",
          "#s2k-dnd-upload-error-modal-content-text")
MAX_BYTES = 200 * 1024 * 1024


class KindleUploadError(RuntimeError):
    """Safe user-facing failure, optionally after a possibly sent submission."""


class UploadCancelled(KindleUploadError):
    pass


def validate_epub(path: Path) -> Path:
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".epub":
        raise KindleUploadError("請先產生有效的 EPUB 檔案。")
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise KindleUploadError("EPUB 不可為空，且大小不可超過 200 MB。")
    return path


def _official_page(page) -> bool:
    url = urlsplit(page.url)
    return (url.scheme == "https" and url.hostname == "www.amazon.com"
            and url.port in (None, 443) and url.path.rstrip("/") == "/sendtokindle")


def _visible(page, selector):
    return page.locator(selector).is_visible()


def _page_error(page):
    for selector in ERRORS:
        if _visible(page, selector):
            text = page.locator(selector).inner_text().strip()
            if text:
                raise KindleUploadError(f"Amazon 網頁回報：{text[:800]}")


def _wait(page, predicate, cancel, timeout, message):
    deadline = time.monotonic() + timeout
    while True:
        if cancel.is_set():
            raise UploadCancelled("已停止自動上傳，EPUB 已保留。")
        if page.is_closed():
            raise KindleUploadError("上傳瀏覽器已關閉。")
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise KindleUploadError(message)
        # Pump Playwright's events while the user logs in / the server responds.
        page.wait_for_timeout(200)


def _file_details(page):
    listing = page.locator(FILE_LIST)
    # Read only this upload's display fields, never account/login inputs.
    values = listing.locator("input[type=text]").evaluate_all(
        "elements => elements.map(element => element.value)")
    return (listing.inner_text().strip(), tuple(values))


def upload_on_page(page, path: Path, *, status, confirm, cancel: Event,
                   login_timeout=600, upload_timeout=300):
    """Drive a fresh page; all callbacks run on the caller's worker thread."""
    path = validate_epub(path)
    submitted = False
    try:
        status("請在新開的瀏覽器登入 Amazon／完成驗證；完成後會自動選取 EPUB。")
        page.goto(SEND_TO_KINDLE_URL, wait_until="domcontentloaded", timeout=30000)
        _wait(page, lambda: _official_page(page) and _visible(page, SELECT_FILES),
              cancel, login_timeout, "等待登入或選檔頁面逾時；請改用手動上傳。")
        if _visible(page, READY) or _visible(page, SUCCESS):
            raise KindleUploadError("上傳頁面已有其他檔案或結果，請關閉後重新開始。")
        status(f"正在選取 EPUB：{path.name}")
        with page.expect_file_chooser(timeout=10000) as chooser:
            page.locator(SELECT_FILES).click(timeout=10000)
        if cancel.is_set() or not _official_page(page):
            raise UploadCancelled("已停止選檔。")
        chooser.value.set_files(str(path), timeout=15000)

        def ready():
            if not _official_page(page):
                return False
            _page_error(page)
            return _visible(page, READY) and page.locator(SEND).is_enabled()

        _wait(page, ready, cancel, 60, "網頁未能準備好 EPUB；請檢查登入或改用手動上傳。")
        details = _file_details(page)
        status("EPUB 已選取，等待送出確認；請勿自行按網頁的 Send。")
        if not confirm("請確認瀏覽器中的 Amazon 帳號與接收位置正確。\n"
                       f"檔案：{path.name}\n\n"
                       "送出表示你同意網頁所列的 Send to Kindle 使用條款。\n"
                       "是否現在送出？"):
            raise UploadCancelled("已取消送出，EPUB 已保留。")
        if cancel.is_set():
            raise UploadCancelled("已取消送出，EPUB 已保留。")
        if (not _official_page(page) or not ready() or _visible(page, SUCCESS)
                or _file_details(page) != details):
            raise KindleUploadError("確認期間網頁內容已改變，未自動送出；請檢查網頁。")
        # From this point any error is ambiguous. Do not retry the click.
        submitted = True
        status("正在送出 EPUB，等待 Amazon 確認……")
        page.locator(SEND).click(timeout=10000)

        def completed():
            if not _official_page(page):
                return False
            _page_error(page)
            return _visible(page, SUCCESS)

        _wait(page, completed, cancel, upload_timeout, "等待 Amazon 成功訊息逾時。")
        return "Amazon 已確認接收 EPUB；Kindle 轉檔與書庫同步仍需等待。"
    except Exception as exc:
        if submitted:
            raise KindleUploadError(
                f"{exc}\n送出結果未能確認，未自動重送。請先到 Send to Kindle／書庫查證，避免重複上傳。"
            ) from exc
        if isinstance(exc, KindleUploadError):
            raise
        raise KindleUploadError(f"自動上傳停止：{exc}\nEPUB 已保留，可改用手動上傳。") from exc


def upload_epub(path: Path, *, status, confirm, cancel: Event):
    """Use a fresh isolated browser, ignoring any previously saved profile."""
    validate_epub(path)
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise KindleUploadError("缺少瀏覽器自動化元件，請更新程式或執行 uv sync；仍可手動上傳。") from exc
    with sync_playwright() as playwright:
        browser = None
        for channel in ("chrome", "msedge"):
            if cancel.is_set():
                raise UploadCancelled("已取消上傳。")
            try:
                browser = playwright.chromium.launch(channel=channel, headless=False)
                break
            except Error:
                continue
        if browser is None:
            raise KindleUploadError("無法啟動 Chrome 或 Edge；請確認已安裝且未遭 Windows 原則封鎖。")
        try:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            page.set_default_timeout(5000)
            return upload_on_page(page, path, status=status, confirm=confirm, cancel=cancel)
        finally:
            try:
                browser.close()
            except Error:
                pass
