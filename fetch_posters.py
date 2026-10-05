"""
fetch_posters.py — جلب صور المسلسلات من u.3seq.com

★ الوظيفة:
  - يجلب featured_media لكل منشور في التصنيف.
  - يجلب source_url لكل صورة.
  - يحفظها في data/posters.json (map: series_name → image_url)
  - idempotent: لا يعيد جلب صور موجودة.
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
    url = config.SOURCE_BASE_URL + path
    headers = {
        "Accept": "application/json",
        "Referer": config.SOURCE_BASE_URL + "/",
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
            return json.loads(POSTERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_posters(posters):
    POSTERS_FILE.write_text(
        json.dumps(posters, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_posters():
    """يجلب صور كل المسلسلات ويحفظها."""
    print("🖼️  جلب صور المسلسلات...")
    posters = _load_posters()
    print(f"   💾 صور موجودة مسبقاً: {len(posters)}")

    # 1) اجلب كل المنشورات مع featured_media
    all_posts = []
    for page in range(1, config.SOURCE_MAX_PAGES + 1):
        posts = _api_get(
            "/wp-json/wp/v2/posts",
            params={
                "categories": config.SOURCE_CATEGORY,
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

    # 3) فلترة الصور المطلوبة فقط
    needed = {mid for mid in media_ids
              if str(mid) not in posters or not posters.get(str(mid))}

    if not needed:
        print("   ✅ كل الصور موجودة مسبقاً — لا شيء جديد")
        # لكن حدّث الربط بين الأسماء والصور
        updated = _update_mapping(series_media, posters)
        if updated:
            _save_posters(posters)
        return posters

    print(f"   🔄 جلب {len(needed)} صورة جديدة...")

    # 4) اجلب كل صورة على حدة
    for i, mid in enumerate(needed, 1):
        data = _api_get(f"/wp-json/wp/v2/media/{mid}")
        if not data:
            continue
        src = (data.get("source_url") or "").strip()
        if src:
            posters[str(mid)] = src
            if i % 20 == 0 or i == len(needed):
                print(f"   · {i}/{len(needed)} — {src[:80]}")
        time.sleep(0.3)

    # 5) اربط الأسماء بالصور
    _update_mapping(series_media, posters)

    _save_posters(posters)
    print(f"   ✅ حُفظت {len(posters)} صورة في {POSTERS_FILE}")
    return posters


def _update_mapping(series_media, posters):
    """يربط اسم المسلسل بالصورة في posters."""
    changed = False
    for name, mid in series_media.items():
        src = posters.get(str(mid))
        if src and posters.get(f"__series__{name}") != src:
            posters[f"__series__{name}"] = src
            changed = True
    return changed


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