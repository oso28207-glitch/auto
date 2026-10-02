"""
downloader.py — تحميل الفيديوهات من u.3seq.com (Cloudflare-protected)
يستخدم استراتيجيات متعددة: yt-dlp, curl_cffi, FlareSolverr.
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
# عنوان FlareSolverr (يمكن تشغيله محلياً أو في Docker)
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
CHUNK_SIZE = 1024 * 1024  # 1MB


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:120]


# ═══════════════════════════════════════════════════════════════
# 1) yt-dlp مع impersonation
# ═══════════════════════════════════════════════════════════════
def _download_with_ytdlp(url: str, out_path: Path) -> bool:
    """
    يحمّل باستخدام yt-dlp مع --impersonate و --cookies.
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
        # --- محاولة استخدام الكوكيز إذا كانت موجودة ---
        "--cookies", "/tmp/cookies.txt",  # سنقوم بإنشائه إذا لزم الأمر
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        url,
    ]

    print(f"    ↳ yt-dlp --impersonate {IMPERSONATE_TARGET} (with cookies)")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # ساعتان
        )
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True

        stderr = result.stderr or ""
        if "cookie" in stderr.lower() and "not found" in stderr.lower():
            print("    ⚠️ ملف الكوكيز غير موجود، سيتم تجاهله.")
        elif "403" in stderr and "cloudflare" in stderr.lower():
            print("    ⚠️ لا يزال Cloudflare يمنع الطلب حتى مع impersonation.")
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
# 2) curl_cffi (خطة بديلة)
# ═══════════════════════════════════════════════════════════════
def _download_with_curl_cffi(url: str, out_path: Path) -> bool:
    """
    يحمّل الملف مباشرة عبر curl_cffi مع محاكاة بصمة Chrome.
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
                        if downloaded % (10 * CHUNK_SIZE) == 0:
                            pct = (downloaded / total_size * 100) if total_size else 0
                            print(f"       ... {downloaded / 1024 / 1024:.1f}MB ({pct:.0f}%)")

        return out_path.exists() and out_path.stat().st_size > 0

    except Exception as e:
        print(f"    ↳ curl_cffi خطأ: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 3) FlareSolverr (خطة متقدمة)
# ═══════════════════════════════════════════════════════════════
def _download_with_flaresolverr(url: str, out_path: Path) -> bool:
    """
    يستخدم FlareSolverr لحل تحدي Cloudflare ثم يحمّل الفيديو.
    """
    try:
        import requests
        import json
    except ImportError:
        print("    ↳ requests غير مثبت")
        return False

    print(f"    ↳ FlareSolverr: {FLARESOLVERR_URL}")

    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000  # 60 ثانية
    }

    try:
        response = requests.post(
            FLARESOLVERR_URL,
            json=payload,
            timeout=90
        )
        data = response.json()
    except Exception as e:
        print(f"    ↳ FlareSolverr غير متاح: {e}")
        return False

    if not data.get("solution"):
        print("    ↳ FlareSolverr: لم يتمكن من حل التحدي.")
        return False

    solution = data["solution"]
    # الحصول على الكوكيز الناتجة
    cookies = solution.get("cookies", [])
    print(f"    ✓ FlareSolverr حل التحدي. عدد الكوكيز: {len(cookies)}")

    # يمكننا استخدام الكوكيز مع requests أو curl_cffi
    # هنا سنستخدم curl_cffi مع الكوكيز الناتجة
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("    ↳ curl_cffi غير مثبت")
        return False

    # تحويل الكوكيز إلى صيغة curl_cffi
    cookie_jar = {c['name']: c['value'] for c in cookies}
    
    headers = {
        "User-Agent": solution.get("userAgent", config.USER_AGENT),
        "Referer": config.SOURCE_BASE_URL + "/",
    }

    try:
        r = cffi_requests.get(
            url,
            headers=headers,
            cookies=cookie_jar,
            impersonate=IMPERSONATE_TARGET,
            stream=True,
            timeout=300,
            allow_redirects=True,
        )
        r.raise_for_status()

        total_size = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded % (10 * CHUNK_SIZE) == 0:
                        pct = (downloaded / total_size * 100) if total_size else 0
                        print(f"       ... {downloaded / 1024 / 1024:.1f}MB ({pct:.0f}%)")

        return out_path.exists() and out_path.stat().st_size > 0

    except Exception as e:
        print(f"    ↳ فشل التحميل بعد FlareSolverr: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 4) الدالة الرئيسية
# ═══════════════════════════════════════════════════════════════
def download_episode(series: str, episode: int, url: str) -> Path:
    """
    يحمّل الحلقة باستخدام استراتيجيات متعددة.
    """
    safe_series = _safe_name(series)
    out_dir = MEDIA_DIR / safe_series
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ep{episode:03d}.mp4"

    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"    ↳ موجودة مسبقاً: {out_path.name}")
        return out_path

    print(f"    ↳ تحميل: {url}")

    # 1) yt-dlp
    if _download_with_ytdlp(url, out_path):
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"    ✓ تم التحميل (yt-dlp): {out_path.name} ({size_mb:.1f}MB)")
        return out_path

    if out_path.exists():
        try:
            out_path.unlink()
        except Exception:
            pass

    # 2) curl_cffi
    if _download_with_curl_cffi(url, out_path):
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"    ✓ تم التحميل (curl_cffi): {out_path.name} ({size_mb:.1f}MB)")
        return out_path

    if out_path.exists():
        try:
            out_path.unlink()
        except Exception:
            pass

    # 3) FlareSolverr
    if _download_with_flaresolverr(url, out_path):
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"    ✓ تم التحميل (FlareSolverr): {out_path.name} ({size_mb:.1f}MB)")
        return out_path

    raise DownloadError(
        f"فشل تحميل {series} الحلقة {episode} من {url}\n"
        f"   جرّب: تشغيل FlareSolverr أو استخدام كوكيز cf_clearance."
    )
