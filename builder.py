"""builder.py — يبني docs/ مع الصور والتصنيفات."""

import json
import shutil
from pathlib import Path

from config import config, DOCS_DIR, ASSETS_DIR, DATA_DIR
from database import db
from errors import BuildError

POSTERS_FILE = DATA_DIR / "posters.json"


def _ensure_structure():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


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


def _load_poster_map():
    """يحمّل map: series_name → poster_url."""
    if not POSTERS_FILE.exists():
        return {}
    try:
        posters = json.loads(POSTERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result = {}
    for k, v in posters.items():
        if k.startswith("__series__"):
            result[k.replace("__series__", "", 1)] = v
    return result


def _write_videos_json():
    videos = db.all_videos()
    (DOCS_DIR / "videos.json").write_text(
        json.dumps(videos, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(videos)


def _series_to_dict(name, poster_map):
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
            "duration": v.get("duration", 0),
            "url": v.get("url", ""),
            "title": v.get("title", f"الحلقة {k}"),
        })

    # ★ poster من map
    poster = poster_map.get(name, "")

    return {
        "name": name,
        "url": s.get("url", ""),
        "slug": s.get("slug", name.replace(" ", "_")),
        "poster": poster,
        "type": s.get("type", "series"),
        "source": s.get("source", ""),
        "season": s.get("season", 0),
        "episodes": uploaded,
        "count": len(uploaded),
        "last_updated": s.get("last_updated"),
    }


def _write_series_index():
    poster_map = _load_poster_map()
    all_series = db.all_series()
    index = [_series_to_dict(n, poster_map) for n in all_series]
    index = [s for s in index if s["count"] > 0]
    index.sort(key=lambda s: s["name"])
    (DOCS_DIR / "series.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(index)


def build_incremental(changed_series=None):
    try:
        _ensure_structure()
        first_time = not (DOCS_DIR / "index.html").exists()
        _copy_assets(force=first_time)
        _write_config()

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


if __name__ == "__main__":
    build_incremental()