"""
fetch_posters.py — جلب صور المسلسلات من u.3seq.com (WordPress)

★ يجلب featured_media لكل منشور ويحفظ source_url.
"""

import json
import os
import re
import time
from pathlib import Path

import cloudscraper

from config import config, DATA_DIR
from errors import SourceError

POSTERS_FILE = DATA_DIR / "posters.json"
_scraper = None


def _get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )
    return _scraper


def _api_get(path, params=None, retries=3):
    url = config.U3SEQ_BASE_URL + path
    headers = {
        "Accept": "application/json",
        "Referer": config.U3SEQ_BASE_URL + "/",
    }
    scraper = _get_scraper()
    for attempt in range(1, retries + 1):
        try:
            r = scraper.get(url, headers=headers, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                time.sleep(5 * attempt)
                continue
            return None
        except Exception:
            time.sleep(3)
    return None


def _clean_series_name(title):
    t = re.sub(r"\s*الحلقة\s*\d+.*$", "", title or "").strip()
    t = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", t).strip()
    t = re.sub(r"\s*مدبلجة?\s*$", "", t).strip()
    t = re.sub(r"\s*مترجمة?\s*$", "", t).strip()
    t = re.sub(r"\s*كاملة\s*$", "", t).strip()
    return t or title


def _load_posters():
    if POSTERS_FILE.exists():
        try:
            data = json.loads(POSTERS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_posters(posters):
    POSTERS_FILE.write_text(
        json.dumps(posters, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_posters():
    """يجلب صور كل المسلسلات من WordPress media API."""
    print("🖼️  جلب صور المسلسلات من u3seq...")
    posters = _load_posters()
    print(f"   💾 صور موجودة مسبقاً: {len([k for k in posters if k.startswith('__series__')])}")

    # 1) اجلب كل المنشورات
    all_posts = []
    for page in range(1, config.U3SEQ_MAX_PAGES + 1):
        posts = _api_get(
            "/wp-json/wp/v2/posts",
            params={
                "categories": config.U3SEQ_CATEGORY,
                "per_page": 100,
                "page": page,
                "_fields": "id,title,featured_media,link",
            },
        )
        if not isinstance(posts, list) or not posts:
            break
        all_posts.extend(posts)
        print(f"   صفحة {page}: {len(posts)} منشور")
        if len(posts) < 100:
            break
        time.sleep(0.5)

    print(f"   📊 إجمالي المنشورات: {len(all_posts)}")

    # 2) اجمع media IDs الفريدة
    media_ids = set()
    series_media = {}  # series_name → media_id
    for post in all_posts:
        title = (post.get("title") or {}).get("rendered", "")
        media_id = post.get("featured_media", 0)
        if not title or not media_id:
            continue
        name = _clean_series_name(title)
        if name and name not in series_media:
            series_media[name] = media_id
            media_ids.add(media_id)

    print(f"   🎬 مسلسلات بصور: {len(series_media)}")
    print(f"   📸 صور فريدة: {len(media_ids)}")

    # 3) فلترة الصور المطلوبة
    needed = {
        mid for mid in media_ids
        if not posters.get(f"__media__{mid}")
    }

    if needed:
        print(f"   🔄 جلب {len(needed)} صورة جديدة...")
        for i, mid in enumerate(needed, 1):
            data = _api_get(f"/wp-json/wp/v2/media/{mid}")
            if not data:
                continue
            src = (data.get("source_url") or "").strip()
            if src:
                posters[f"__media__{mid}"] = src
            time.sleep(0.2)

    # 4) اربط الأسماء بالصور
    for name, mid in series_media.items():
        src = posters.get(f"__media__{mid}")
        if src:
            posters[f"__series__{name}"] = src

    _save_posters(posters)

    total = len([k for k in posters if k.startswith("__series__")])
    print(f"   ✅ إجمالي الصور: {total}")
    return posters


def get_poster_map():
    """يعيد dict: series_name → image_url."""
    posters = _load_posters()
    result = {}
    for k, v in posters.items():
        if k.startswith("__series__"):
            result[k.replace("__series__", "", 1)] = v
    return result


if __name__ == "__main__":
    fetch_posters()
    m = get_poster_map()
    print(f"\n📸 عدد المسلسلات بصور: {len(m)}")
    for name, url in list(m.items())[:5]:
        print(f"   · {name}: {url[:80]}")