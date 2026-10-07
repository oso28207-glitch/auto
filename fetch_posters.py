"""
fetch_posters.py — جلب صور المسلسلات (إصلاح)
"""

import json
import re
import time
from pathlib import Path

import cloudscraper

from config import config, DATA_DIR

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
    headers = {"Accept": "application/json", "Referer": config.U3SEQ_BASE_URL + "/"}
    scraper = _get_scraper()
    for attempt in range(1, retries + 1):
        try:
            r = scraper.get(url, headers=headers, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                time.sleep(5 * attempt)
                continue
            print(f"      ⚠️ HTTP {r.status_code} من {url}")
            return None
        except Exception as e:
            print(f"      ⚠️ {str(e)[:100]}")
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
    print("🖼️  جلب الصور من u3seq...")
    posters = _load_posters()

    # ★ 1) جلب كل المنشورات مع featured_media
    all_posts = []
    for page in range(1, config.U3SEQ_MAX_PAGES + 1):
        # ★ استخدم _embed لضمان featured_media كامل
        posts = _api_get(
            "/wp-json/wp/v2/posts",
            params={
                "categories": config.U3SEQ_CATEGORY,
                "per_page": 100,
                "page": page,
                "_embed": "wp:featuredmedia",
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
    if not all_posts:
        return posters

    # ★★★ 2) افحص أول منشور لمعرفة البنية
    sample = all_posts[0]
    print(f"   🔍 فحص أول منشور:")
    print(f"      featured_media: {sample.get('featured_media', 'N/A')}")
    if "_embedded" in sample:
        emb = sample.get("_embedded", {})
        if "wp:featuredmedia" in emb:
            fm = emb["wp:featuredmedia"]
            if fm and isinstance(fm, list):
                print(f"      wp:featuredmedia[0].source_url: "
                      f"{fm[0].get('source_url', 'N/A')[:80]}")

    # ★★★ 3) استخرج الصور مباشرة من _embedded
    series_map = {}
    for post in all_posts:
        title = (post.get("title") or {}).get("rendered", "")
        if not title: continue

        name = _clean_series_name(title)
        if not name or name in series_map:
            continue

        # جرّب _embedded أولاً
        poster_url = ""
        emb = post.get("_embedded", {})
        if "wp:featuredmedia" in emb:
            fm = emb["wp:featuredmedia"]
            if isinstance(fm, list) and fm:
                poster_url = fm[0].get("source_url", "") or ""
                if not poster_url:
                    # جرّب sizes
                    media_details = fm[0].get("media_details", {})
                    sizes = media_details.get("sizes", {})
                    for size in ["medium_large", "large", "medium", "thumbnail"]:
                        if size in sizes:
                            poster_url = sizes[size].get("source_url", "")
                            break

        # إذا لم يُوجد في embedded، احفظ media_id للجلب لاحقاً
        media_id = post.get("featured_media", 0)
        if not poster_url and media_id:
            series_map[name] = {"media_id": media_id, "url": ""}
        elif poster_url:
            series_map[name] = {"media_id": media_id, "url": poster_url}
            posters[f"__series__{name}"] = poster_url
            print(f"      ✅ {name}: {poster_url[:80]}")

    print(f"   🎬 مسلسلات: {len(series_map)}")

    # ★★★ 4) اجلب الصور المتبقية عبر media API
    pending_ids = set()
    for info in series_map.values():
        if info["media_id"] and not info["url"]:
            pending_ids.add(info["media_id"])

    if pending_ids:
        print(f"   🔄 جلب {len(pending_ids)} صورة عبر media API...")
        media_urls = {}
        for i, mid in enumerate(pending_ids, 1):
            data = _api_get(f"/wp-json/wp/v2/media/{mid}")
            if data:
                src = data.get("source_url", "")
                if src:
                    media_urls[mid] = src
            if i % 10 == 0:
                print(f"      {i}/{len(pending_ids)}")
            time.sleep(0.3)

        # اربط
        for name, info in series_map.items():
            if not info["url"] and info["media_id"] in media_urls:
                posters[f"__series__{name}"] = media_urls[info["media_id"]]

    _save_posters(posters)

    total = len([k for k in posters if k.startswith("__series__")])
    print(f"   ✅ إجمالي الصور: {total}")
    return posters


def get_poster_map():
    posters = _load_posters()
    return {k.replace("__series__", "", 1): v
            for k, v in posters.items()
            if k.startswith("__series__")}


if __name__ == "__main__":
    fetch_posters()
    m = get_poster_map()
    print(f"\n📸 عدد الصور: {len(m)}")
    for n, u in list(m.items())[:10]:
        print(f"   · {n}: {u[:80]}")