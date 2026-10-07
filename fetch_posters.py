"""
fetch_posters.py — جلب صور من og:image (لأن featured_media=0)
"""

import json
import re
import time
from pathlib import Path

import cloudscraper
from bs4 import BeautifulSoup

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
                time.sleep(5 * attempt); continue
            return None
        except Exception:
            time.sleep(3)
    return None


def _extract_og_image(page_url, retries=2):
    """★ يجلب og:image من HTML الصفحة."""
    scraper = _get_scraper()
    for _ in range(retries):
        try:
            r = scraper.get(page_url, timeout=30,
                            headers={"Referer": config.U3SEQ_BASE_URL + "/"})
            if r.status_code != 200:
                time.sleep(2); continue
            soup = BeautifulSoup(r.text, "html.parser")

            # og:image
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                return og["content"]

            # twitter:image
            tw = soup.find("meta", attrs={"name": "twitter:image"})
            if tw and tw.get("content"):
                return tw["content"]

            # itemprop="image"
            ip = soup.find("meta", attrs={"itemprop": "image"})
            if ip and ip.get("content"):
                return ip["content"]

            return ""
        except Exception:
            time.sleep(2)
    return ""


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
        except Exception: pass
    return {}


def _save_posters(posters):
    POSTERS_FILE.write_text(
        json.dumps(posters, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_posters():
    print("🖼️  جلب الصور من u3seq (og:image)...")
    posters = _load_posters()

    # 1) اجلب كل المنشورات (id, title, link)
    all_posts = []
    for page in range(1, config.U3SEQ_MAX_PAGES + 1):
        posts = _api_get(
            "/wp-json/wp/v2/posts",
            params={
                "categories": config.U3SEQ_CATEGORY,
                "per_page": 100,
                "page": page,
                "_fields": "id,title,link,slug",
            },
        )
        if not isinstance(posts, list) or not posts: break
        all_posts.extend(posts)
        print(f"   صفحة {page}: {len(posts)} منشور")
        if len(posts) < 100: break
        time.sleep(0.5)

    print(f"   📊 إجمالي المنشورات: {len(all_posts)}")

    # 2) استخرج الأسماء + الروابط
    series_links = {}
    for post in all_posts:
        title = (post.get("title") or {}).get("rendered", "")
        link = post.get("link", "")
        if not title or not link: continue

        name = _clean_series_name(title)
        if name and name not in series_links:
            series_links[name] = link

    print(f"   🎬 مسلسلات فريدة: {len(series_links)}")

    # 3) فلترة المطلوب فقط
    needed = {n: u for n, u in series_links.items()
              if not posters.get(f"__series__{n}")}
    print(f"   🔄 جلب {len(needed)} صورة من HTML...")

    # 4) اجلب og:image لكل صفحة
    success = 0
    for i, (name, page_url) in enumerate(needed.items(), 1):
        try:
            og = _extract_og_image(page_url)
            if og:
                posters[f"__series__{name}"] = og
                success += 1
                print(f"      [{i}/{len(needed)}] ✅ {name}: {og[:70]}")
            else:
                print(f"      [{i}/{len(needed)}] ⚠️ {name}: لا صورة")
        except Exception as e:
            print(f"      [{i}/{len(needed)}] ❌ {name}: {str(e)[:50]}")
        time.sleep(0.5)

    _save_posters(posters)
    total = len([k for k in posters if k.startswith("__series__")])
    print(f"   ✅ إجمالي الصور: {total} (+{success} جديد)")
    return posters


def get_poster_map():
    posters = _load_posters()
    return {k.replace("__series__", "", 1): v
            for k, v in posters.items() if k.startswith("__series__")}


if __name__ == "__main__":
    fetch_posters()
    m = get_poster_map()
    print(f"\n📸 عدد الصور: {len(m)}")
    for n, u in list(m.items())[:10]:
        print(f"   · {n}: {u[:80]}")