"""
sources/yam_ahwak.py — مصدر yam.ahwaktv.net (أهواك تي في)

★ البنية:
  - الصفحة الرئيسية: /moslslat.php (قائمة المسلسلات)
  - كل مسلسل له صفحة تحتوي على حلقاته
  - الصور في وسوم img داخل بطاقات المسلسلات
"""

import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import SourceBase, MediaItem
from config import config


class YamAhwakSource(SourceBase):
    name = "yam"

    def __init__(self):
        self.base = config.YAM_BASE_URL.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ar,en;q=0.9",
        })

    def _get_soup(self, url, retries=3):
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 200:
                    return BeautifulSoup(r.text, "html.parser")
                if r.status_code == 403:
                    time.sleep(5 * attempt)
                    continue
                return None
            except Exception:
                time.sleep(3)
        return None

    def _extract_series_name(self, text):
        """يستخرج اسم المسلسل من النص."""
        t = re.sub(r"\s*الحلقة\s*\d+.*$", "", text or "").strip()
        t = re.sub(r"\s*مترجم.*$", "", t).strip()
        t = re.sub(r"\s*مدبلج.*$", "", t).strip()
        return t or text

    def _extract_ep_number(self, text):
        if not text:
            return 0
        for p in [r"الحلقة\s*(\d+)", r"[Ee]pisode\s*(\d+)", r"(\d+)"]:
            m = re.search(p, text)
            if m:
                return int(m.group(1))
        return 0

    async def fetch_items(self) -> list[MediaItem]:
        print(f"📋 [{self.name}] جلب المسلسلات...")
        url = self.base + config.YAM_SERIES_PATH
        soup = self._get_soup(url)
        if not soup:
            print(f"⚠️ فشل جلب {url}")
            return []

        items_map = {}

        # ★ البحث عن روابط المسلسلات في الصفحة
        # البنية: روابط watch.php?vid=XXX مع نص العنوان
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)

            if not href or not text:
                continue

            # روابط المشاهدة
            if "watch.php" not in href:
                continue

            full_url = urljoin(self.base, href)
            series_name = self._extract_series_name(text)
            ep_num = self._extract_ep_number(text)

            if not series_name or len(series_name) < 2:
                continue

            if series_name not in items_map:
                items_map[series_name] = MediaItem(
                    name=series_name,
                    name_ar=series_name,
                    type="series",
                    source=self.name,
                    url=full_url,
                    parts=[],
                )

            items_map[series_name].parts.append({
                "number": ep_num or len(items_map[series_name].parts) + 1,
                "url": full_url,
                "title": text,
            })

        # ترتيب الأجزاء
        for item in items_map.values():
            item.parts.sort(key=lambda x: x["number"])
            item.total_parts = len(item.parts)

        result = list(items_map.values())
        print(f"✅ [{self.name}] {len(result)} مسلسل")
        return result

    async def fetch_episodes(self, item: MediaItem) -> list[dict]:
        """يجلب الحلقات من صفحة المسلسل."""
        soup = self._get_soup(item.url)
        if not soup:
            return item.parts

        episodes = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)
            if "watch.php" in href and text:
                full_url = urljoin(self.base, href)
                ep_num = self._extract_ep_number(text)
                episodes.append({
                    "number": ep_num or len(episodes) + 1,
                    "url": full_url,
                    "title": text,
                })

        episodes.sort(key=lambda x: x["number"])
        return episodes or item.parts

    def get_episode_url(self, episode: dict) -> str:
        return episode.get("url", "")

    def cleanup(self):
        self.session.close()