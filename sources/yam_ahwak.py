"""
sources/yam_ahwak.py — مصدر yam.ahwaktv.net (إصلاح)
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ar,en;q=0.9",
        })

    def _get_soup(self, url, retries=3):
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 200:
                    return BeautifulSoup(r.text, "html.parser")
                if r.status_code in (403, 503):
                    time.sleep(5 * attempt)
                    continue
                return None
            except Exception:
                time.sleep(3)
        return None

    def _extract_ep_number(self, text):
        if not text: return 0
        for p in [r"الحلقة\s*(\d+)", r"[Ee]pisode\s*(\d+)", r"(\d+)"]:
            m = re.search(p, text)
            if m: return int(m.group(1))
        return 0

    def _clean_name(self, text):
        t = re.sub(r"\s*الحلقة\s*\d+.*$", "", text or "").strip()
        t = re.sub(r"\s*مترجم.*$", "", t).strip()
        t = re.sub(r"\s*مدبلج.*$", "", t).strip()
        t = re.sub(r"\s*كامل.*$", "", t).strip()
        return t or text

    async def fetch_items(self) -> list[MediaItem]:
        print(f"📋 [{self.name}] جلب المسلسلات...")

        # جرّب عدة صفحات
        pages_to_try = [
            config.YAM_SERIES_PATH,
            "/",
            "/moslslat.php",
            "/series.php",
        ]

        all_links = []
        for page_path in pages_to_try:
            url = self.base + page_path if not page_path.startswith("http") else page_path
            soup = self._get_soup(url)
            if not soup:
                continue

            # ★★★ ابحث عن كل الروابط المحتملة
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if not href or not text or len(text) < 2:
                    continue
                # تجاهل الروابط الثابتة
                if any(x in href.lower() for x in [
                    "#", "javascript:", "mailto:", "facebook", "twitter",
                    "instagram", "youtube", "login", "register",
                    ".css", ".js", ".png", ".jpg", ".ico",
                ]):
                    continue
                # المسلسلات عادة فيها كلمات مثل watch/series/episode/video
                if any(x in href.lower() for x in [
                    "watch", "series", "episode", "video", "مسلسل", "moslsl",
                ]) or any(x in text for x in ["مسلسل", "حلقة"]):
                    full = urljoin(self.base, href)
                    all_links.append((full, text))

            if all_links:
                print(f"   ✅ وُجد {len(all_links)} رابط في {url}")
                break

        if not all_links:
            print(f"   ⚠️ لم يُعثر على روابط مسلسلات")
            return []

        # تجميع
        items_map = {}
        for full_url, text in all_links:
            name = self._clean_name(text)
            ep_num = self._extract_ep_number(text)

            if not name or len(name) < 2:
                continue

            if name not in items_map:
                items_map[name] = MediaItem(
                    name=name,
                    name_ar=name,
                    type="series",
                    source=self.name,
                    url=full_url,
                    parts=[],
                )
            items_map[name].parts.append({
                "number": ep_num or len(items_map[name].parts) + 1,
                "url": full_url,
                "title": text,
            })

        for item in items_map.values():
            item.parts.sort(key=lambda x: x["number"])
            item.total_parts = len(item.parts)

        result = list(items_map.values())
        print(f"✅ [{self.name}] {len(result)} مسلسل")
        return result

    async def fetch_episodes(self, item: MediaItem) -> list[dict]:
        return item.parts

    def get_episode_url(self, episode: dict) -> str:
        return episode.get("url", "")

    def cleanup(self):
        self.session.close()