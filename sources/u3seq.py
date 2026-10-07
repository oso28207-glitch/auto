"""
u3seq source — قصة عشق (u.3seq.cam / u.3seq.com)
★ تجرب مسارات متعددة للعثور على أرشيف المسلسلات
★ تدعم النطاقات البديلة
"""
import os
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

# النطاقات الأساسية
PRIMARY_DOMAIN = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/")
DOMAIN_CANDIDATES = [
    PRIMARY_DOMAIN,
    "https://u.3seq.com",
    "https://u.3seq.cam",
    "https://3seq.cam",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# أنماط روابط الحلقات
EPISODE_PATTERNS = [
    re.compile(r'href=["\']([^"\']*?/video/[^"\']*?modablaj-[^"\']*?episode[-_](\d+)[^"\']*)["\']', re.I),
    re.compile(r'href=["\']([^"\']*?modablaj-[^"\']*?episode[-_](\d+)[^"\']*)["\']', re.I),
    re.compile(r'href=["\']([^"\']*?episode[-_](\d+)[^"\']*)["\']', re.I),
]


def _get(url, timeout=25):
    try:
        return cffi.get(
            url, impersonate="chrome120", timeout=timeout, verify=False,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
            },
        )
    except Exception:
        return None


def _clean_name(raw):
    if not raw:
        return ""
    name = html.unescape(raw).strip()
    name = re.sub(r'\s*[-–—]\s*الحلقة\s*\d+.*$', '', name).strip()
    name = re.sub(r'\s*الحلقة\s*\d+\s*(مدبلجة|مترجمة)?\s*$', '', name).strip()
    name = re.sub(r'\s*\|\s*قصة عشق.*$', '', name).strip()
    return name


def _try_base():
    """يجرب النطاقات ويرجع أول واحد يعمل."""
    for base in DOMAIN_CANDIDATES:
        r = _get(base + "/", timeout=10)
        if r and r.status_code == 200 and len(r.text) > 1000:
            print(f"   ✅ النطاق: {base}")
            return base
    return None


def _discover_series_urls(base):
    """
    يعيد قائمة URLs لصفحات المسلسلات.
    يجرب مسارات متعددة.
    """
    candidates = [
        f"{base}/video/series/",
        f"{base}/video/series/page/1/",
        f"{base}/series/",
        f"{base}/مسلسلات/",
        f"{base}/%d9%85%d8%b3%d9%84%d8%b3%d9%84%d8%a7%d8%aa/",
        f"{base}/category/series/",
    ]

    series_urls = set()
    working_base = None

    for url in candidates:
        r = _get(url, timeout=15)
        if not r or r.status_code != 200:
            continue
        if "/video/series" not in r.text and "modablaj" not in r.text:
            continue
        working_base = url
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/video/series/" in href or "/series/" in href:
                full = urljoin(base, href).rstrip("/")
                # تجنب القائمة نفسها
                if "/page/" in full or full.endswith("/video/series") or full.endswith("/series"):
                    continue
                series_urls.add(full)

        if series_urls:
            print(f"   ✅ مسار يعمل: {url} ({len(series_urls)} رابط)")
            break

    # جلب صفحات إضافية
    if working_base and series_urls:
        for page in range(2, 8):
            page_url = working_base.rstrip("/").split("/page/")[0] + f"/page/{page}/"
            r = _get(page_url, timeout=15)
            if not r or r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            new = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/video/series/" in href or "/series/" in href:
                    full = urljoin(base, href).rstrip("/")
                    if "/page/" in full or full.endswith("/video/series") or full.endswith("/series"):
                        continue
                    if full not in series_urls:
                        series_urls.add(full)
                        new += 1
            if new == 0:
                break
            print(f"   صفحة {page}: +{new}")

    return list(series_urls)


def _parse_series(url):
    r = _get(url, timeout=20)
    if not r or r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    name = ""
    h1 = soup.find("h1")
    if h1:
        name = _clean_name(h1.get_text(strip=True))
    if not name:
        og = soup.find("meta", property="og:title")
        if og:
            name = _clean_name(og.get("content", ""))

    poster = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        poster = og_img["content"]

    genre = ""
    for li in soup.select("ul.postlist li"):
        lbl = li.find("label")
        if lbl and "النوع" in lbl.get_text():
            a = li.find("a")
            if a:
                genre = a.get_text(strip=True)
                break

    # ═══ الحلقات من ul.eplist ═══
    episodes = []
    seen = set()
    eplist = soup.find("ul", class_="eplist")
    if eplist:
        for a in eplist.find_all("a", class_="epNum", href=True):
            href = a["href"]
            sp = a.find("span")
            if not sp:
                continue
            try:
                num = int(sp.get_text(strip=True))
            except ValueError:
                continue
            if num in seen or num < 1 or num > 10000:
                continue
            seen.add(num)
            page_url = urljoin(url, href)
            if not page_url.endswith("/"):
                page_url += "/"
            episodes.append({
                "num": num,
                "url": page_url + "?do=watch",
                "page_url": page_url,
            })

    if not episodes:
        # جرّب النمط الاحتياطي
        for pat in EPISODE_PATTERNS:
            for m in pat.finditer(r.text):
                raw_url = m.group(1)
                try:
                    num = int(m.group(2))
                except ValueError:
                    continue
                if num in seen or num < 1 or num > 10000:
                    continue
                seen.add(num)
                full = urljoin(url, raw_url)
                if not full.endswith("/"):
                    full += "/"
                episodes.append({
                    "num": num,
                    "url": full + "?do=watch",
                    "page_url": full,
                })
            if episodes:
                break

    if not episodes:
        return None

    return {
        "name": name or url.rstrip("/").split("/")[-1],
        "poster": poster,
        "genre": genre,
        "episodes": sorted(episodes, key=lambda x: x["num"]),
        "source_url": url,
    }


def fetch():
    base = _try_base()
    if not base:
        print(f"   ⚠️ لا نطاق يعمل")
        return []

    print(f"   📄 جلب قائمة المسلسلات من {base}...")
    urls = _discover_series_urls(base)
    if not urls:
        print(f"   ⚠️ لا مسلسلات")
        return []

    print(f"   🔄 جلب تفاصيل {len(urls)} مسلسل...")
    series = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_parse_series, u) for u in urls]
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                s = fut.result()
                if s and s["episodes"]:
                    series.append(s)
                    print(f"      [{done}/{len(urls)}] ✅ {s['name'][:50]}: {len(s['episodes'])}")
            except Exception:
                pass

    return series