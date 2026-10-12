"""Conservative reading-only line breaks shared by output and developer preview."""

from dataclasses import dataclass

OPENERS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】"}
ENDINGS = frozenset("。！？!?")


@dataclass(frozen=True)
class ReadingLines:
    text: str
    # Output offsets of inserted newlines, for an exact reversible integrity check.
    inserted: tuple[int, ...]

    def restores(self, source: str) -> bool:
        added = set(self.inserted)
        return "".join(char for i, char in enumerate(self.text) if i not in added) == source


def split_for_reading(source: str) -> ReadingLines:
    """Keep dialogue together, split outside sentence endings and adjacent dialogues.

    Existing whitespace is untouched. An unmatched opening quote conservatively
    suppresses splitting until its matching close; ellipses and dashes do not split.
    """
    output, inserted, stack = [], [], []
    for index, char in enumerate(source):
        output.append(char)
        if char in OPENERS:
            stack.append(OPENERS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        if stack or index + 1 == len(source):
            continue
        following = source[index + 1]
        sentence_end = char in ENDINGS and following not in ENDINGS
        adjacent_dialogue = char in "」』" and following in "「『"
        if not (sentence_end or adjacent_dialogue):
            continue
        # Preserve existing line breaks, including those following indentation.
        ahead = index + 1
        while ahead < len(source) and source[ahead] in " \t\u3000":
            ahead += 1
        if ahead == len(source) or source[ahead] in "\r\n":
            continue
        inserted.append(len(output))
        output.append("\n")
    result = ReadingLines("".join(output), tuple(inserted))
    if not result.restores(source):
        raise ValueError("閱讀分行改動了原始文字，已停止輸出。")
    return result
