"""Synthetic public-product fixtures only; no private novel samples."""

import tkinter as tk
from unittest.mock import patch

import pytest

from core.ebook.works import EbookWorkStore
from extractors.bookwalker import normalize_bookwalker_url, parse_bookwalker
from ui.ebook.work_page import EbookWorkPage

URL = "https://bookwalker.jp/de00000000-0000-0000-0000-000000000001/"
HTML = """
<h1 class="t-c-product-main-data__title">合成測試書名</h1>
<dl class="t-c-product-main-data__authors"><dt>著者</dt><dd>測試作者</dd>
<dt>イラスト</dt><dd>測試插畫</dd></dl>
<dl class="t-c-detail-about-information__data"><dt>出版社</dt><dd>測試出版社</dd>
<dt>レーベル</dt><dd>測試文庫</dd></dl>
"""


def test_parse_and_normalize_product():
    data = parse_bookwalker(HTML, URL + "?campaign=test#detail")
    assert data["url"] == URL
    assert data["title"] == "合成測試書名"
    assert data["author"] == "測試作者"
    assert data["illustrator"] == "測試插畫"
    assert data["label"] == "測試文庫"


@pytest.mark.parametrize("url", [
    "http://bookwalker.jp/", "https://bookwalker.jp/series/1/list/",
    "https://bookwalker.jp.evil.example/", "https://user@bookwalker.jp/",
    "file:///etc/passwd", "https://global.bookwalker.jp/", "",
])
def test_reject_non_product_url(url):
    with pytest.raises(ValueError):
        normalize_bookwalker_url(url)


def test_reject_interstitial():
    with pytest.raises(ValueError):
        parse_bookwalker("<h1>Login required</h1>", URL)


def test_create_reopen_and_duplicate(tmp_path):
    store = EbookWorkStore(tmp_path)
    assert store.list_works() == []
    data = parse_bookwalker(HTML, URL)
    work, created = store.create(data)
    assert created
    other, created = store.create(dict(data, title="不得覆寫"))
    assert not created
    assert other == work
    assert EbookWorkStore(tmp_path).list_works() == [work]
    with pytest.raises(ValueError):
        store.create(dict(data, title=" "))


def test_work_ui_readonly_and_create(tmp_path):
    root = tk.Tk()
    root.withdraw()
    page = EbookWorkPage(root, app_directory=tmp_path)
    page.pack()
    try:
        assert str(page.existing_combo.cget("state")) == "readonly"
        assert page.provider.get() == "bookwalker"
        page.url.set(URL)
        page.loaded_url = URL
        for key, value in parse_bookwalker(HTML, URL).items():
            if key in page.fields:
                page.fields[key].set(value)
        page.create()
        assert page.current_work["title"] == "合成測試書名"
        page.url.set(URL + "?changed=1")
        assert page.loaded_url is None
        assert str(page.create_button.cget("state")) == "disabled"
        with patch("ui.ebook.work_page.messagebox.showerror") as error:
            page.url.set("https://example.com/")
            page.fetch()
            error.assert_called_once()
        assert not page.loading
    finally:
        root.destroy()
