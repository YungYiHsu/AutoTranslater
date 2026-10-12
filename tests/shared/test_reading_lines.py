"""Synthetic tests of reversible reading breaks and the developer-only UI."""

import tkinter as tk

import pytest

from developer.page import DeveloperPage
from developer.reading_lines import split_for_reading


@pytest.mark.parametrize(("source", "expected"), [
    ("", ""),
    ("彼は立ち止まった。「どうした？　大丈夫？」「平気だ」と彼女は答えた。",
     "彼は立ち止まった。\n「どうした？　大丈夫？」\n「平気だ」と彼女は答えた。"),
    ("えっ！？そう。次だ！", "えっ！？\nそう。\n次だ！"),
    ("「『本当？』と聞いた。」「はい」", "「『本当？』と聞いた。」\n「はい」"),
    ("まだ……でも――続く、話。", "まだ……でも――続く、話。"),
    ("一。\r\n二。\n\n三。", "一。\r\n二。\n\n三。"),
    ("一。　\n二。", "一。　\n二。"),
    ("「途中。まだ！", "「途中。まだ！"),
    ("（補足。続く）次。終。", "（補足。続く）次。\n終。"),
    ("小っゃボポ〜…―。次", "小っゃボポ〜…―。\n次"),
])
def test_breaks_only_insert_newlines(source, expected):
    result = split_for_reading(source)
    assert result.text == expected
    assert result.restores(source)
    assert split_for_reading(result.text).text == result.text


def test_developer_reading_preview_is_isolated():
    root = tk.Tk()
    root.withdraw()
    try:
        page = DeveloperPage(root, is_busy=lambda: True)
        panel = page.reading_page
        assert page.tabs.tab(1, "text") == "閱讀分行測試"
        panel.source.insert("1.0", "一。二。")
        panel.run()
        root.update()
        assert panel.source.get("1.0", "end-1c") == "一。二。"
        assert panel.result.get("1.0", "end-1c") == "一。\n二。"
        assert panel.result.cget("state") == "disabled"
        assert "一致性檢查通過" in panel.status.get()
        panel.source.insert("end", "三。")
        root.update()
        assert not panel.result.get("1.0", "end-1c")
        assert panel.copy_button.instate(["disabled"])
        panel.clear()
        assert not panel.source.get("1.0", "end-1c")
    finally:
        root.destroy()
