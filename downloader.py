"""
يستخدم yt-dlp أولاً (يدعم 1000+ موقع)، ثم requests كخطة بديلة.
"""

import re
import subprocess
from pathlib import Path

import requests

from config import config, MEDIA_DIR
from errors import DownloadError


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


def _download_with_ytdlp(url: str, out_path: Path) -> bool:
    """yt-dlp يتعامل مع 1000+ موقع تلقائياً."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--progress",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True
        print(f"    ↳ yt-dlp فشل: {result.stderr[:200]}")
        return False
    except FileNotFoundError:
        print("    ↳ yt-dlp غير مثبت، سيتم استخدام requests")
        return False
    except subprocess.TimeoutExpired:
        print("    ↳ yt-dlp تجاوز الوقت المسموح")
        return False
    except Exception as e:
        print(f"    ↳ yt-dlp خطأ: {e}")
        return False


def _download_with_requests(url: str, out_path: Path) -> bool:
    """خطة بديلة للروابط المباشرة."""
    headers = {"User-Agent": config.USER_AGENT}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            if out_path.exists() and out_path.stat().st_size > 0:
                return True
        return False
    except Exception as e:
        print(f"    ↳ requests خطأ: {e}")
        return False


def download_episode(series: str, episode: int, url: str) -> Path:
    """
    يحمّل الحلقة. يرفع DownloadError عند الفشل → يوقف كل شيء.
    """
    safe_series = _safe_name(series)
    out_dir = MEDIA_DIR / safe_series
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ep{episode:03d}.mp4"

    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"    ↳ موجودة مسبقاً: {out_path.name}")
        return out_path

    print(f"    ↳ تحميل: {url}")

    if _download_with_ytdlp(url, out_path):
        print(f"    ✓ تم التحميل: {out_path.name} ({out_path.stat().st_size / 1e6:.1f} MB)")
        return out_path

    if _download_with_requests(url, out_path):
        print(f"    ✓ تم التحميل (requests): {out_path.name}")
        return out_path

    raise DownloadError(f"فشل تحميل {series} الحلقة {episode} من {url}")