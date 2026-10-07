"""
u3seq source — Scraping مباشر لصفحات HTML
يدعم: قائمة المسلسلات + قائمة الحلقات + البوستر + التصنيف
"""
import os
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/")
MAX_PAGES = int(os.environ.get("SOURCE_MAX_PAGES", "20"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════
def _get(url, timeout=30):
    """طلب HTTP مع محاكاة بصمة Chrome."""
    try:
        return cffi.get(
            url,
            impersonate="chrome120",
            timeout=timeout,
            verify=False,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            },
        )
    except Exception:
        return None


def _clean_name(raw):
    """تنظيف اسم المسلسل من لاحقات الحلقة."""
    if not raw:
        return ""
    name = html.unescape(raw).strip()
    name = re.sub(r'\s*[-–—]\s*الحلقة\s*\d+.*$', '', name).strip()
    name = re.sub(r'\s*الحلقة\s*\d+\s*(مدبلجة|مترجمة)?\s*$', '', name).strip()
    return name


# ═══════════════════════════════════════════════════════════════
# جلب قائمة المسلسلات من /video/series/
# ═══════════════════════════════════════════════════════════════
def _fetch_series_list():
    """جلب روابط جميع المسلسلات من أرشيف المسلسلات."""
    all_links = []
    seen = set()
    page = 1

    while page <= MAX_PAGES:
        if page == 1:
            url = f"{BASE}/video/series/"
        else:
            url = f"{BASE}/video/series/page/{page}/"

        r = _get(url)
        if not r or r.status_code != 200:
            break

        soup = BeautifulSoup(r.text, "html.parser")
        found_on_page = 0

        # ابحث عن كل روابط المسلسلات
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # فلترة: فقط روابط /video/series/ الفريدة
            if "/video/series/" not in href:
                continue
            full = urljoin(BASE, href)
            # تجاوز الصفحة الرئيسية للأرشيف
            if full.rstrip("/") == f"{BASE}/video/series":
                continue
            if full in seen:
                continue

            seen.add(full)
            all_links.append({
                "url": full,
                "name": a.get_text(strip=True),
            })
            found_on_page += 1

        if found_on_page == 0:
            break  # لا مزيد من الصفحات

        print(f"   صفحة {page}: {found_on_page} رابط")
        page += 1

    print(f"   ✅ وُجد {len(all_links)} رابط مسلسل")
    return all_links


# ═══════════════════════════════════════════════════════════════
# تحليل صفحة مسلسل واحدة
# ═══════════════════════════════════════════════════════════════
def _parse_series_page(url):
    """
    يستخرج: العنوان، البوستر، التصنيف، جميع الحلقات.
    يدعم المواسم المتعددة (data-season).
    """
    r = _get(url)
    if not r or r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # ─── 1) العنوان ───
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = _clean_name(h1.get_text(strip=True))
    if not name:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            name = _clean_name(og_title.get("content", ""))
    if not name:
        slug = url.rstrip("/").split("/")[-1]
        name = _clean_name(slug.replace("-", " "))

    # ─── 2) البوستر ───
    poster = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        poster = og_img["content"]
    if not poster:
        img = soup.select_one(".poster img, .singleSeries .poster img")
        if img:
            poster = img.get("src") or img.get("data-src") or ""
    if poster and not poster.startswith("http"):
        poster = urljoin(BASE, poster)

    # ─── 3) التصنيف ───
    genre = ""
    for li in soup.select("ul.postlist li"):
        label = li.find("label")
        if label and "النوع" in label.get_text():
            a = li.find("a")
            if a:
                genre = a.get_text(strip=True)
                break
    if not genre:
        # جرّب من categories
        cat_link = soup.select_one("a[href*='/category/']")
        if cat_link:
            genre = cat_link.get_text(strip=True)

    # ─── 4) الحلقات من ul.eplist ───
    episodes = []
    seen_nums = set()
    eplist = soup.find("ul", class_="eplist")
    if eplist:
        for a in eplist.find_all("a", class_="epNum", href=True):
            href = a["href"]
            # رقم الحلقة من <span>
            num_span = a.find("span")
            if not num_span:
                continue
            try:
                num = int(num_span.get_text(strip=True))
            except ValueError:
                continue
            if num < 1 or num > 10000 or num in seen_nums:
                continue
            seen_nums.add(num)

            page_url = urljoin(url, href)
            if not page_url.endswith("/"):
                page_url += "/"
            # ★ الرابط الفعلي للمشاهدة
            watch_url = page_url + "?do=watch"

            episodes.append({
                "num": num,
                "url": watch_url,       # الرابط الكامل للمشاهدة
                "page_url": page_url,   # رابط الصفحة الأساسية
            })

    if not episodes:
        return None

    episodes.sort(key=lambda x: x["num"])

    return {
        "name": name,
        "poster": poster,
        "genre": genre,
        "episodes": episodes,
        "source_url": url,
    }


# ═══════════════════════════════════════════════════════════════
# الواجهة العامة
# ═══════════════════════════════════════════════════════════════
def fetch():
    """ترجع قائمة مسلسلات مع جميع حلقاتها."""
    print(f"   📄 جلب قائمة المسلسلات...")
    links = _fetch_series_list()

    if not links:
        print(f"   ⚠️ لا مسلسلات")
        return []

    print(f"   🔄 جلب تفاصيل {len(links)} مسلسل (متوازي)...")

    series_list = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_parse_series_page, item["url"]): item
                   for item in links}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                s = fut.result()
                if s and s["episodes"]:
                    series_list.append(s)
                    print(f"      [{done}/{len(links)}] "
                          f"✅ {s['name'][:50]}: {len(s['episodes'])} حلقة")
                elif s:
                    print(f"      [{done}/{len(links)}] "
                          f"⚠️ {s['name'][:50]}: 0 حلقة")
            except Exception as e:
                print(f"      [{done}/{len(links)}] ❌ {str(e)[:80]}")

    print(f"   ✅ {len(series_list)} مسلسل صالح")
    return series_list