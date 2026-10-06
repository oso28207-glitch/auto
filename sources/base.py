"""
sources/base.py — واجهة موحدة لأي مصدر محتوى
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MediaItem:
    """عنصر وسائط موحد (مسلسل أو فيلم)."""
    name: str                          # الاسم النظيف
    name_ar: str = ""                  # الاسم العربي
    type: str = "series"               # series | movie
    source: str = ""                   # u3seq | yam | egybest
    url: str = ""                      # رابط الصفحة الأصلية
    poster: str = ""                   # رابط الصورة
    season: int = 0                    # رقم الموسم (0 = بدون)
    total_parts: int = 0               # إجمالي الأجزاء/الحلقات
    parts: list = field(default_factory=list)  # [{"number": 1, "url": "..."}]
    slug: str = ""


class SourceBase(ABC):
    """واجهة المصدر الأساسية."""

    name: str = "base"

    @abstractmethod
    async def fetch_items(self) -> list[MediaItem]:
        """يجلب كل العناصر (مسلسلات + أفلام)."""
        pass

    @abstractmethod
    async def fetch_episodes(self, item: MediaItem) -> list[dict]:
        """يجلب حلقات/أجزاء عنصر معين."""
        pass

    @abstractmethod
    def get_episode_url(self, episode: dict) -> str:
        """يعيد رابط التحميل الفعلي لحلقة."""
        pass

    def cleanup(self):
        """تنظيف الموارد (اختياري)."""
        pass