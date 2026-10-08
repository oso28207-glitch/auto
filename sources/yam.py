"""yam.ahwaktv.net — يستخرج see.php?vid=XXX

★ v4: 
    - استخراج الصور بشكل صحيح (og:image مع urljoin)
    - استخراج التصنيف تلقائيًا إذا لم يُمرر
    - حفظ last_updated لتحديث ترتيب الموقع
"""
import os
import re
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SKIP = {
    "الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
    "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن",
    "سياسة الخصوصية", "شروط الاستخدام", "DMCA", "حقوق النشر",
    "أفلام", "مسلسلات", "مسرحيات", "برامج", "عرض المزيد", "المزيد",
}

BAD_URL_PARTS = [
    "see.php", "watch.php", "moslslat.php", "topvideos.php",
    "?page=", "/category/", "/tag/", "/actor/",
    "index.php", "?do=", "?cat=", "?p=",
    "javascript:", "#", "mailto:", "tel:",
]

SEE_RE = re.compile(r'see\.php\?vid=([A-Za-z0-9]+)', re.I)
WATCH_RE = re.compile(r'watch\.php\?vid=([A-Za-z0-9]+)', re.I)
EP_RE = re.compile(r'(?:الحلقة|حلقة|الحلقه)[\s\-_:]*?(\d{1,4})', re.I)


def _get(url, timeout=25):
    try:
        return cffi.get(
            url, impersonate="chrome120", timeout=timeout, verify=False,
            headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"},
        )
    except Exception:
        return None


def _clean(n):
    if not n:
        return ""
    n = n.strip()
    n = re.sub(r'\s*[-–—]\s*اهواك.*$', '', n).strip()
    n = re.sub(r'\s*\|.*$', '', n).strip()
    return n


def _clean_series_hint(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r'\s*(?:الحلقة|حلقة|الحلقه|Episode|Ep\.?)\s*[\-_:]?\s*\d+.*$',
               '', text, flags=re.I).strip()
    t = re.sub(r'\s*\d+\s*$', '', t).strip()
    t = re.sub(r'^[📺🎬🎥🎞️🔹\-•\s]+', '', t).strip()
    return t


def _extract_ep_num(text: str) -> int:
    if not text:
        return 0
    m = EP_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


def _fetch_list():
    r = _get(f"{BASE}{SERIES_PATH}", 25)
    if not r or r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()
    dom = urlparse(BASE).netloc

    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        t = _clean(a.get_text(strip=True))

        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if t in SKIP or len(t) < 4:
            continue

        f = urljoin(BASE, h)
        if dom not in f:
            continue
        if any(x in f for x in BAD_URL_PARTS):
            continue
        if urlparse(f).netloc != dom:
            continue
        if f in seen:
            continue
        seen.add(f)
        links.append({"url": f, "name": t})

    return links


def _parse(url, category="", depth=0, visited=None):
    if visited is None:
        visited = set()
    if url in visited or depth > 2:
        return None
    visited.add(url)

    r = _get(url, 20)
    if not r or r.status_code != 200:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    page_name = ""
    h1 = soup.find("h1")
    if h1:
        page_name = _clean(h1.get_text(strip=True))

    # استخراج الصورة الرئيسية
    poster = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        poster = urljoin(BASE, og["content"])

    # استخراج التصنيف تلقائيًا إذا لم يُمرر
    if not category:
        # البحث عن أول رابط في breadcrumbs يشير إلى تصنيف
        breadcrumb = soup.find("a", href=lambda x: x and "category" in x)
        if breadcrumb:
            category = _clean(breadcrumb.get_text(strip=True))
        else:
            category = page_name

    # جمع الحلقات مع hints
    candidates = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        sm = SEE_RE.search(h)
        wm = WATCH_RE.search(h)
        if not sm and not wm:
            continue
        vid = (sm or wm).group(1)
        text = a.get_text(" ", strip=True)
        if not text:
            continue
        ep_num = _extract_ep_num(text)
        hint = _clean_series_hint(text)
        candidates.append({"vid": vid, "ep_num": ep_num, "hint": hint})

    if not candidates:
        return None

    unique_hints = set(c["hint"] for c in candidates if c["hint"])
    unique_ep_nums = set(c["ep_num"] for c in candidates if c["ep_num"])

    is_series = True
    if len(unique_hints) > 1:
        is_series = False
    if is_series and len(candidates) > 30 and len(unique_ep_nums) <= 2:
        is_series = False

    if is_series:
        eps = {}
        for c in candidates:
            num = c["ep_num"] or (len(eps) + 1)
            while num in eps:
                num += 1
            eps[num] = f"{BASE}/see.php?vid={c['vid']}"

        # تحديد تاريخ آخر تحديث (تقديري بناءً على أعلى رقم حلقة)
        last_updated = ""
        if eps:
            last_ep = max(eps.keys())
            last_updated = f"2026-01-01T{last_ep:02d}:00:00"  # مثال، يمكن تحسينه

        return {
            "name": page_name or url.rstrip("/").split("/")[-1],
            "poster": poster,
            "genre": "",
            "category": category,
            "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)],
            "last_updated": last_updated,
            "source_url": url,
        }

    # حالة التصنيف
    series_urls = {}
    dom = urlparse(BASE).netloc
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        f = urljoin(BASE, h)
        if urlparse(f).netloc != dom:
            continue
        if any(x in f for x in BAD_URL_PARTS):
            continue
        if f == url or f in visited:
            continue
        text = _clean(a.get_text(" ", strip=True))
        if not text or len(text) < 4 or text in SKIP:
            continue
        if f in series_urls:
            continue
        series_urls[f] = text

    if not series_urls:
        eps = {}
        for c in candidates:
            num = c["ep_num"] or (len(eps) + 1)
            while num in eps:
                num += 1
            eps[num] = f"{BASE}/see.php?vid={c['vid']}"
        last_updated = ""
        if eps:
            last_updated = f"2026-01-01T{max(eps.keys()):02d}:00:00"
        return {
            "name": page_name or url.rstrip("/").split("/")[-1],
            "poster": poster,
            "genre": "",
            "category": category,
            "episodes": [{"num": n, "url": eps[n]} for n in sorted(eps)],
            "last_updated": last_updated,
            "source_url": url,
        }

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_parse, u, category, depth + 1, visited): u
                for u in series_urls.keys()}
        for f in as_completed(futs):
            try:
                s = f.result()
                if isinstance(s, dict):
                    results.append(s)
                elif isinstance(s, list):
                    results.extend(s)
            except Exception:
                pass
    return results


def fetch():
    links = _fetch_list()
    if not links:
        return []

    all_series = []
    seen_names = set()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_parse, i["url"], i["name"]): i for i in links}
        for f in as_completed(futs):
            try:
                result = f.result()
                items = [result] if isinstance(result, dict) else result
                for s in items:
                    if not s or not s.get("episodes"):
                        continue
                    key = (s.get("name") or "").strip().lower()
                    if not key or key in seen_names:
                        continue
                    seen_names.add(key)
                    all_series.append(s)
            except Exception:
                pass
    return all_series