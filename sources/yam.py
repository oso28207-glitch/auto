"""yam source — مع فلترة صفحات التنقل وتنظيف الأسماء"""
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

# عناوين يجب تجاهلها (صفحات تنقل)
SKIP_NAMES = {
    "الصفحة الرئيسية", "الرئيسية", "جديد الأفلام", "أحدث الحلقات",
    "المسلسلات", "الأفلام", "اهواك تي في", "اتصل بنا", "من نحن",
    "سياسة الخصوصية", "الأكثر مشاهدة",
}


def _get(url, timeout=30):
    try:
        return cffi.get(url, impersonate="chrome120", timeout=timeout,
                        verify=False, headers={"User-Agent": UA})
    except Exception:
        return None


def _clean_name(raw):
    """تنظيف الاسم من لاحقات الموقع."""
    if not raw:
        return ""
    name = raw.strip()
    # إزالة " - اهواك تي في - مشاهدة..."
    name = re.sub(r'\s*[-–—]\s*اهواك.*$', '', name).strip()
    name = re.sub(r'\s*[-–—]\s*مشاهدة\s*افلام.*$', '', name).strip()
    name = re.sub(r'\s*\|.*$', '', name).strip()
    return name


def _is_nav_page(name):
    if not name:
        return True
    n = name.strip()
    if n in SKIP_NAMES:
        return True
    for skip in SKIP_NAMES:
        if n.startswith(skip) and len(n) < len(skip) + 15:
            return True
    if len(n) < 4:
        return True
    return False


def _fetch_series_list():
    """فقط روابط المسلسلات الحقيقية."""
    url = f"{BASE}{SERIES_PATH}"
    r = _get(url)
    if not r or r.status_code != 200:
        print(f"   ⚠️ فشل {url}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = _clean_name(a.get_text(strip=True))
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(BASE, href)
        if BASE not in full:
            continue
        if full in seen:
            continue
        # تجاوز صفحات التنقل والصفحات الإدارية
        if _is_nav_page(text):
            continue
        if any(x in full for x in ["moslslat.php", "topvideos.php",
                                    "?page=", "?cat=", "?s=",
                                    "/category/", "/tag/", "/actor/"]):
            continue
        # اسم معقول
        if len(text) < 4:
            continue
        seen.add(full)
        links.append({"url": full, "name": text})

    print(f"   ✅ وُجد {len(links)} رابط مسلسل")
    return links


def _extract_episodes(series_url):
    """استخراج حلقات watch.php?vid=..."""
    r = _get(series_url)
    if not r or r.status_code != 200:
        return [], ""

    soup = BeautifulSoup(r.text, "html.parser")

    poster = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        poster = og["content"]

    episodes = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # ★ الروابط الفعلية: watch.php?vid=XXX
        if "watch.php" not in href and "vid=" not in href:
            continue
        m = re.search(r'vid=([A-Za-z0-9]+)', href)
        if not m:
            continue
        # استخراج رقم الحلقة من النص أو الرابط
        text = a.get_text(strip=True)
        num_m = re.search(r'(?:الحلقة|حلقة|ep|episode)[\s\-_]*?(\d{1,4})',
                          text + " " + href, re.I)
        num = None
        if num_m:
            try:
                num = int(num_m.group(1))
            except ValueError:
                pass
        if num is None:
            # استخدم ترتيب الرابط
            num = len(episodes) + 1
        if num in episodes:
            continue
        full = urljoin(series_url, href)
        episodes[num] = full

    return [{"num": n, "url": episodes[n]} for n in sorted(episodes)], poster


def fetch():
    links = _fetch_series_list()
    if not links:
        return []

    print(f"   🔄 جلب تفاصيل {len(links)} مسلسل...")

    def _enrich(item):
        episodes, poster = _extract_episodes(item["url"])
        name = item["name"]
        if not name:
            r = _get(item["url"])
            if r:
                soup = BeautifulSoup(r.text, "html.parser")
                t = soup.find("title")
                if t:
                    name = _clean_name(t.get_text(strip=True))
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