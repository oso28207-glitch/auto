"""
yam source — استخراج مسلسلات yam.ahwaktv.net
★ الرابط الفعلي للحلقة: see.php?vid=XXX
"""
import os
import re
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SKIP_NAMES = {
    "الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
    "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن",
    "سياسة الخصوصية", "الأكثر مشاهدة",
}

# أنماط روابط الفيديو (see.php مفضّل)
SEE_PATTERN = re.compile(r'see\.php\?vid=([A-Za-z0-9]+)', re.I)
WATCH_PATTERN = re.compile(r'watch\.php\?vid=([A-Za-z0-9]+)', re.I)


def _get(url, timeout=30):
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
    name = raw.strip()
    name = re.sub(r'\s*[-–—]\s*اهواك.*$', '', name).strip()
    name = re.sub(r'\s*[-–—]\s*مشاهدة\s*افلام.*$', '', name).strip()
    name = re.sub(r'\s*\|.*$', '', name).strip()
    return name


def _is_nav_page(name):
    if not name or len(name.strip()) < 4:
        return True
    n = name.strip()
    if n in SKIP_NAMES:
        return True
    for skip in SKIP_NAMES:
        if n.startswith(skip) and len(n) < len(skip) + 15:
            return True
    return False


def _fetch_series_list():
    """جلب روابط المسلسلات (بدون روابط الفيديو)."""
    candidates = [
        f"{BASE}{SERIES_PATH}",
        f"{BASE}/moslslat.php",
        f"{BASE}/",
    ]
    links, seen = [], set()
    base_domain = urlparse(BASE).netloc

    for url in candidates:
        r = _get(url, timeout=20)
        if not r or r.status_code != 200:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = _clean_name(a.get_text(strip=True))
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            if _is_nav_page(text):
                continue
            full = urljoin(BASE, href)
            if base_domain not in full:
                continue
            # ★ استبعاد كل ما هو فيديو أو تنقّل
            if any(x in full for x in ["moslslat.php", "topvideos.php",
                                        "?page=", "?cat=", "?s=", "/category/",
                                        "/tag/", "/actor/",
                                        "see.php", "watch.php"]):
                continue
            if full in seen:
                continue
            if len(text) < 5:
                continue
            seen.add(full)
            links.append({"url": full, "name": text})
            found += 1
        if found >= 20:
            print(f"   ✅ {found} رابط من {url}")
            break

    print(f"   ✅ الإجمالي: {len(links)} رابط")
    return links


def _extract_episodes(series_url):
    """
    استخراج حلقات see.php?vid=XXX.
    ★ الأولوية: see.php → watch.php
    """
    r = _get(series_url, timeout=25)
    if not r or r.status_code != 200:
        return [], ""

    soup = BeautifulSoup(r.text, "html.parser")

    poster = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        poster = og["content"]

    episodes = {}  # num → dict

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)

        # ★ الأولوية: see.php
        see_m = SEE_PATTERN.search(href)
        watch_m = WATCH_PATTERN.search(href)
        if not see_m and not watch_m:
            continue

        vid = see_m.group(1) if see_m else watch_m.group(1)

        # استخراج رقم الحلقة
        num = None
        m = re.search(r'(?:الحلقة|حلقة|ep|episode)[\s\-_]*?(\d{1,4})',
                      text + " " + href, re.I)
        if m:
            try:
                num = int(m.group(1))
            except ValueError:
                pass
        if num is None:
            num = len(episodes) + 1

        if num in episodes:
            # إذا كان الرابط الحالي see.php والقديم watch.php → استبدل
            if see_m and "watch.php" in episodes[num]["url"]:
                episodes[num]["url"] = f"{BASE}/see.php?vid={vid}"
            continue

        # ★ استخدم see.php دائماً (لأنه الصفحة الفعلية للفيديو)
        full_url = f"{BASE}/see.php?vid={vid}"

        episodes[num] = {
            "num": num,
            "url": full_url,
            "vid": vid,
        }

    return [episodes[n] for n in sorted(episodes)], poster


def fetch():
    print(f"   📄 جلب قائمة المسلسلات...")
    links = _fetch_series_list()
    if not links:
        return []

    print(f"   🔄 جلب تفاصيل {len(links)} مسلسل...")

    def _enrich(item):
        episodes, poster = _extract_episodes(item["url"])
        name = item["name"]
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
                    print(f"      [{done}/{len(links)}] ✅ {s['name'][:50]}: {len(s['episodes'])}")
            except Exception:
                pass

    return series_list