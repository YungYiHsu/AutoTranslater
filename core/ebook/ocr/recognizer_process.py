"""Local JSON-lines bridge; keep model dependencies outside the desktop process."""

import base64
import io
import json
import math
import os
import queue
import subprocess
import threading
from pathlib import Path


class ProcessRecognizer:
    def __init__(self, directory, *, load_timeout=300, recognition_timeout=60):
        if any(
            not math.isfinite(value) or value <= 0 for value in (load_timeout, recognition_timeout)
        ):
            raise ValueError("OCR 逾時秒數須為正的有限數值。")
        self.directory = Path(directory)
        self.process = None
        self.versions = {}
        self.load_timeout = load_timeout
        self.recognition_timeout = recognition_timeout

    def request(self, payload):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("OCR 背景程序已結束，請重新辨識。")
        process = self.process
        result = queue.Queue(maxsize=1)

        def exchange():
            try:
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
                result.put(process.stdout.readline())
            except Exception as exc:  # noqa: BLE001 - transfer IO failures across threads.
                result.put(exc)

        thread = threading.Thread(target=exchange, daemon=True)
        thread.start()
        timeout = self.load_timeout if payload["action"] == "load" else self.recognition_timeout
        try:
            line = result.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError(
                f"OCR 背景程序超過 {timeout} 秒未回應；已保留完成結果，可續跑。"
            ) from None
        if isinstance(line, Exception):
            self.close()
            raise RuntimeError("OCR 背景程序通訊失敗，請重新辨識。") from line  # noqa: TRY004
        if not line:
            self.close()
            raise RuntimeError("OCR 背景程序已結束，請重新辨識；詳見 logs/ebook-worker.log。")
        try:
            response = json.loads(line)
            if not isinstance(response, dict):
                raise ValueError("Response must be an object")  # noqa: TRY004
        except ValueError as exc:
            self.close()
            raise RuntimeError("OCR 背景程序回應格式錯誤，請重新辨識。") from exc
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def load(self):
        if self.process is not None and self.process.poll() is not None:
            self.close()
        if self.process is None:
            python = self.directory / "runtime/ocr/venv/Scripts/python.exe"
            if not python.is_file():
                raise RuntimeError("找不到本機 OCR 環境：" + str(python))
            log = self.directory / "logs/ebook-worker.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                self.process = subprocess.Popen(
                    [str(python), "-u", str(Path(__file__).resolve()), "--worker"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stream,
                    text=True,
                    encoding="utf-8",
                    cwd=self.directory,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
        result = self.request({"action": "load"})
        self.versions = result["versions"]
        return result["seconds"]

    def recognize(self, crop):
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        result = self.request(
            {"action": "recognize", "image": base64.b64encode(buffer.getvalue()).decode("ascii")}
        )
        return result["text"], result["seconds"], result["result"]

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


def worker():
    import importlib.metadata
    import sys

    # Libraries may print diagnostics: reserve the original stdout for the protocol.
    sys.stdout.reconfigure(encoding="utf-8")
    output = sys.stdout
    sys.stdout = sys.stderr
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    import session
    from PIL import Image

    session.MODEL_CACHE = Path.cwd() / "cache/ocr"
    engine = session.Recognizer()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["action"] == "load":
                response = {
                    "seconds": engine.load(),
                    "versions": {p: importlib.metadata.version(p) for p in ("yomitoku", "torch")},
                }
            else:
                with Image.open(io.BytesIO(base64.b64decode(request["image"]))) as crop:
                    text, seconds, result = engine.recognize(crop)
                response = {"text": text, "seconds": seconds, "result": result}
        except Exception as exc:  # noqa: BLE001 - report worker errors through the protocol.
            response = {"error": str(exc)}
        output.write(json.dumps(response, ensure_ascii=True) + "\n")
        output.flush()


if __name__ == "__main__":
    worker()
