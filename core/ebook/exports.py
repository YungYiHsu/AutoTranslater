"""Page-numbered images and chapter text exports. No LLM calls."""

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def page_stem(right, left=None):
    pages = [right] if left is None else [right, left]
    if any(type(page) is not int or page < 0 for page in pages):
        raise ValueError("頁碼必須為非負整數。")
    if left is not None and left < right:
        raise ValueError("尾頁不能小於頭頁。")
    if left == right:
        pages = [right]
    return "-".join(f"p{page:03d}" for page in pages)


def suggested_pages(directory):
    maximum = None
    if directory:
        for path in (Path(directory) / "images").glob("*.png"):
            match = re.fullmatch(r"p(\d+)(?:-p(\d+))?(?:_v\d+)?\.png", path.name)
            if match:
                value = max(int(n) for n in match.groups() if n is not None)
                maximum = value if maximum is None else max(maximum, value)
    return (1, 2) if maximum is None else (maximum + 1, maximum + 2)


def has_page_images(directory):
    return bool(directory) and any(
        re.fullmatch(r"p\d+(?:-p\d+)?(?:_v\d+)?\.png", p.name)
        for p in (Path(directory) / "images").glob("*.png")
    )


def save_page_image(directory, image, right, left=None, *, overwrite=False):
    directory = Path(directory)
    for name in ("images", "ocr", "originals", "translations"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    stem = page_stem(right, left)
    path = directory / "images" / (stem + ".png")
    if overwrite:
        from io import BytesIO
        buffer = BytesIO()
        image.convert('RGB').save(buffer, format='PNG')
        _atomic_bytes(path, buffer.getvalue())
        return path
    stream = path.open("xb")
    try:
        with stream:
            image.convert("RGB").save(stream, format="PNG")
    except Exception:
        # Only remove the new file owned by this attempt, never a prior image.
        path.unlink(missing_ok=True)
        raise
    return path


def _atomic_bytes(path, content):
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def export_image(directory, image, right, left, before):
    """Reject stale confirmations and restore the image if chapter commit fails."""
    path = Path(directory) / 'images' / (page_stem(right, left) + '.png')
    if (path.read_bytes() if path.exists() else None) != before:
        raise ValueError('圖片已變更，請重新匯出並確認。')
    saved = save_page_image(directory, image, right, left, overwrite=before is not None)
    try:
        yield saved
    except Exception:
        if before is None:
            saved.unlink(missing_ok=True)
        else:
            _atomic_bytes(saved, before)
        raise
