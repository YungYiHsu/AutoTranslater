"""Local ebook metadata, independent of website chapter translation models."""

import json
from pathlib import Path

from core.shared.config import load_config
from core.shared.paths import resolve_runtime_path
from extractors.bookwalker import normalize_bookwalker_url


class EbookWorkStore:
    def __init__(self, app_directory):
        app_directory = Path(app_directory)
        config = load_config(app_directory / "config.json")
        self.directory = (
            resolve_runtime_path(config.output_directory, app_dir=app_directory)
            / "ebook"
            / "bookwalker"
        )

    def work_directory(self, work):
        _, product_id = normalize_bookwalker_url(work["url"])
        work_id = "bookwalker-" + product_id
        if work.get("id") != work_id:
            raise ValueError("作品 ID 與網址不符。")
        folder = self.directory / work_id
        if not (folder / "work.json").is_file():
            raise ValueError("作品資料夾不存在。")
        return folder

    def list_works(self):
        works = [self._read_work(path) for path in self.directory.glob("bookwalker-*/work.json")]
        return sorted(works, key=lambda work: work["title"])

    @staticmethod
    def _read_work(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        url, product_id = normalize_bookwalker_url(data["url"])
        if (
            path.parent.name != "bookwalker-" + product_id
            or data.get("id") != path.parent.name
            or data.get("provider") != "bookwalker"
            or not data["title"].strip()
        ):
            raise ValueError(f"作品資料格式錯誤：{path}")
        data["url"] = url
        return data

    def create(self, metadata):
        url, product_id = normalize_bookwalker_url(metadata["url"])
        title = metadata["title"].strip()
        if not title:
            raise ValueError("請填寫書名。")
        work_id = "bookwalker-" + product_id
        existing = self.directory / work_id / "work.json"
        if existing.exists():
            return self._read_work(existing), False
        data = {
            key: str(metadata.get(key, "")).strip()
            for key in ("title", "author", "illustrator", "publisher", "label", "series")
        }
        data.update(schema_version=1, id=work_id, provider="bookwalker", url=url)
        folder = self.directory / work_id
        folder.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: never overwrite a prior work, including invalid metadata.
        with (folder / "work.json").open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        return data, True
