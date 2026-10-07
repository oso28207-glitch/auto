"""yam source — yam.ahwaktv.net"""
import os
import re
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _get(url, timeout=30):
    try:
        return cffi.get(url, impersonate="chrome120", timeout=timeout,
                        verify=False, headers={"User-Agent": UA})
    except Exception:
        return None


def _fetch_series_list():
    url = f"{BASE}{SERIES_PATH}"
    r = _get(url)
    if not r or r.status_code != 200:
        print(f"   ⚠️ فشل جلب {url}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    seen = set()

    # كل الروابط الداخلية التي تشبه صفحة مسلسل
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if (not href or href.startswith("#")
                or href.startswith("javascript:") or href.startswith("mailto:")):
            continue
        full = urljoin(BASE, href)
        if BASE not in full:
            continue
        # تجاهل الصفحات الإدارية
        if any(x in full for x in ["moslslat.php", "topvideos.php",
                                    "?page=", "?cat=", "/category/"]):
            continue
        if full in seen:
            continue
        seen.add(full)
        links.append({
            "url": full,
            "name": a.get_text(strip=True),
        })

    print(f"   ✅ وُجد {len(links)} رابط")
    return links


def _extract_episodes(series_url):
    """استخراج الحلقات والبوستر من صفحة المسلسل."""
    r = _get(series_url)
    if not r or r.status_code != 200:
        return [], ""

    soup = BeautifulSoup(r.text, "html.parser")

    # البوستر
    poster = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        poster = og["content"]

    # الحلقات
    episodes = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        m = re.search(
            r"(?:الحلقة|حلقة|episode|ep)[\s\-_]*?(\d{1,4})",
            text + " " + href, re.I)
        if m:
            try:
                num = int(m.group(1))
            except ValueError:
                continue
            if num < 1 or num > 5000:
                continue
            full = urljoin(series_url, href)
            if num not in episodes:
                episodes[num] = full

    return [{"num": n, "url": episodes[n]} for n in sorted(episodes)], poster


def fetch():
    links = _fetch_series_list()
    if not links:
        return []

    print(f"   🔄 جلب تفاصيل {len(links)} مسلسل...")

    def _enrich(item):
        episodes, poster = _extract_episodes(item["url"])
        name = item.get("name", "").strip()
        if not name or len(name) < 3:
            r = _get(item["url"])
            if r:
                soup = BeautifulSoup(r.text, "html.parser")
                t = soup.find("title")
                if t:
                    name = t.get_text(strip=True).split("|")[0].strip()
        return {
            "name": name or item["url"].rstrip("/").split("/")[-1],
            "poster": poster,
            "genre": "",
            "episodes": episodes,
            "source_url": item["url"],
        }

    series_list = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(_enrich, item) for item in links]
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                s = fut.result()
                if s["episodes"]:
                    series_list.append(s)
                    print(f"      [{done}/{len(links)}] "
                          f"✅ {s['name'][:50]}: {len(s['episodes'])} حلقة")
            except Exception:
                pass

    return series_list