"""Image extraction through the same ChatGPT-authenticated transport as web novels."""

import json
import time
import uuid

from core.ebook.extraction_thread import locked_thread, open_thread
from core.ebook.usage import observe
from core.shared.prompt_templates import PROMPT_TEMPLATES, PromptTemplateStore
from core.shared.storage import atomic_text, write_json
from translators.codex_client import CodexClient
from translators.codex_session import final_text


def default_prompt():
    from pathlib import Path
    contract = next(t.required_text for t in PROMPT_TEMPLATES if t.key == 'ebook_extract')
    return PromptTemplateStore(Path('.')).default_text('ebook_extract').rstrip() + '\n\n' + contract


def parse_response(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("回覆為空白，請查看原始回覆。")
    return text


def extract(folder, config, cancel, *, client_factory=CodexClient, prompt=None, usage_directory=None):
    with locked_thread(usage_directory or folder) as (state_path, state):
        return _extract(folder, config, cancel, client_factory=client_factory, prompt=prompt,
                        usage_directory=usage_directory, state_path=state_path, state=state)


def _extract(folder, config, cancel, *, client_factory, prompt, usage_directory, state_path, state):
    prompt = default_prompt() if prompt is None else prompt
    prompt += '\n\n本回合只提取這次附上的圖片。先前對話僅供辨識參考，不重複輸出舊圖片內文，不依前文補寫本圖沒有的文字；以本回合規則為準。'
    started = time.monotonic()
    journal = {"engine": "codex", "model": config.codex_model,
               "effort": config.codex_reasoning_effort, "status": "starting"}
    atomic_text(folder / "prompt.txt", prompt)
    try:
        with client_factory(folder) as client, observe(
            client, usage_directory or folder, cancel,
            model=config.codex_model or 'default', effort=config.codex_reasoning_effort or 'default',
        ) as usage_entry:
            account = client.account()
            if cancel.is_set():
                raise ValueError("已取消。")
            params = {"cwd": str(folder.resolve()), "approvalPolicy": "never",
                      "sandbox": "read-only", "modelProvider": "openai",
                      "baseInstructions": prompt, "config": client.translation_config()}
            if config.codex_model not in ("", "default"):
                params["model"] = config.codex_model
            opened = open_thread(client, account, params, state_path, state)
            thread = opened["thread"]["id"]
            journal.update(thread_id=thread, model=opened.get("model", config.codex_model))
            usage_entry['model'] = opened.get('model') or usage_entry['model']
            usage_entry['thread_id'] = thread
            write_json(folder / "request.json", journal)
            if cancel.is_set():
                raise ValueError("已取消。")
            marker = 'EBOOK_EXTRACTION_' + uuid.uuid4().hex
            request = {"threadId": thread, "input": [
                {"type": "text", "text": prompt + '\n請求識別（不要輸出）：' + marker},
                {"type": "localImage", "path": str((folder / "original.png").resolve())}
            ]}
            if config.codex_reasoning_effort not in ("", "default"):
                request["effort"] = config.codex_reasoning_effort
            journal["status"] = "sending"
            write_json(folder / "request.json", journal)
            state['pending'] = {'marker': marker, 'record': folder.name}
            write_json(state_path, state)
            turn_id = client.call("turn/start", request)["turn"]["id"]
            state['pending']['turn_id'] = turn_id
            write_json(state_path, state)
            usage_entry['turn_id'] = turn_id
            journal.update(turn_id=turn_id, status="inProgress")
            write_json(folder / "request.json", journal)
            client.wait_turn(thread, turn_id, cancel=cancel)
            history = client.call("thread/read", {"threadId": thread, "includeTurns": True})
            turn = next(t for t in history["thread"]["turns"] if t["id"] == turn_id)
            if turn.get('status') in ('completed', 'failed', 'interrupted'):
                state.pop('pending', None)
                write_json(state_path, state)
            atomic_text(folder / "raw-turn.json", json.dumps(turn, ensure_ascii=False, indent=2))
            text = final_text(turn)
            atomic_text(folder / "raw-response.txt", text)
            extracted_text = parse_response(text)
            if cancel.is_set():
                raise ValueError("已取消，回覆已保留。")
            journal["status"] = "completed"
            return extracted_text
    except Exception as exc:
        journal.update(status="cancelled" if cancel.is_set() else "failed", error=str(exc))
        raise
    finally:
        journal["elapsed_seconds"] = time.monotonic() - started
        write_json(folder / "request.json", journal)
