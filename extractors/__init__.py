"""小說內容擷取模組。"""

from extractors.base import BaseExtractor
from extractors.web_syosetu import SyosetuExtractor
from extractors.web_syosetu_work import SyosetuWorkExtractor
from extractors.work_base import BaseWorkExtractor

__all__ = ["BaseExtractor", "BaseWorkExtractor", "SyosetuExtractor", "SyosetuWorkExtractor"]
