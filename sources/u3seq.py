"""
sources/u3seq.py — جلب المسلسلات من u.3seq.cam
═══════════════════════════════════════════════════════════
يعتمد على WordPress REST API:
  GET /wp-json/wp/v2/posts?per_page=100&page=N

★ لا نرسل categories عندما تكون 0 (WordPress يفسّرها كتصنيف غير موجود).
★ عند فشل wp-json، يحاول scrape HTML مباشرة كخطة بديلة.
"""

import re
import time
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup

from config import config
from errors import SourceError

# ═══════════════════════════════════════════════════════════════
# cloudscraper (مُنشأ مرة واحدة)
# ═══════════════════════════════════════════════════════════════
_scraper = None


def _get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
            },
            delay=5,
        )
    return _scraper


# ═══════════════════════════════════════════════════════════════
# استخراج
# ═══════════════════════════════════════════════════════════════
def _extract_ep(text: str) -> int:
    """يستخرج رقم الحلقة من النص."""
    if not text:
        return 0
    for p in [
        r"الحلقة\s*(\d+)",
        r"[Ee]pisode\s*(\d+)",
        r"حلقة\s*(\d+)",
        r"Ep\.?\s*(\d+)",
    ]:
        m = re.search(p, text)
        if m:
            return int(m.group(1))
    return 0


def _clean_series_name(title: str) -> str:
    t = re.sub(r"\s*الحلقة\s*\d+.*$", "", title or "").strip()
    t = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", t).strip()
    t = re.sub(r"\s*مدبلجة?\s*$", "", t).strip()
    t = re.sub(r"\s*مترجمة?\s*$", "", t).strip()
    t = re.sub(r"\s*كاملة\s*$", "", t).strip()
    return t or title


# ═══════════════════════════════════════════════════════════════
# API helper
# ═══════════════════════════════════════════════════════════════
def _api_get(path: str, params: Optional[Dict] = None, retries: int = 3) -> Optional[Any]:
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ")

    url = config.SOURCE_BASE_URL.rstrip("/") + path
    headers = {
        "Accept": "application/json",
        "Accept-Language": "ar,en;q=0.9",
        "Referer": config.SOURCE_BASE_URL + "/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    scraper = _get_scraper()
    last_err = None

    for attempt in range(1, retries + 1):
        try:
            r = scraper.get(url, headers=headers, params=params, timeout=60)

            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:
                    raise SourceError(f"فشل تحليل JSON: {e}")

            if r.status_code == 400:
                # 400 = bad request، غالباً categories غير صحيح
                # نرجع None ليجرّب المسار البديل
                print(f"   ⚠️ 400 — لا نستمر مع هذه المعطيات")
                return None

            if r.status_code == 403:
                last_err = f"403 Cloudflare (محاولة {attempt})"
                print(f"   ⚠️ {last_err}")
                time.sleep(5 * attempt)
                continue

            if r.status_code == 404:
                return None

            last_err = f"HTTP {r.status_code}"
            time.sleep(3)

        except SourceError:
            raise
        except Exception as e:
            last_err = str(e)[:150]
            time.sleep(3)

    raise SourceError(f"فشل جلب {url} — {last_err}")


# ═══════════════════════════════════════════════════════════════
# المحاولة 1: WordPress REST API
# ═══════════════════════════════════════════════════════════════
def _fetch_via_api() -> List[Dict]:
    """يجلب من wp-json. لا يرسل categories إذا كانت 0."""
    print(f"   📡 محاولة WordPress REST API...")
    all_posts = []
    per_page = 100
    max_pages = config.SOURCE_MAX_PAGES

    for page in range(1, max_pages + 1):
        params = {
            "per_page": per_page,
            "page": page,
            "_fields": "id,link,title,date,slug",
        }
        # ★ لا نرسل categories إذا كانت 0
        if config.SOURCE_CATEGORY and config.SOURCE_CATEGORY > 0:
            params["categories"] = config.SOURCE_CATEGORY

        try:
            posts = _api_get("/wp-json/wp/v2/posts", params=params)
        except SourceError as e:
            print(f"   ⚠️ فشل: {str(e)[:120]}")
            return []

        if posts is None:
            # قد تكون 400 لأن categories غير صحيح
            if config.SOURCE_CATEGORY and config.SOURCE_CATEGORY > 0:
                print(f"   🔄 إعادة محاولة بدون تصنيف...")
                params.pop("categories", None)
                try:
                    posts = _api_get("/wp-json/wp/v2/posts", params=params)
                except SourceError:
                    return []
            if posts is None:
                return []

        if not isinstance(posts, list) or not posts:
            break

        all_posts.extend(posts)
        print(f"   صفحة {page}: {len(posts)} منشور (المجموع: {len(all_posts)})")

        if len(posts) < per_page:
            break

        time.sleep(0.8)

    return _group_into_series(all_posts)


# ═══════════════════════════════════════════════════════════════
# المحاولة 2: زحف HTML
# ═══════════════════════════════════════════════════════════════
def _fetch_via_html() -> List[Dict]:
    """يقرأ الصفحة الرئيسية ويستخرج المسلسلات."""
    print(f"   📡 محاولة زحف HTML...")
    base = config.SOURCE_BASE_URL.rstrip("/")
    urls_to_try = [
        base + "/",
        base + "/series/",
        base + "/moslslat/",
        base + "/tv/",
    ]

    scraper = _get_scraper()
    all_links = []

    for url in urls_to_try:
        try:
            r = scraper.get(url, timeout=30)
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            # ابحث عن روابط تحتوي على كلمات مفتاحية
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not text or len(text) < 3:
                    continue
                # علامات: video/series/watch/episode
                low = href.lower()
                if any(k in low for k in [
                    "/video/", "/series/", "/watch/", "/episode/",
                    "modablaj-", "/moslslat/",
                ]):
                    full = href if href.startswith("http") else urljoin(base + "/", href)
                    all_links.append({"link": full, "title": text})

            if all_links:
                print(f"   ✅ عُثر على {len(all_links)} رابط من {url}")
                break
        except Exception as e:
            print(f"   ⚠️ {url}: {str(e)[:80]}")
            continue

    if not all_links:
        print(f"   ❌ لم يُعثر على روابط")
        return []

    # نحوّل الروابط لصيغة منشورات
    posts = []
    seen = set()
    for item in all_links:
        link = item["link"]
        title = item["title"]
        if link in seen:
            continue
        seen.add(link)
        posts.append({
            "link": link,
            "title": {"rendered": title},
            "slug": link.rstrip("/").split("/")[-1],
        })

    return _group_into_series(posts)


# ═══════════════════════════════════════════════════════════════
# تجميع المنشورات في مسلسلات
# ═══════════════════════════════════════════════════════════════
def _group_into_series(posts: List[Dict]) -> List[Dict]:
    if not posts:
        return []

    series_map: Dict[str, Dict] = {}

    for post in posts:
        link = post.get("link", "")
        title_obj = post.get("title", "")
        if isinstance(title_obj, dict):
            title = title_obj.get("rendered", "")
        else:
            title = title_obj or ""

        if not link or not title:
            continue

        name = _clean_series_name(title)
        ep_num = _extract_ep(title)

        # إذا لم نجد رقم حلقة، اعتبرها منشوراً منفصلاً
        if not ep_num:
            # جرّب من الرابط
            ep_num = _extract_ep(link)
        if not ep_num:
            continue

        if name not in series_map:
            series_map[name] = {
                "name": name,
                "url": link,
                "poster": "",
                "genre": "",
                "source": "u3seq",
                "episodes": [],
            }

        series_map[name]["episodes"].append({
            "num": ep_num,
            "url": link,
        })

    result = []
    for s in series_map.values():
        seen = set()
        unique_eps = []
        for ep in sorted(s["episodes"], key=lambda x: x["num"]):
            if ep["num"] not in seen:
                seen.add(ep["num"])
                unique_eps.append(ep)
        s["episodes"] = unique_eps
        if s["episodes"]:
            result.append(s)

    return result


# ═══════════════════════════════════════════════════════════════
# الواجهة الرئيسية
# ═══════════════════════════════════════════════════════════════
def fetch() -> List[Dict]:
    """يجلب المسلسلات من u.3seq.cam."""
    cat = config.SOURCE_CATEGORY
    print(f"📋 جلب قائمة المسلسلات (تصنيف {cat if cat > 0 else 'الكل'})...")

    # المحاولة 1: REST API
    result = _fetch_via_api()
    if result:
        print(f"✅ {len(result)} مسلسل (API)")
        return result

    # المحاولة 2: HTML
    result = _fetch_via_html()
    if result:
        print(f"✅ {len(result)} مسلسل (HTML)")
        return result

    print(f"   ⚠️ لا مسلسلات من u3seq")
    return []


if __name__ == "__main__":
    config.validate()
    series = fetch()
    print(f"\n📊 النتيجة: {len(series)} مسلسل")
    for s in series[:5]:
        print(f"   · {s['name']}: {len(s['episodes'])} حلقة")