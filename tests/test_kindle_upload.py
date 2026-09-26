"""Synthetic browser tests: no Amazon requests, login, or real uploads."""

import os
from threading import Event
from unittest.mock import MagicMock

import pytest

from core.kindle_upload import (
    ERRORS,
    FILE_LIST,
    MAX_BYTES,
    READY,
    SELECT_FILES,
    SEND,
    SUCCESS,
    KindleUploadError,
    UploadCancelled,
    _official_page,
    upload_epub,
    upload_on_page,
    validate_epub,
)


@pytest.fixture
def epub(tmp_path):
    path = tmp_path / "0001-0010 - 測試.epub"
    path.write_bytes(b"test fixture, not sent to Amazon")
    return path


class Page:
    def __init__(self):
        self.url = "https://www.amazon.com/sendtokindle"
        self.stage = "select"
        self.send_count = 0
        self.select_count = 0
        self.error = ""
        self.closed = False
        self.file_details = "fixture.epub"
        self.chooser = MagicMock()
        self.chooser.value.set_files.side_effect = self.select_file
        self.sent_error = None
        self.complete = True

    def goto(self, *args, **kwargs):
        pass

    def is_closed(self):
        return self.closed

    def expect_file_chooser(self, **kwargs):
        context = MagicMock()
        context.__enter__.return_value = self.chooser
        return context

    def select_file(self, *args, **kwargs):
        self.select_count += 1
        self.stage = "ready"

    def locator(self, selector):
        locator = MagicMock()
        locator.is_visible.side_effect = lambda: (
            (selector == SELECT_FILES and self.stage == "select")
            or (selector == READY and self.stage == "ready")
            or (selector == SUCCESS and self.stage == "success")
            or (selector == ERRORS[0] and bool(self.error)))
        locator.is_enabled.return_value = self.stage == "ready"
        locator.inner_text.side_effect = lambda: self.file_details if selector == FILE_LIST else self.error
        locator.locator.return_value.evaluate_all.return_value = ["fixture"]
        if selector == SEND:
            locator.click.side_effect = self.send
        return locator

    def send(self, **kwargs):
        self.send_count += 1
        if self.sent_error:
            raise self.sent_error
        if self.complete:
            self.stage = "success"

    def wait_for_timeout(self, timeout):
        pass


def run(page, epub, **kwargs):
    return upload_on_page(page, epub, status=lambda text: None,
                          confirm=kwargs.pop("confirm", lambda text: True),
                          cancel=kwargs.pop("cancel", Event()), **kwargs)


def test_success_requires_confirmation_and_one_submission(epub):
    page = Page()

    def confirm(message):
        assert page.select_count == 1 and page.send_count == 0
        assert epub.name in message and "使用條款" in message
        return True

    assert "Amazon 已確認" in run(page, epub, confirm=confirm)
    assert page.send_count == 1
    assert page.chooser.value.set_files.call_args.args == (str(epub.resolve()),)


def test_rejected_confirmation_never_sends(epub):
    page = Page()
    with pytest.raises(UploadCancelled):
        run(page, epub, confirm=lambda message: False)
    assert page.send_count == 0 and epub.exists()


def test_cancel_before_select(epub):
    page, cancel = Page(), Event()
    cancel.set()
    with pytest.raises(UploadCancelled):
        run(page, epub, cancel=cancel)
    assert page.select_count == page.send_count == 0


def test_cancel_after_select(epub):
    page, cancel = Page(), Event()

    def confirm(message):
        cancel.set()
        return True

    with pytest.raises(UploadCancelled):
        run(page, epub, confirm=confirm, cancel=cancel)
    assert page.send_count == 0


def test_page_error_before_send(epub):
    page = Page()
    page.error = "File type not supported"
    with pytest.raises(KindleUploadError, match="File type not supported"):
        run(page, epub)
    assert page.send_count == 0


@pytest.mark.parametrize("change", ["file", "url", "manual_send"])
def test_changed_page_after_confirmation_never_sends(epub, change):
    page = Page()

    def confirm(message):
        if change == "file":
            page.file_details = "different.epub"
        elif change == "url":
            page.url = "https://example.com/sendtokindle"
        else:
            page.stage = "success"
        return True

    with pytest.raises(KindleUploadError, match="內容已改變"):
        run(page, epub, confirm=confirm)
    assert page.send_count == 0


def test_ambiguous_click_not_retried(epub):
    page = Page()
    page.sent_error = RuntimeError("connection lost")
    with pytest.raises(KindleUploadError, match="未自動重送"):
        run(page, epub)
    assert page.send_count == 1


def test_success_timeout_not_retried(epub):
    page = Page()
    page.complete = False
    with pytest.raises(KindleUploadError, match="未自動重送"):
        run(page, epub, upload_timeout=0)
    assert page.send_count == 1


def test_login_timeout_never_selects(epub):
    page = Page()
    page.url = "https://www.amazon.com/ap/signin"
    with pytest.raises(KindleUploadError, match="等待登入"):
        run(page, epub, login_timeout=0)
    assert page.select_count == page.send_count == 0


def test_browser_closed_before_upload(epub):
    page = Page()
    page.closed = True
    with pytest.raises(KindleUploadError, match="瀏覽器已關閉"):
        run(page, epub)
    assert page.select_count == 0


@pytest.mark.parametrize("url", ["http://www.amazon.com/sendtokindle",
    "https://www.amazon.com.evil.test/sendtokindle", "https://www.amazon.com/ap/signin",
    "https://www.amazon.com:8443/sendtokindle"])
def test_refuse_unexpected_destination(url):
    page = Page()
    page.url = url
    assert not _official_page(page)


def test_validate_file(epub):
    assert validate_epub(epub) == epub.resolve()
    epub.write_bytes(b"")
    with pytest.raises(KindleUploadError, match="不可為空"):
        validate_epub(epub)
    with epub.open("wb") as stream:
        stream.truncate(MAX_BYTES + 1)
    with pytest.raises(KindleUploadError, match="200 MB"):
        validate_epub(epub)
    epub.unlink()
    with pytest.raises(FileNotFoundError):
        validate_epub(epub)


def test_each_upload_launches_fresh_browser_and_closes(epub, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = runtime
    browser = runtime.chromium.launch.return_value
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: manager)
    monkeypatch.setattr("core.kindle_upload.upload_on_page", lambda *a, **k: "done")
    for _ in range(2):
        assert upload_epub(epub, status=lambda text: None, confirm=lambda text: True, cancel=Event()) == "done"
    assert runtime.chromium.launch.call_count == 2
    assert browser.new_context.call_count == 2
    assert browser.close.call_count == 2
    runtime.chromium.launch_persistent_context.assert_not_called()
    assert not (tmp_path / "AutoTranslator").exists()


def test_chrome_failure_falls_back_to_edge(epub, monkeypatch):
    from playwright.sync_api import Error

    runtime, manager, browser = MagicMock(), MagicMock(), MagicMock()
    manager.__enter__.return_value = runtime
    runtime.chromium.launch.side_effect = [Error("launch failed"), browser]
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: manager)
    monkeypatch.setattr("core.kindle_upload.upload_on_page", lambda *a, **k: "done")
    assert upload_epub(epub, status=lambda text: None, confirm=lambda text: True, cancel=Event()) == "done"
    assert [call.kwargs["channel"] for call in runtime.chromium.launch.call_args_list] == ["chrome", "msedge"]
    browser.close.assert_called_once()


@pytest.mark.skipif(not os.getenv("AUTOTRANSLATOR_BROWSER_TEST"), reason="Opt-in installed-browser smoke test")
def test_installed_browser_with_fully_intercepted_page(epub):
    from playwright.sync_api import Error, sync_playwright

    markup = """<!doctype html><html><body>
    <button id="s2k-dnd-add-your-files-button" onclick="document.querySelector('input').click()">Select</button>
    <input type="file" hidden onchange="document.querySelector('#ready-2-send').hidden=false;
      document.querySelector('#s2k-r2s-file-list').textContent=this.files[0].name">
    <div id="ready-2-send" hidden><div id="s2k-r2s-file-list"></div>
    <button id="s2k-r2s-send-button" onclick="document.querySelector('#ready-2-send').hidden=true;
      document.querySelector('#s2k-dnd-files-on-the-way').hidden=false">Send</button></div>
    <div id="s2k-dnd-files-on-the-way" hidden>Your files are on the way</div>
    </body></html>"""
    with sync_playwright() as playwright:
        browser = None
        for channel in ("chrome", "msedge"):
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True)
                break
            except Error:
                continue
        assert browser is not None, "No supported installed browser could launch"
        try:
            context = browser.new_context(service_workers="block")
            # No request reaches Amazon or any other host, including subresources.
            context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
            page = context.new_page()
            assert "Amazon 已確認" in run(page, epub)
        finally:
            browser.close()
