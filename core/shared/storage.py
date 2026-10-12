"""Small UTF-8 atomic file writers shared by ebook services."""

import json
import os
import tempfile
import threading
from pathlib import Path

_REPLACE_LOCKS = tuple(threading.RLock() for _ in range(32))


def atomic_text(path, text, *, encoding="utf-8", replace_file=None):
    """Durably replace a file using a unique sibling; clean up on every failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        lock = _REPLACE_LOCKS[hash(os.path.normcase(str(path.absolute()))) % len(_REPLACE_LOCKS)]
        with lock:
            if replace_file is None:
                temporary_path.replace(path)
            else:
                replace_file(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))
