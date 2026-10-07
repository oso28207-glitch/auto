"""
sources/u3seq.py — جلب المسلسلات من u.3seq.cam
═══════════════════════════════════════════════════════════
يعتمد على WordPress REST API:
  GET /wp-json/wp/v2/posts?categories={SOURCE_CATEGORY}&per_page=100&page=N

يستخدم cloudscraper لتجاوز Cloudflare.
"""

import re
import time
from typing import List, Dict, Any, Optional

import cloudscraper

from config import config
from errors import SourceError

# ═══════════════════════════════════════════════════════════════
# cloudscraper (مُنشأ مرة واحدة)
# ═══════════════════════════════════════════════════════════════
_scraper = None


def _get_scraper():
    """يُنشئ scraper يتجاوز Cloudflare (chrome windows)."""
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
# استخراج البيانات من العنوان
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
        r"(\d+)",
    ]:
        m = re.search(p, text)
        if m:
            return int(m.group(1))
    return 0


def _clean_series_name(title: str) -> str:
    """ينظّف اسم المسلسل من العنوان."""
    t = re.sub(r"\s*الحلقة\s*\d+.*$", "", title or "").strip()
    t = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", t).strip()
    t = re.sub(r"\s*مدبلجة?\s*$", "", t).strip()
    t = re.sub(r"\s*مترجمة?\s*$", "", t).strip()
    t = re.sub(r"\s*كاملة\s*$", "", t).strip()
    return t or title


# ═══════════════════════════════════════════════════════════════
# جلب البيانات من API
# ═══════════════════════════════════════════════════════════════
def _api_get(path: str, params: Optional[Dict] = None, retries: int = 3) -> Optional[Any]:
    """
    يطلب JSON من WordPress REST API.
    يتعامل مع 403 (Cloudflare) بإعادة المحاولة.
    """
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ")

    url = config.SOURCE_BASE_URL.rstrip("/") + path
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
                try:
                    return r.json()
                except Exception as e:
                    raise SourceError(f"فشل تحليل JSON: {e}")

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
            last_err = str(e)[:150]
            time.sleep(3)

    raise SourceError(f"فشل جلب {url} — {last_err}")


# ═══════════════════════════════════════════════════════════════
# الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
def fetch() -> List[Dict]:
    """
    يجلب المسلسلات من u.3seq.cam.

    يعيد قائمة بالشكل:
    [
        {
            "name": "اسم المسلسل",
            "url": "https://u.3seq.cam/...",
            "poster": "",
            "genre": "",
            "episodes": [
                {"num": 1, "url": "https://u.3seq.cam/..."},
                ...
            ],
        },
        ...
    ]
    """
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
                # لا مزيد من الصفحات
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
        print(f"   ⚠️ لا منشورات في التصنيف {config.SOURCE_CATEGORY}")
        return []

    # ═══ تجميع المنشورات في مسلسلات ═══
    series_map: Dict[str, Dict] = {}

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
                "poster": "",
                "genre": "",
                "source": "u3seq",
                "episodes": [],
            }

        series_map[name]["episodes"].append({
            "num": ep_num,
            "url": link,
        })

    # ═══ ترتيب الحلقات + إزالة التكرار ═══
    result = []
    for s in series_map.values():
        # ترتيب حسب رقم الحلقة
        seen = set()
        unique_eps = []
        for ep in sorted(s["episodes"], key=lambda x: x["num"]):
            if ep["num"] not in seen:
                seen.add(ep["num"])
                unique_eps.append(ep)
        s["episodes"] = unique_eps

        if s["episodes"]:
            result.append(s)

    print(f"✅ {len(result)} مسلسل، {len(all_posts)} منشور")
    return result


# ═══════════════════════════════════════════════════════════════
# اختبار سريع
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from config import config
    config.validate()
    series = fetch()
    print(f"\n📊 النتيجة: {len(series)} مسلسل")
    for s in series[:5]:
        print(f"   · {s['name']}: {len(s['episodes'])} حلقة")