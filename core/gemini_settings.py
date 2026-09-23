"""Apply source-only overrides while frozen applications retain provider defaults."""

import sys
from typing import Any


def request_config(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    if not getattr(sys, "frozen", False):
        from developer.settings import get_overrides

        overrides = get_overrides()
        if overrides:
            result["safety_settings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in overrides.items()
            ]
    return result
