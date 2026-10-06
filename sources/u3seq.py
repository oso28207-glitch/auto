"""
sources/u3seq.py — مصدر u.3seq.com (WordPress REST API)
"""

import re
import time
import cloudscraper

from .base import SourceBase, MediaItem
from config import config


class U3SeqSource(SourceBase):
    name = "u3seq"

    def __init__(self):
        self.base = config.U3SEQ_BASE_URL.rstrip("/")
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )

    def _api_get(self, path, params=None, retries=3):
        url = self.base + path
        headers = {
            "Accept": "application/json",
            "Referer": self.base + "/",
        }
        for attempt in range(1, retries + 1):
            try:
                r = self.scraper.get(url, headers=headers, params=params, timeout=60)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 403:
                    time.sleep(5 * attempt)
                    continue
                return None
            except Exception:
                time.sleep(3)
        return None

    def _extract_ep_number(self, text):
        if not text:
            return 0
        for p in [r"الحلقة\s*(\d+)", r"[Ee]pisode\s*(\d+)", r"(\d+)"]:
            m = re.search(p, text)
            if m:
                return int(m.group(1))
        return 0

    def _is_movie(self, title):
        t = title or ""
        if "الحلقة" in t or "حلقة" in t or "episode" in t.lower():
            return False
        if "فيلم" in t or "film" in t.lower() or "movie" in t.lower():
            return True
        return False

    def _clean_name(self, title):
        t = re.sub(r"\s*الحلقة\s*\d+.*$", "", title or "").strip()
        t = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", t).strip()
        t = re.sub(r"\s*مدبلجة?\s*$", "", t).strip()
        t = re.sub(r"\s*مترجمة?\s*$", "", t).strip()
        t = re.sub(r"\s*كاملة\s*$", "", t).strip()
        t = re.sub(r"\s*الجزء\s*\d+.*$", "", t).strip()
        return t or title

    async def fetch_items(self) -> list[MediaItem]:
        print(f"📋 [{self.name}] جلب العناصر...")
        all_posts = []

        for page in range(1, config.U3SEQ_MAX_PAGES + 1):
            posts = self._api_get(
                "/wp-json/wp/v2/posts",
                params={
                    "categories": config.U3SEQ_CATEGORY,
                    "per_page": 100,
                    "page": page,
                    "_fields": "id,title,featured_media,link,date,slug",
                },
            )
            if not isinstance(posts, list) or not posts:
                break
            all_posts.extend(posts)
            print(f"   صفحة {page}: {len(posts)} منشور")
            if len(posts) < 100:
                break
            time.sleep(0.5)

        # تجميع حسب العمل
        items_map = {}
        for post in all_posts:
            title = (post.get("title") or {}).get("rendered", "")
            link = post.get("link", "")
            if not title or not link:
                continue

            is_movie = self._is_movie(title)
            name = self._clean_name(title)
            ep_num = self._extract_ep_number(title)
            part_num = ep_num

            if is_movie:
                m = re.search(r"الجزء\s*(\d+)", title)
                if m:
                    part_num = int(m.group(1))

            if name not in items_map:
                items_map[name] = MediaItem(
                    name=name,
                    name_ar=name,
                    type="movie" if is_movie else "series",
                    source=self.name,
                    url=link,
                    slug=post.get("slug", ""),
                    parts=[],
                )

            items_map[name].parts.append({
                "number": part_num or len(items_map[name].parts) + 1,
                "url": link,
                "title": title,
                "media_id": post.get("featured_media", 0),
            })

        for item in items_map.values():
            item.parts.sort(key=lambda x: x["number"])
            item.total_parts = len(item.parts)

        result = list(items_map.values())
        print(f"✅ [{self.name}] {len(result)} عنصر")
        return result

    async def fetch_episodes(self, item: MediaItem) -> list[dict]:
        return item.parts

    def get_episode_url(self, episode: dict) -> str:
        return episode.get("url", "")

    def cleanup(self):
        pass