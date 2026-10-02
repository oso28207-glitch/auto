"""
checker.py — فحص المسلسلات والحلقات من موقع u.3seq.cam
يستخدم cloudscraper لتجاوز Cloudflare + WordPress REST API.
"""

import re
import time

import cloudscraper

from config import config
from database import db
from errors import SourceError


# ═══════════════════════════════════════════════════════════════
# جلسة cloudscraper مشتركة (تُنشأ مرة واحدة)
# ═══════════════════════════════════════════════════════════════
_scraper = None


def _get_scraper():
    """ينشئ scraper واحد ويُعيده في كل الطلبات."""
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
# دوال مساعدة
# ═══════════════════════════════════════════════════════════════
def _extract_episode_number(text: str) -> int:
    """يستخرج رقم الحلقة من نص مثل 'الحلقة 5' أو 'Episode 5'."""
    if not text:
        return 0
    patterns = [
        r"الحلقة\s*(\d+)",
        r"[Ee]pisode\s*(\d+)",
        r"[Ee]p\.?\s*(\d+)",
        r"(\d+)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return int(m.group(1))
    return 0


def _api_get(path: str, params: dict = None, retries: int = 3):
    """
    يستدعي WordPress REST API عبر cloudscraper.
    يعيد JSON. يرفع SourceError عند الفشل النهائي.
    """
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ. اضبطه في .env أو GitHub Secrets.")

    base = config.SOURCE_BASE_URL.rstrip("/")
    url = f"{base}{path}"

    if not url.startswith(("http://", "https://")):
        raise SourceError(f"URL غير صالح: '{url}'")

    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ar,en;q=0.9",
        "Referer": base + "/",
    }

    scraper = _get_scraper()
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            print(f"   ↳ محاولة {attempt}/{retries}: {url}")
            r = scraper.get(url, headers=headers, params=params, timeout=60)
            print(f"      → HTTP {r.status_code}")

            if r.status_code == 200:
                return r.json()

            if r.status_code == 403:
                last_error = f"HTTP 403 (Cloudflare) — محاولة {attempt}"
                print(f"      ⚠️  {last_error}")
                # انتظر أطول قبل إعادة المحاولة
                time.sleep(5 * attempt)
                continue

            if r.status_code == 404:
                raise SourceError(
                    f"HTTP 404 — المسار غير موجود: {url}\n"
                    f"   تحقق من SOURCE_SERIES_PATH"
                )

            last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            print(f"      ⚠️  {last_error}")
            time.sleep(3 * attempt)

        except SourceError:
            raise
        except Exception as e:
            last_error = str(e)
            print(f"      ⚠️  خطأ: {last_error}")
            time.sleep(3 * attempt)

    raise SourceError(
        f"فشلت كل المحاولات ({retries}) لـ {url}\n"
        f"   آخر خطأ: {last_error}\n"
        f"   الحل: تحقق من أن cloudscraper مثبت وأن الموقع لا يحجب IP."
    )


# ═══════════════════════════════════════════════════════════════
# الدوال الرئيسية
# ═══════════════════════════════════════════════════════════════
def fetch_series_list() -> list[dict]:
    """
    يجلب قائمة المسلسلات من REST API.
    يعيد: [{name, url, slug, posts: [...]}]
    """
    print(f"📋 جلب قائمة المسلسلات...")

    path = config.SOURCE_SERIES_PATH
    if not path:
        path = "/wp-json/wp/v2/posts?categories=712&per_page=100"

    # استخراج category_id والـ per_page من المسار
    cat_match = re.search(r"categories=(\d+)", path)
    per_page_match = re.search(r"per_page=(\d+)", path)

    category_id = int(cat_match.group(1)) if cat_match else 712
    per_page = int(per_page_match.group(1)) if per_page_match else 100

    # جلب كل الصفحات
    all_posts = []
    page = 1
    max_pages = 20  # حد أقصى 2000 منشور لتجنب الجلب اللانهائي

    while page <= max_pages:
        api_path = "/wp-json/wp/v2/posts"
        params = {
            "categories": category_id,
            "per_page": per_page,
            "page": page,
            "_fields": "id,link,title,date,slug",
        }

        try:
            posts = _api_get(api_path, params=params)
        except SourceError as e:
            if "404" in str(e) and page > 1:
                # وصلنا لنهاية الصفحات
                break
            raise

        if not isinstance(posts, list) or not posts:
            break

        all_posts.extend(posts)
        print(f"   صفحة {page}: {len(posts)} منشور (المجموع: {len(all_posts)})")

        if len(posts) < per_page:
            break

        page += 1
        time.sleep(1)  # تأخير بسيط بين الصفحات

    if not all_posts:
        raise SourceError(
            f"لم يتم العثور على أي منشورات في التصنيف {category_id}.\n"
            f"   تحقق من صحة categories في SOURCE_SERIES_PATH."
        )

    # تجميع المنشورات حسب اسم المسلسل
    series_map = {}
    for post in all_posts:
        link = post.get("link", "")
        title = (post.get("title") or {}).get("rendered", "")
        if not link or not title:
            continue

        # تنظيف العنوان لاستخراج اسم المسلسل
        clean = re.sub(r"\s*الحلقة\s*\d+.*$", "", title).strip()
        clean = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", clean).strip()
        clean = re.sub(r"\s*مدبلجة?\s*$", "", clean).strip()
        clean = re.sub(r"\s*مترجمة?\s*$", "", clean).strip()
        clean = clean or title

        if clean not in series_map:
            series_map[clean] = {
                "name": clean,
                "url": link,
                "slug": post.get("slug", ""),
                "posts": [],
            }

        series_map[clean]["posts"].append({
            "id": post.get("id"),
            "title": title,
            "link": link,
            "date": post.get("date", ""),
            "episode": _extract_episode_number(title),
        })

    # ترتيب حلقات كل مسلسل تصاعدياً
    for s in series_map.values():
        s["posts"].sort(key=lambda x: x["episode"])

    series_list = list(series_map.values())
    print(f"\n✅ إجمالي: {len(series_list)} مسلسل، {len(all_posts)} منشور")
    return series_list


def fetch_episodes(series_url: str) -> list[dict]:
    """توافقية — تعيد حلقات مسلسل عبر URL."""
    return find_new_episodes_by_url(series_url)


def find_new_episodes(series_name: str) -> list[dict]:
    """يعيد الحلقات الجديدة (غير المرفوعة) لمسلسل معين."""
    series = db.get_series(series_name)
    if not series.get("url"):
        return []

    # إذا كانت الحلقات محفوظة في قاعدة البيانات من دورة سابقة
    cached = series.get("episodes", {})
    if cached:
        return [
            {"number": int(k), "url": v["url"], "title": f"الحلقة {k}"}
            for k, v in cached.items()
            if v.get("status") != "uploaded"
        ]

    return []


def find_new_episodes_by_url(series_url: str) -> list[dict]:
    """يعيد كل الحلقات من رابط مسلسل (للاستخدام المباشر)."""
    # استخراج slug من الرابط
    slug_match = re.search(r"/video/([^/]+?)/?$", series_url)
    if not slug_match:
        return []

    slug = slug_match.group(1)
    base_slug = re.sub(r"-episode-\d+$", "", slug)

    # البحث في REST API
    api_path = "/wp-json/wp/v2/posts"
    params = {
        "search": base_slug.replace("-", " "),
        "per_page": 100,
        "_fields": "id,link,title,date",
    }

    try:
        posts = _api_get(api_path, params=params)
    except SourceError:
        return []

    if not isinstance(posts, list):
        return []

    episodes = []
    for post in posts:
        title = (post.get("title") or {}).get("rendered", "")
        ep_num = _extract_episode_number(title)
        if ep_num:
            episodes.append({
                "number": ep_num,
                "url": post.get("link", ""),
                "title": title,
            })

    episodes.sort(key=lambda x: x["number"])
    return episodes
