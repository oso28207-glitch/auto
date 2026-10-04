"""قاعدة بيانات JSON مع دمج عميق وكتابة ذرّية."""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

STATE_FILE = DATA_DIR / "state.json"
BACKUP_FILE = DATA_DIR / "state.backup.json"

DEFAULT = {"version": 2, "series": {}, "videos": [], "last_run": None}


class Database:
    def __init__(self, path=None):
        self.path = Path(path) if path else STATE_FILE
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self):
        for c in (self.path, BACKUP_FILE):
            if not c.exists():
                continue
            try:
                d = json.loads(c.read_text(encoding="utf-8"))
                for k, v in DEFAULT.items():
                    d.setdefault(k, v)
                print(f"💾 تم تحميل الحالة ({len(d.get('series', {}))} مسلسل)")
                return d
            except Exception as e:
                print(f"⚠️ فشل تحميل {c.name}: {e}")
        return dict(DEFAULT)

    def _save(self):
        with self._lock:
            self._data["last_run"] = datetime.now(timezone.utc).isoformat()
            tmp_fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if self.path.exists():
                    shutil.copy2(self.path, BACKUP_FILE)
                os.replace(tmp, self.path)
            except Exception:
                try: os.unlink(tmp)
                except Exception: pass
                raise

    # ─── Series ───
    def get_series(self, name):
        with self._lock:
            return dict(self._data["series"].get(name, {}))

    def set_series(self, name, value):
        with self._lock:
            old = self._data["series"].get(name, {})
            merged = {**old, **value}
            old_eps = old.get("episodes", {}) or {}
            new_eps = value.get("episodes", {}) or {}
            if old_eps or new_eps:
                merged["episodes"] = {**old_eps, **new_eps}
            self._data["series"][name] = merged
            self._save()

    def all_series(self):
        with self._lock:
            return list(self._data["series"].keys())

    # ─── Episodes ───
    def episode_status(self, series, ep):
        with self._lock:
            s = self._data["series"].get(series, {})
            ep_data = s.get("episodes", {}).get(str(ep), {})
            return ep_data.get("status")

    def is_uploaded(self, series, ep):
        return self.episode_status(series, ep) == "uploaded"

    def set_episode(self, series, ep, **kw):
        with self._lock:
            s = self._data["series"].setdefault(series, {"url": "", "episodes": {}})
            s.setdefault("episodes", {})
            e = s["episodes"].setdefault(str(ep), {})
            e.update(kw)
            e["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save()

    def mark_failed(self, series, ep, reason=""):
        self.set_episode(series, ep, status="failed", error=reason[:500])

    def uploaded_episodes(self, series):
        with self._lock:
            s = self._data["series"].get(series, {})
            return sorted(
                int(k) for k, v in s.get("episodes", {}).items()
                if v.get("status") == "uploaded"
            )

    # ─── Videos ───
    def add_video(self, video):
        if not video or "id" not in video:
            return
        with self._lock:
            ids = {v.get("id") for v in self._data["videos"]}
            if video["id"] in ids:
                return
            self._data["videos"].append(video)
            self._data["videos"].sort(
                key=lambda v: v.get("date", ""), reverse=True
            )
            self._save()

    def all_videos(self):
        with self._lock:
            return list(self._data["videos"])

    # ─── Stats ───
    def stats(self):
        with self._lock:
            total_eps = sum(
                len(s.get("episodes", {})) for s in self._data["series"].values()
            )
            uploaded = sum(
                sum(1 for e in s.get("episodes", {}).values() if e.get("status") == "uploaded")
                for s in self._data["series"].values()
            )
            return {
                "series": len(self._data["series"]),
                "episodes": total_eps,
                "uploaded": uploaded,
                "videos": len(self._data["videos"]),
                "last_run": self._data.get("last_run"),
            }


db = Database()