"""
checker.py — كشف الجديد من u.3seq.com عبر WordPress REST API.
يتجاهل المسلسلات الفاشلة.
"""

import re
import time

import cloudscraper

from config import config
from database import db
from errors import SourceError

_scraper = None


def _get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )
    return _scraper


def _extract_ep(text):
    if not text:
        return 0
    for p in [r"الحلقة\s*(\d+)", r"[Ee]pisode\s*(\d+)", r"(\d+)"]:
        m = re.search(p, text)
        if m:
            return int(m.group(1))
    return 0


def _clean_series_name(title):
    t = re.sub(r"\s*الحلقة\s*\d+.*$", "", title).strip()
    t = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", t).strip()
    t = re.sub(r"\s*مدبلجة?\s*$", "", t).strip()
    t = re.sub(r"\s*مترجمة?\s*$", "", t).strip()
    t = re.sub(r"\s*كاملة\s*$", "", t).strip()
    return t or title


def _api_get(path, params=None, retries=3):
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ")

    url = config.SOURCE_BASE_URL + path
    headers = {
        "Accept": "application/json",
        "Accept-Language": "ar,en;q=0.9",
        "Referer": config.SOURCE_BASE_URL + "/",
    }
    scraper = _get_scraper()
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            r = scraper.get(url, headers=headers, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                last_err = f"403 Cloudflare (محاولة {attempt})"
                print(f"   ⚠️ {last_err}")
                time.sleep(5 * attempt)
                continue
            if r.status_code == 404:
                raise SourceError(f"404: {url}")
            last_err = f"HTTP {r.status_code}"
            time.sleep(3)
        except SourceError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(3)

    raise SourceError(f"فشل جلب {url} — {last_err}")


def fetch_series_list():
    print(f"📋 جلب قائمة المسلسلات (تصنيف {config.SOURCE_CATEGORY})...")
    all_posts = []
    per_page = 100
    max_pages = config.SOURCE_MAX_PAGES

    for page in range(1, max_pages + 1):
        try:
            posts = _api_get(
                "/wp-json/wp/v2/posts",
                params={
                    "categories": config.SOURCE_CATEGORY,
                    "per_page": per_page,
                    "page": page,
                    "_fields": "id,link,title,date,slug",
                },
            )
        except SourceError as e:
            if "404" in str(e) and page > 1:
                break
            raise

        if not isinstance(posts, list) or not posts:
            break

        all_posts.extend(posts)
        print(f"   صفحة {page}: {len(posts)} منشور (المجموع: {len(all_posts)})")

        if len(posts) < per_page:
            break
        time.sleep(0.8)

    if not all_posts:
        raise SourceError(f"لا منشورات في التصنيف {config.SOURCE_CATEGORY}")

    series_map = {}
    for post in all_posts:
        link = post.get("link", "")
        title = (post.get("title") or {}).get("rendered", "")
        if not link or not title:
            continue

        name = _clean_series_name(title)
        ep_num = _extract_ep(title)
        if not ep_num:
            continue

        if name not in series_map:
            series_map[name] = {
                "name": name,
                "url": link,
                "slug": post.get("slug", ""),
                "posts": [],
            }

        series_map[name]["posts"].append({
            "id": post.get("id"),
            "episode": ep_num,
            "title": title,
            "link": link,
            "date": post.get("date", ""),
        })

    for s in series_map.values():
        s["posts"].sort(key=lambda x: x["episode"])

    result = list(series_map.values())
    print(f"✅ {len(result)} مسلسل، {len(all_posts)} منشور\n")
    return result


def find_new_episodes(series_data, series_name):
    """
    يقارن حلقات المصدر مع قاعدة البيانات.
    ★ يتجاهل المسلسلات التي فُشلت بالكامل.
    """
    # ★ تجاهل المسلسلات الفاشلة
    if db.has_failed(series_name):
        return []

    new = []
    for post in series_data.get("posts", []):
        ep = post["episode"]
        status = db.episode_status(series_name, ep)
        if status == "uploaded":
            continue
        if status == "failed":
            continue
        new.append(post)
    return new