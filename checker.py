"""
checker.py — فحص المسلسلات والحلقات من موقع WordPress (u.3seq.cam)
يستخدم REST API بدلاً من تحليل HTML.
"""

import re
import requests

from config import config
from database import db
from errors import SourceError


def _extract_episode_number(text: str) -> int:
    """يستخرج رقم الحلقة من نص مثل 'الحلقة 5' أو 'Episode 5'."""
    if not text:
        return 0
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


def _api_get(path: str, params: dict = None) -> list | dict:
    """يستدعي WordPress REST API ويُعيد JSON."""
    if not config.SOURCE_BASE_URL:
        raise SourceError("SOURCE_BASE_URL فارغ. اضبطه في .env أو GitHub Secrets.")

    base = config.SOURCE_BASE_URL.rstrip("/")
    url = f"{base}{path}"
    if not url.startswith(("http://", "https://")):
        raise SourceError(f"URL غير صالح: '{url}'")

    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        raise SourceError(f"HTTP {e.response.status_code} من {url}: {e}", e)
    except Exception as e:
        raise SourceError(f"فشل جلب {url}: {e}", e)


def fetch_series_list() -> list[dict]:
    """
    يجلب قائمة المسلسلات من REST API.
    كل مسلسل يُعاد كقاموس {name, slug, url, category_id}.
    """
    print(f"   ↳ جلب من: {config.SOURCE_BASE_URL}{config.SOURCE_SERIES_PATH}")

    # جلب المنشورات من التصنيف المحدد
    posts = _api_get(config.SOURCE_SERIES_PATH)
    if not isinstance(posts, list):
        raise SourceError(f"REST API أعاد استجابة غير متوقعة: {type(posts)}")

    series_map = {}
    for post in posts:
        link = post.get("link", "")
        title = (post.get("title") or {}).get("rendered", "")
        if not link or not title:
            continue

        # استخراج اسم المسلسل (بدون رقم الحلقة)
        # مثال: "مسلسل حيث تشرق الشمس الحلقة 3 مترجمة" → "مسلسل حيث تشرق الشمس"
        clean = re.sub(r"\s*الحلقة\s*\d+.*$", "", title).strip()
        clean = re.sub(r"\s*[Ee]pisode\s*\d+.*$", "", clean).strip()
        clean = clean or title

        if clean not in series_map:
            series_map[clean] = {
                "name": clean,
                "url": link,
                "posts": [],
            }
        series_map[clean]["posts"].append({
            "id": post.get("id"),
            "title": title,
            "link": link,
            "episode": _extract_episode_number(title),
        })

    series_list = list(series_map.values())
    print(f"   وجدت {len(series_list)} مسلسل في الصفحة الأولى")
    return series_list


def fetch_episodes(series_url: str) -> list[dict]:
    """
    بما أن REST API يعيد كل الحلقات في نفس الاستجابة،
    نستخدم الدالة السابقة مباشرة. هذه الدالة موجودة للتوافق.
    """
    return []


def find_new_episodes(series_name: str) -> list[dict]:
    """يعيد الحلقات الجديدة التي لم تُرفع بعد."""
    series = db.get_series(series_name)
    if not series.get("url"):
        return []

    # جلب كل منشورات المسلسل من API
    posts = _api_get(config.SOURCE_SERIES_PATH)

    new = []
    for post in posts:
        link = post.get("link", "")
        title = (post.get("title") or {}).get("rendered", "")
        ep_num = _extract_episode_number(title)

        if ep_num and db.episode_status(series_name, ep_num) != "uploaded":
            new.append({
                "number": ep_num,
                "url": link,
                "title": title,
            })

    new.sort(key=lambda x: x["number"])
    return new
