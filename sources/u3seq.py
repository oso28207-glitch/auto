"""
sources/u3seq.py — جلب المسلسلات من u.3seq.cam / u.3seq.com
════════════════════════════════════════════════════════════
يعتمد على WordPress REST API:
  GET /wp-json/wp/v2/posts?per_page=100&page=N

★ v3 (إصلاح جذري لتجاوز Cloudflare):
    - كان cloudscraper يفشل دائماً بـ 403.
    - الحل: استخدام curl_cffi مع impersonate="safari17_0"
      (يعمل بينما chrome120/124/131 محجوبة على .cam).
    - تدوير الـ impersonations + تدوير النطاق (.cam/.com) تلقائياً.
    - استخراج البوستر من صفحة المنشور (img.img-responsive) لأن
      featured_media = 0 في هذا الموقع (لا يوجد صورة بارزة).
    - استخراج التصنيف من wp:term (taxonomy=category).
"""

import re
import time
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

from config import config
from errors import SourceError

# ═══════════════════════════════════════════════════════════════════
# إعدادات المتصفح (تجاوز Cloudflare)
# ═══════════════════════════════════════════════════════════════════
# safari17_0 يعمل على .cam — والباقي كاحتياط
IMPERSONATIONS = ["safari17_0", "chrome120", "chrome110", "chrome104", "chrome99"]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# نطاقات بديلة (fallback)
_ALT_DOMAINS = ["https://u.3seq.com", "https://u.3seq.cam"]


def _bases() -> List[str]:
    """قائمة النطاقات التي سنجربها بالترتيب."""
    out = []
    if config.SOURCE_BASE_URL:
        out.append(config.SOURCE_BASE_URL.rstrip("/"))
    for alt in _ALT_DOMAINS:
        if alt not in out:
            out.append(alt)
    return out


def _headers(referer: str = "") -> Dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Referer": referer or (config.SOURCE_BASE_URL or "https://u.3seq.com") + "/",
        "User-Agent": UA,
        "Upgrade-Insecure-Requests": "1",
    }


# ═══════════════════════════════════════════════════════════════════
# استخراج نصي
# ═══════════════════════════════════════════════════════════════════
def _extract_ep(text: str) -> int:
    if not text:
        return 0
    for p in [
        r"الحلقة\s*(\d+)",
        r"[Ee]pisode\s*(\d+)",
        r"حلقة\s*(\d+)",
        r"Ep\.?\s*(\d+)",
        r"episode-?(\d+)",
        r"-ep-?(\d+)",
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
    t = re.sub(r"\s*والاخيرة\s*$", "", t).strip()
    t = re.sub(r"\s*والأخيرة\s*$", "", t).strip()
    return t or title


def _strip_html(text: str) -> str:
    """يزيل وسوم HTML من نص title.rendered."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    t = t.replace("&amp;", "&").replace("&#8217;", "'").replace("&quot;", '"')
    t = t.replace("&#8211;", "-").replace("&nbsp;", " ").replace("&#8216;", "'")
    return t.strip()


# ═══════════════════════════════════════════════════════════════════
# API helper — curl_cffi مع تدوير impersonate/domain
# ═══════════════════════════════════════════════════════════════════
def _api_get(path: str, params: Optional[Dict] = None, retries: int = 3) -> Optional[Any]:
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ")

    bases = _bases()
    last_err = None

    for attempt in range(1, retries + 1):
        for base in bases:
            url = base + path
            for imp in IMPERSONATIONS:
                try:
                    r = cffi.get(
                        url,
                        headers=_headers(referer=base + "/"),
                        params=params,
                        impersonate=imp,
                        timeout=45,
                    )
                except Exception as e:
                    last_err = f"{str(e)[:100]} ({base}/{imp})"
                    continue

                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception as e:
                        raise SourceError(f"فشل تحليل JSON: {e}")

                if r.status_code == 400:
                    print(f"   ⚠️ 400 — لا نستمر مع هذه المعطيات")
                    return None

                if r.status_code == 404:
                    return None

                if r.status_code == 403:
                    last_err = f"403 ({base.split('//')[-1]}/{imp})"
                    continue

                last_err = f"HTTP {r.status_code} ({base.split('//')[-1]}/{imp})"

        print(f"   ⚠️ {last_err} (محاولة {attempt})")
        time.sleep(3 * attempt)

    raise SourceError(f"فشل جلب {path} — {last_err}")


def _fetch_page_html(url: str, retries: int = 2) -> str:
    """يجلب HTML لصفحة منشور (مع تدوير impersonate)."""
    for attempt in range(retries):
        for imp in IMPERSONATIONS:
            try:
                r = cffi.get(url, headers=_headers(), impersonate=imp, timeout=45)
                if r.status_code == 200:
                    return r.text
            except Exception:
                continue
    return ""


# ═══════════════════════════════════════════════════════════════════
# استخراج poster و category من post واحد
# ═══════════════════════════════════════════════════════════════════
def _extract_poster_from_post(post: Dict) -> str:
    """
    يستخرج رابط الصورة الرئيسية من منشور WordPress (featured_media).
    ملاحظة: هذا الموقع يجعل featured_media=0 عادةً، لذا نعتمد أيضاً
    على _poster_from_page() لاحقاً.
    """
    try:
        emb = post.get("_embedded", {})
        media_list = emb.get("wp:featuredmedia", [])
        if not media_list:
            return ""
        media = media_list[0]
        if not isinstance(media, dict):
            return ""

        url = media.get("source_url", "")
        if url:
            return url

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
                if term.get("taxonomy", "") == "category":
                    name = term.get("name", "")
                    if name:
                        return _strip_html(name)

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


# ═══════════════════════════════════════════════════════════════════
# استخراج البوستر من صفحة المنشور (الحل الأساسي)
# ═══════════════════════════════════════════════════════════════════
def _poster_from_page(link: str) -> str:
    """
    يجلب صفحة المنشور ويستخرج صورة البوستر.
    الأولوية:
      1) <img class="img-responsive">  (بوستر المسلسل الحقيقي)
      2) og:image
      3) أكبر صورة من /wp-content/uploads/ (بعد استثناء الشعار)
    """
    if not link:
        return ""
    html = _fetch_page_html(link)
    if not html:
        return ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    # 1) img.img-responsive
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src:
            continue
        low = src.lower()
        if "logo" in low or "placeholder" in low or "avatar" in low:
            continue
        cls = img.get("class", [])
        cls = " ".join(cls) if isinstance(cls, list) else str(cls)
        if "img-responsive" in cls:
            return urljoin(link, src)

    # 2) og:image
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        return og["content"]

    # 3) أكبر صورة من uploads (استثناء الشعار)
    best = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        low = src.lower()
        if "logo" in low or "/wp-content/uploads/" not in low:
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", low):
            continue
        best = urljoin(link, src)
        break

    return best


# ═══════════════════════════════════════════════════════════════════
# المحاولة 1: WordPress REST API
# ═══════════════════════════════════════════════════════════════════
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

        time.sleep(0.5)

    return _group_into_series(all_posts)


# ═══════════════════════════════════════════════════════════════════
# المحاولة 2: HTML scraping
# ═══════════════════════════════════════════════════════════════════
def _fetch_via_html() -> List[Dict]:
    print(f"   📡 محاولة زحف HTML...")
    base = config.SOURCE_BASE_URL.rstrip("/")
    urls_to_try = [
        base + "/",
        base + "/series/",
        base + "/moslslat/",
        base + "/tv/",
    ]

    all_links = []

    for url in urls_to_try:
        html = _fetch_page_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
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


# ═══════════════════════════════════════════════════════════════════
# تجميع المنشورات في مسلسلات
# ═══════════════════════════════════════════════════════════════════
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
                "category": category,
                "genre": "",
                "source": "u3seq",
                "last_updated": date,
                "episodes": [],
            }
        else:
            if date and date > (series_map[name]["last_updated"] or ""):
                series_map[name]["last_updated"] = date
            if poster and not series_map[name]["poster"]:
                series_map[name]["poster"] = poster
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

    # ── جلب البوسترات الناقصة من صفحات المنشورات (بالتوازي) ──
    missing = [s for s in result if not s.get("poster")]
    if missing:
        print(f"   🖼️  جلب بوسترات {len(missing)} مسلسل من صفحات المنشورات...")
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_poster_from_page, s["url"]): s for s in missing}
            done = 0
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    p = fut.result()
                except Exception:
                    p = ""
                if p:
                    s["poster"] = p
                done += 1
                if done % 25 == 0:
                    print(f"      … {done}/{len(missing)}")

    return result


# ═══════════════════════════════════════════════════════════════════
# الواجهة الرئيسية
# ═══════════════════════════════════════════════════════════════════
def fetch() -> List[Dict]:
    cat = config.SOURCE_CATEGORY
    print(f"📋 جلب قائمة المسلسلات (تصنيف {cat if cat > 0 else 'الكل'})...")

    result = _fetch_via_api()
    if result:
        with_poster = sum(1 for s in result if s.get("poster"))
        with_cat = sum(1 for s in result if s.get("category"))
        print(f"✅ {len(result)} مسلسل (API) — "
              f"{with_poster} مع صور، {with_cat} مع تصنيفات")
        return result

    result = _fetch_via_html()
    if result:
        with_poster = sum(1 for s in result if s.get("poster"))
        print(f"✅ {len(result)} مسلسل (HTML) — {with_poster} مع صور")
        return result

    print(f"   ⚠️ لا مسلسلات من u3seq")
    return []


if __name__ == "__main__":
    config.validate()
    series = fetch()
    print(f"\n📊 النتيجة: {len(series)} مسلسل")
    for s in series[:8]:
        cat = (s.get("category") or "?")[:25]
        poster_flag = "🖼️" if s.get("poster") else "❌"
        print(f"   · [{cat}] {s['name']}: {len(s['episodes'])} "
              f"| {poster_flag} | {s.get('last_updated', '')[:10]}")
