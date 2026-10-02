"""
downloader.py — تحميل الفيديوهات من u.3seq.com (Cloudflare-protected)
يستخدم yt-dlp مع impersonation، ثم curl_cffi كخطة بديلة.
"""

import os
import re
import subprocess
from pathlib import Path

from config import config, MEDIA_DIR
from errors import DownloadError


# ═══════════════════════════════════════════════════════════════
# إعدادات
# ═══════════════════════════════════════════════════════════════
IMPERSONATE_TARGET = os.environ.get("IMPERSONATE_TARGET", "chrome")
CHUNK_SIZE = 1024 * 1024  # 1MB


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


# ═══════════════════════════════════════════════════════════════
# 1) yt-dlp مع impersonation
# ═══════════════════════════════════════════════════════════════
def _download_with_ytdlp(url: str, out_path: Path) -> bool:
    """
    يحمّل باستخدام yt-dlp مع --impersonate لتجاوز Cloudflare.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--progress",
        "--impersonate", IMPERSONATE_TARGET,
        "--extractor-args", "generic:impersonate",
        "--user-agent", config.USER_AGENT,
        "--referer", config.SOURCE_BASE_URL + "/",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        url,
    ]

    print(f"    ↳ yt-dlp --impersonate {IMPERSONATE_TARGET}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # ساعتان
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True

        # طباعة الخطأ للمساعدة في التشخيص
        stderr = result.stderr or ""
        if "impersonate" in stderr.lower() or "curl_cffi" in stderr.lower():
            print(f"    ⚠️ مشكلة في impersonation: {stderr[:300]}")
        else:
            print(f"    ↳ yt-dlp فشل: {stderr[:300]}")
        return False

    except FileNotFoundError:
        print("    ↳ yt-dlp غير مثبت")
        return False
    except subprocess.TimeoutExpired:
        print("    ↳ yt-dlp تجاوز الوقت المسموح")
        return False
    except Exception as e:
        print(f"    ↳ yt-dlp خطأ: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 2) curl_cffi (خطة بديلة — تتجاوز Cloudflare مباشرة)
# ═══════════════════════════════════════════════════════════════
def _download_with_curl_cffi(url: str, out_path: Path) -> bool:
    """
    يحمّل الملف مباشرة عبر curl_cffi مع محاكاة بصمة Chrome.
    مفيد عندما يفشل yt-dlp لكن الرابط يعيد ملف فيديو مباشر.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("    ↳ curl_cffi غير مثبت")
        return False

    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.9",
        "Referer": config.SOURCE_BASE_URL + "/",
        "Origin": config.SOURCE_BASE_URL,
    }

    print(f"    ↳ curl_cffi (impersonate={IMPERSONATE_TARGET})")

    try:
        # أولاً: تحقق من الحجم
        head = cffi_requests.head(
            url,
            headers=headers,
            impersonate=IMPERSONATE_TARGET,
            timeout=60,
            allow_redirects=True,
        )

        if head.status_code >= 400:
            print(f"    ↳ HEAD {head.status_code}")
            return False

        content_type = head.headers.get("content-type", "")
        if "video" not in content_type and "octet-stream" not in content_type:
            print(f"    ↳ ليس فيديو: {content_type}")
            return False

        total_size = int(head.headers.get("content-length", 0))
        print(f"    ↳ الحجم: {total_size / 1024 / 1024:.1f}MB")

        # ثانيًا: حمّل الفيديو
        with cffi_requests.get(
            url,
            headers=headers,
            impersonate=IMPERSONATE_TARGET,
            stream=True,
            timeout=300,
            allow_redirects=True,
        ) as r:
            r.raise_for_status()

            downloaded = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        # اطبع كل 10MB
                        if downloaded % (10 * CHUNK_SIZE) == 0:
                            pct = (downloaded / total_size * 100) if total_size else 0
                            print(f"       ... {downloaded / 1024 / 1024:.1f}MB ({pct:.0f}%)")

        if out_path.exists() and out_path.stat().st_size > 0:
            return True
        return False

    except Exception as e:
        print(f"    ↳ curl_cffi خطأ: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 3) الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
def download_episode(series: str, episode: int, url: str) -> Path:
    """
    يحمّل الحلقة:
      1. يتحقق إن كانت موجودة مسبقاً.
      2. يجرب yt-dlp مع impersonation.
      3. يجرب curl_cffi مباشرة.
      4. يرفع DownloadError إذا فشل كل شيء.
    """
    safe_series = _safe_name(series)
    out_dir = MEDIA_DIR / safe_series
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ep{episode:03d}.mp4"

    # ─── 1) موجودة مسبقاً ───
    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"    ↳ موجودة مسبقاً: {out_path.name}")
        return out_path

    print(f"    ↳ تحميل: {url}")

    # ─── 2) yt-dlp ───
    if _download_with_ytdlp(url, out_path):
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"    ✓ تم التحميل (yt-dlp): {out_path.name} ({size_mb:.1f}MB)")
        return out_path

    # نظّف أي ملف جزئي
    if out_path.exists():
        try:
            out_path.unlink()
        except Exception:
            pass

    # ─── 3) curl_cffi ───
    if _download_with_curl_cffi(url, out_path):
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"    ✓ تم التحميل (curl_cffi): {out_path.name} ({size_mb:.1f}MB)")
        return out_path

    # ─── 4) فشل ───
    raise DownloadError(
        f"فشل تحميل {series} الحلقة {episode} من {url}\n"
        f"   جرّب: pip install 'yt-dlp[default,curl-cffi]' curl-cffi==0.7.4"
    )
