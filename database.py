"""
قاعدة بيانات خفيفة (JSON) لتتبع حالة كل حلقة.
تضمن عدم إعادة تحميل أو رفع ما تم سابقاً.
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import DATA_DIR


class Database:
    def __init__(self, path: Path = None):
        self.path = path or DATA_DIR / "state.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {"series": {}, "videos": []}
        return {"series": {}, "videos": []}

    def _save(self):
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_series(self, name: str) -> dict:
        with self._lock:
            return self._data["series"].get(name, {})

    def set_series(self, name: str, value: dict):
        with self._lock:
            self._data["series"][name] = value
            self._save()

    def episode_status(self, series: str, episode: int) -> Optional[str]:
        s = self.get_series(series)
        return s.get("episodes", {}).get(str(episode), {}).get("status")

    def set_episode(self, series: str, episode: int, **kwargs):
        with self._lock:
            s = self._data["series"].setdefault(series, {"episodes": {}})
            ep = s["episodes"].setdefault(str(episode), {})
            ep.update(kwargs)
            ep["updated_at"] = datetime.utcnow().isoformat()
            self._save()

    def add_video(self, video: dict):
        with self._lock:
            ids = {v["id"] for v in self._data["videos"]}
            if video["id"] not in ids:
                self._data["videos"].append(video)
                self._save()

    def all_videos(self) -> list:
        return list(self._data["videos"])

    def latest_episode(self, series: str) -> int:
        s = self.get_series(series)
        eps = s.get("episodes", {})
        if not eps:
            return 0
        return max(int(k) for k in eps.keys())

    def all_series(self) -> list:
        return list(self._data["series"].keys())


db = Database()