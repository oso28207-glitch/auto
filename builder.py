"""بناء تدريجي — يحدّث فقط ما تغيّر."""

import json
import shutil
from datetime import datetime
from pathlib import Path

from config import config, DOCS_DIR, ASSETS_DIR
from database import db
from errors import BuildError


def _ensure_structure():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "series").mkdir(parents=True, exist_ok=True)


def _copy_assets(force=False):
    """ينسخ ملفات الموقع الأساسية."""
    if not ASSETS_DIR.exists():
        return
    for f in ASSETS_DIR.iterdir():
        if f.is_file():
            dest = DOCS_DIR / f.name
            if force or not dest.exists():
                shutil.copy2(f, dest)


def _write_config():
    """يكتب config.js مع API_BASE."""
    cfg = f'window.APP_CONFIG = {{ API_BASE: "{config.API_BASE}" }};\n'
    (DOCS_DIR / "config.js").write_text(cfg, encoding="utf-8")


def _write_videos_json():
    """يكتب videos.json الكامل."""
    videos = db.all_videos()
    (DOCS_DIR / "videos.json").write_text(
        json.dumps(videos, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(videos)


def _series_to_dict(name):
    """يحوّل بيانات مسلسل إلى قاموس للـ JSON."""
    s = db.get_series(name)
    eps = s.get("episodes", {})
    uploaded = []
    for k, v in sorted(eps.items(), key=lambda x: int(x[0])):
        if v.get("status") == "uploaded" and v.get("message_id"):
            uploaded.append({
                "episode": int(k),
                "message_id": v["message_id"],
                "url": v.get("url", ""),
                "title": v.get("title", f"الحلقة {k}"),
            })
    return {
        "name": name,
        "url": s.get("url", ""),
        "slug": s.get("slug", name.replace(" ", "_")),
        "episodes": uploaded,
        "count": len(uploaded),
        "last_updated": s.get("last_updated"),
    }


def _write_series_index():
    """يكتب فهرس كل المسلسلات في ملف واحد (للـ SPA)."""
    all_series = db.all_series()
    index = [_series_to_dict(name) for name in all_series]
    index.sort(key=lambda s: s["name"])
    (DOCS_DIR / "series.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(index)


def build_incremental(changed_series=None):
    """
    بناء تدريجي:
      - يحدّث series.json و videos.json دائماً (سريع).
      - ينسخ الأصول فقط عند أول بناء.
      - لا يعيد بناء الموقع كاملاً.
    """
    try:
        _ensure_structure()

        # الأصول — مرة واحدة فقط
        _copy_assets(force=not (DOCS_DIR / "index.html").exists())
        _write_config()

        # البيانات — دائماً
        n_videos = _write_videos_json()
        n_series = _write_series_index()

        msg = f"[Builder] ✅ {n_series} مسلسل، {n_videos} فيديو"
        if changed_series:
            msg += f" (محدّث: {', '.join(changed_series[:3])}{'...' if len(changed_series) > 3 else ''})"
        print(msg)

    except Exception as e:
        raise BuildError(f"فشل البناء: {e}", e)