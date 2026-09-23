"""Session-only Gemini safety overrides for source development."""

from collections.abc import Mapping

CATEGORIES = {
    "HARM_CATEGORY_HARASSMENT": "騷擾",
    "HARM_CATEGORY_HATE_SPEECH": "仇恨言論",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "露骨色情",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "危險內容",
}
THRESHOLDS = (
    "模型預設", "OFF", "BLOCK_NONE", "BLOCK_ONLY_HIGH",
    "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE",
)
_overrides: dict[str, str] = {}


def set_overrides(values: Mapping[str, str]) -> None:
    """Validate all inputs before atomically replacing the active snapshot."""
    if any(key not in CATEGORIES or value not in THRESHOLDS for key, value in values.items()):
        raise ValueError("無效的 Gemini 安全設定。")
    global _overrides
    _overrides = {key: value for key, value in values.items() if value != "模型預設"}


def get_overrides() -> dict[str, str]:
    return dict(_overrides)
