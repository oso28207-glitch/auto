"""u3seq source — يجرّب مسارات متعددة للعثور على قائمة المسلسلات"""
import os
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _get(url, timeout=30):
    try:
        return cffi.get(
            url, impersonate="chrome120", timeout=timeout, verify=False,
            headers={"User-Agent": UA,
                     "Accept-Language": "ar,en-US;q=0.9,en;q=0.8"},
        )
    except Exception:
        return None


def _clean_name(raw):
    if not raw:
        return ""
    name = html.unescape(raw).strip()
    name = re.sub(r'\s*[-–—]\s*الحلقة\s*\d+.*$', '', name).strip()
    name = re.sub(r'\s*الحلقة\s*\d+\s*(مدبلجة|مترجمة)?\s*$', '', name).strip()
    return name


def _try_archive_paths():
    """يجرّب مسارات متعددة لأرشيف المسلسلات."""
    candidates = [
        f"{BASE}/video/series/",
        f"{BASE}/series/",
        f"{BASE}/مسلسلات/",
        f"{BASE}/video/series/page/1/",
        f"{BASE}/genre/series/",
    ]
    for url in candidates:
        r = _get(url, timeout=15)
        if not r or r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/video/series/" in href or "/series/" in href:
                full = urljoin(BASE, href).rstrip("/")
                if full != f"{BASE}/video/series" and full != f"{BASE}/series":
                    links.add(full)
        if links:
            print(f"   ✅ مسار ناجح: {url} ({len(links)} رابط)")
            return url, links
    return None, set()


def _fetch_all_series():
    base_url, first_links = _try_archive_paths()
    if not base_url:
        return []

    all_links = set(first_links)
    # جلب صفحات إضافية
    for page in range(2, 11):
        url = f"{base_url.rstrip('/')}/page/{page}/"
        r = _get(url, timeout=15)
        if not r or r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/video/series/" in href or "/series/" in href:
                full = urljoin(BASE, href).rstrip("/")
                if full not in all_links:
                    all_links.add(full)
                    new += 1
        if new == 0:
            break
        print(f"   صفحة {page}: +{new}")

    return list(all_links)


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
            if num in seen:
                continue
            seen.add(num)
            page_url = urljoin(url, href)
            if not page_url.endswith("/"):
                page_url += "/"
            episodes.append({"num": num, "url": page_url + "?do=watch"})

    if not episodes:
        return None

    return {
        "name": name, "poster": poster, "genre": genre,
        "episodes": sorted(episodes, key=lambda x: x["num"]),
        "source_url": url,
    }


def fetch():
    print(f"   📄 جلب قائمة المسلسلات...")
    urls = _fetch_all_series()
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