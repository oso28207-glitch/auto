"""
sources/u3seq.py — جلب المسلسلات من u.3seq.cam
═══════════════════════════════════════════════════════════
يعتمد على WordPress REST API:
  GET /wp-json/wp/v2/posts?per_page=100&page=N

★ v2: إضافة
    - poster (من featured_media._embedded)
    - category (من wp:term._embedded)
    - last_updated (تاريخ آخر منشور في المسلسل)
    - fallback: HTML scraping لجلب og:image عند الحاجة

★ لا نرسل categories عندما تكون 0.
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
# cloudscraper
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
# استخراج نصي
# ═══════════════════════════════════════════════════════════════
def _extract_ep(text: str) -> int:
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


def _strip_html(text: str) -> str:
    """يزيل وسوم HTML من نص title.rendered."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    t = t.replace("&amp;", "&").replace("&#8217;", "'").replace("&quot;", '"')
    t = t.replace("&#8211;", "-").replace("&nbsp;", " ")
    return t.strip()


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
# استخراج poster و category من post واحد
# ═══════════════════════════════════════════════════════════════
def _extract_poster_from_post(post: Dict) -> str:
    """
    يستخرج رابط الصورة الرئيسية من منشور WordPress.
    يدعم:
      - _embedded['wp:featuredmedia'][0].source_url
      - _embedded['wp:featuredmedia'][0].media_details.sizes.full.source_url
    """
    try:
        emb = post.get("_embedded", {})
        media_list = emb.get("wp:featuredmedia", [])
        if not media_list:
            return ""
        media = media_list[0]
        if not isinstance(media, dict):
            return ""

        # أولاً: source_url
        url = media.get("source_url", "")
        if url:
            return url

        # ثانياً: media_details.sizes
        details = media.get("media_details", {})
        sizes = details.get("sizes", {})
        for size_key in ("full", "large", "medium_large", "medium"):
            if size_key in sizes:
                src = sizes[size_key].get("source_url", "")
                if src:
                    return src
    except Exception:
        pass
    return ""


def _extract_category_from_post(post: Dict) -> str:
    """
    يستخرج أول تصنيف من _embedded['wp:term'].
    البنية: [ [cat1, cat2], [tag1, tag2] ]
    """
    try:
        emb = post.get("_embedded", {})
        terms = emb.get("wp:term", [])
        for term_group in terms:
            if not isinstance(term_group, list):
                continue
            for term in term_group:
                if not isinstance(term, dict):
                    continue
                # فقط taxonomy = category
                tax = term.get("taxonomy", "")
                if tax == "category":
                    name = term.get("name", "")
                    if name:
                        return _strip_html(name)

        # fallback: أي اسم term
        for term_group in terms:
            if not isinstance(term_group, list):
                continue
            for term in term_group:
                if isinstance(term, dict):
                    name = term.get("name", "")
                    if name:
                        return _strip_html(name)
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# المحاولة 1: WordPress REST API
# ═══════════════════════════════════════════════════════════════
def _fetch_via_api() -> List[Dict]:
    print(f"   📡 محاولة WordPress REST API...")
    all_posts = []
    per_page = 100
    max_pages = config.SOURCE_MAX_PAGES

    for page in range(1, max_pages + 1):
        params = {
            "per_page": per_page,
            "page": page,
            "_fields": "id,link,title,date,slug,featured_media,_embedded,_links",
            "_embed": "wp:featuredmedia,wp:term",
        }
        if config.SOURCE_CATEGORY and config.SOURCE_CATEGORY > 0:
            params["categories"] = config.SOURCE_CATEGORY

        try:
            posts = _api_get("/wp-json/wp/v2/posts", params=params)
        except SourceError as e:
            print(f"   ⚠️ فشل: {str(e)[:120]}")
            return []

        if posts is None:
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
# المحاولة 2: HTML scraping
# ═══════════════════════════════════════════════════════════════
def _fetch_via_html() -> List[Dict]:
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
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not text or len(text) < 3:
                    continue
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
            "date": "",
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
            title = _strip_html(title_obj.get("rendered", ""))
        else:
            title = _strip_html(str(title_obj or ""))

        if not link or not title:
            continue

        name = _clean_series_name(title)
        ep_num = _extract_ep(title)

        if not ep_num:
            ep_num = _extract_ep(link)
        if not ep_num:
            continue

        date = post.get("date", "") or ""
        poster = _extract_poster_from_post(post)
        category = _extract_category_from_post(post)

        if name not in series_map:
            series_map[name] = {
                "name": name,
                "url": link,
                "poster": poster,
                "category": category,   # ★ جديد
                "genre": "",
                "source": "u3seq",
                "last_updated": date,    # ★ جديد
                "episodes": [],
            }
        else:
            # إذا كان المنشور الحالي أحدث، نحدّث last_updated
            if date and date > (series_map[name]["last_updated"] or ""):
                series_map[name]["last_updated"] = date
            # إذا كان فيه poster ولم يكن موجوداً
            if poster and not series_map[name]["poster"]:
                series_map[name]["poster"] = poster
            # إذا كان فيه category ولم يكن موجوداً
            if category and not series_map[name]["category"]:
                series_map[name]["category"] = category

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
    cat = config.SOURCE_CATEGORY
    print(f"📋 جلب قائمة المسلسلات (تصنيف {cat if cat > 0 else 'الكل'})...")

    result = _fetch_via_api()
    if result:
        # إحصائيات سريعة
        with_poster = sum(1 for s in result if s.get("poster"))
        with_cat = sum(1 for s in result if s.get("category"))
        print(f"✅ {len(result)} مسلسل (API) — "
              f"{with_poster} مع صور، {with_cat} مع تصنيفات")
        return result

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
    for s in series[:8]:
        cat = s.get("category", "?")[:25]
        poster_flag = "🖼️" if s.get("poster") else "❌"
        print(f"   · [{cat}] {s['name']}: {len(s['episodes'])} "
              f"| {poster_flag} | {s.get('last_updated', '')[:10]}")