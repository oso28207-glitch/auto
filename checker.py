"""
يفحص الموقع الخارجي بحثاً عن حلقات جديدة.
يدعم yt-dlp للكشف عن الفيديوهات، أو requests + BeautifulSoup.
"""

import re

import requests
from bs4 import BeautifulSoup

from config import config
from database import db
from errors import SourceError


def _extract_episode_number(text: str) -> int:
    """يستخرج رقم الحلقة من نص مثل 'الحلقة 5' أو 'Episode 5'."""
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


def fetch_series_list() -> list[dict]:
    """يجلب قائمة المسلسلات من الموقع."""
    url = config.SOURCE_BASE_URL + config.SOURCE_SERIES_PATH
    try:
        r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise SourceError(f"فشل جلب قائمة المسلسلات من {url}: {e}", e)

    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select(config.SOURCE_LIST_SELECTOR)
    if not items:
        raise SourceError(f"لم يتم العثور على مسلسلات في {url}")

    series = []
    for a in items:
        href = a.get("href", "")
        name = a.get_text(strip=True) or href
        if href:
            series.append({
                "name": name,
                "url": href if href.startswith("http") else config.SOURCE_BASE_URL + href,
            })
    return series


def fetch_episodes(series_url: str) -> list[dict]:
    """يجلب قائمة الحلقات من صفحة المسلسل."""
    try:
        r = requests.get(series_url, headers={"User-Agent": config.USER_AGENT}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise SourceError(f"فشل جلب حلقات {series_url}: {e}", e)

    soup = BeautifulSoup(r.text, "html.parser")
    episodes = []

    for a in soup.select(config.SOURCE_EP_SELECTOR):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        ep_num = _extract_episode_number(text) or _extract_episode_number(href)
        if ep_num and href:
            episodes.append({
                "number": ep_num,
                "url": href if href.startswith("http") else config.SOURCE_BASE_URL + href,
                "title": text,
            })

    # ترتيب تصاعدي
    episodes.sort(key=lambda x: x["number"])
    return episodes


def find_new_episodes(series_name: str) -> list[dict]:
    """يعيد الحلقات الجديدة التي لم تُرفع بعد."""
    series = db.get_series(series_name)
    if not series.get("url"):
        return []

    all_eps = fetch_episodes(series["url"])
    new = []
    for ep in all_eps:
        status = db.episode_status(series_name, ep["number"])
        if status != "uploaded":
            new.append(ep)
    return new