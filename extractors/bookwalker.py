"""Public BOOK WALKER Japan product metadata only; no reader or book download."""

import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

_PRODUCT_PATH = re.compile(
    r"^/(de[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)


def normalize_bookwalker_url(value):
    parsed = urlsplit(value.strip())
    match = _PRODUCT_PATH.fullmatch(parsed.path)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "bookwalker.jp" or not match):
        raise ValueError("請輸入 BOOK☆WALKER 日本站的單本商品網址（https://bookwalker.jp/de…/）。")
    product_id = match.group(1).lower()
    return f"https://bookwalker.jp/{product_id}/", product_id


def parse_bookwalker(html, url):
    url, _ = normalize_bookwalker_url(url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1.t-c-product-main-data__title")
    if title is None or not title.get_text(strip=True):
        raise ValueError("未取得商品書名，可能是存取限制或網頁格式已變更。")
    data = {"url": url, "title": title.get_text(strip=True)}
    fields = {"著者": "author", "イラスト": "illustrator", "出版社": "publisher",
              "レーベル": "label", "シリーズ": "series"}
    for container in soup.select(
        "dl.t-c-product-main-data__authors, dl.t-c-detail-about-information__data"
    ):
        for label in container.find_all("dt", recursive=False):
            key = fields.get(label.get_text(strip=True))
            value = label.find_next_sibling("dd")
            if key and value and key not in data:
                data[key] = value.get_text(" ", strip=True)
    return data


def fetch_bookwalker(url):
    url, _ = normalize_bookwalker_url(url)
    # Do not follow arbitrary redirects to another site/login/reader.
    response = requests.get(url, timeout=(5, 20), allow_redirects=False,
                            headers={"User-Agent": "AutoTranslator/0.4 ebook-metadata"})
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError("商品頁要求重新導向，請使用日本站的正式商品網址。")
    return parse_bookwalker(response.content, url)
