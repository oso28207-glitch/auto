"""builder.py — بناء تدريجي: يحدّث فقط ما تغيّر."""

import json
import shutil
from pathlib import Path

from config import config, DOCS_DIR, ASSETS_DIR
from database import db
from errors import BuildError


def _ensure_structure():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "series").mkdir(parents=True, exist_ok=True)


def _copy_assets(force=False):
    if not ASSETS_DIR.exists():
        return
    for f in ASSETS_DIR.iterdir():
        if f.is_file():
            dest = DOCS_DIR / f.name
            if force or not dest.exists():
                shutil.copy2(f, dest)


def _write_config():
    cfg = f'window.APP_CONFIG = {{ API_BASE: "{config.API_BASE}" }};\n'
    (DOCS_DIR / "config.js").write_text(cfg, encoding="utf-8")


def _write_videos_json():
    videos = db.all_videos()
    (DOCS_DIR / "videos.json").write_text(
        json.dumps(videos, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(videos)


def _series_to_dict(name):
    """يحوّل بيانات مسلسل إلى قاموس JSON مع file_id و size."""
    s = db.get_series(name)
    eps = s.get("episodes", {})
    uploaded = []

    for k, v in sorted(eps.items(), key=lambda x: int(x[0])):
        if v.get("status") != "uploaded":
            continue
        if not v.get("message_id") or not v.get("file_id"):
            continue

        uploaded.append({
            "episode": int(k),
            "message_id": v["message_id"],
            "file_id": v.get("file_id", ""),
            "size": v.get("size", 0),
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
    all_series = db.all_series()
    index = [_series_to_dict(n) for n in all_series]
    index = [s for s in index if s["count"] > 0]  # تجاهل المسلسلات الفارغة
    index.sort(key=lambda s: s["name"])
    (DOCS_DIR / "series.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(index)


def build_incremental(changed_series=None):
    """
    بناء تدريجي:
      - ينسخ الأصول مرة واحدة.
      - يحدّث series.json و videos.json دائماً.
    """
    try:
        _ensure_structure()

        # الأصول — مرة واحدة فقط
        first_time = not (DOCS_DIR / "index.html").exists()
        _copy_assets(force=first_time)
        _write_config()

        # البيانات
        n_videos = _write_videos_json()
        n_series = _write_series_index()

        msg = f"[Builder] ✅ {n_series} مسلسل، {n_videos} فيديو"
        if changed_series:
            preview = ", ".join(changed_series[:3])
            if len(changed_series) > 3:
                preview += "..."
            msg += f" (محدّث: {preview})"
        print(msg)

    except Exception as e:
        raise BuildError(f"فشل البناء: {e}", e)