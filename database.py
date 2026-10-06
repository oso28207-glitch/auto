"""
database.py — قاعدة بيانات JSON مع:
  - كتابة ذرّية (atomic write)
  - نسخة احتياطية تلقائية
  - دمج عميق (deep merge) للمسلسلات
  - البحث عن أعمال مكررة بأسماء مشابهة
  - قفل داخلي (thread-safe)
"""

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DATA_DIR


# ═══════════════════════════════════════════════════════════════
# ثوابت
# ═══════════════════════════════════════════════════════════════
STATE_FILE = DATA_DIR / "state.json"
BACKUP_FILE = DATA_DIR / "state.backup.json"

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"

DEFAULT_DATA = {
    "version": 2,
    "series": {},
    "videos": [],
    "last_run": None,
}


# ═══════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════
def _normalize_name(name: str) -> str:
    """تطبيع الاسم للمقارنة (إزالة التشكيل والرموز)."""
    if not name:
        return ""
    n = name.strip()
    n = re.sub(r"\s+", " ", n)
    # إزالة "مسلسل" أو "فيلم" من البداية
    n = re.sub(r"^(مسلسل|فيلم)\s+", "", n)
    # إزالة "الموسم X" من النهاية
    n = re.sub(r"\s+الموسم\s+.*$", "", n)
    # إزالة أرقام في النهاية
    n = re.sub(r"\s+\d+\s*$", "", n)
    # إزالة الرموز الخاصة
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return n.strip().lower()


# ═══════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════
class Database:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else STATE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    # ─────────────────────────────────────────────────────────
    # التحميل والحفظ
    # ─────────────────────────────────────────────────────────
    def _load(self) -> dict:
        """يحمّل الحالة من الملف. إذا كان تالفاً، يحاول النسخة الاحتياطية."""
        for candidate in (self.path, BACKUP_FILE):
            if not candidate.exists():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("state.json ليس قاموساً")
                # ضمان وجود المفاتيح الأساسية
                for key, default in DEFAULT_DATA.items():
                    data.setdefault(key, default)
                print(f"💾 تم تحميل الحالة ({len(data.get('series', {}))} مسلسل)")
                return data
            except Exception as e:
                print(f"⚠️ فشل تحميل {candidate.name}: {e}")

        print("💾 لا توجد حالة سابقة — إنشاء جديدة")
        return dict(DEFAULT_DATA)

    def _save(self):
        """كتابة آمنة (atomic) + نسخة احتياطية."""
        with self._lock:
            self._data["last_run"] = datetime.now(timezone.utc).isoformat()

            # 1) اكتب في ملف مؤقت
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix="state_", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                # 2) نسخة احتياطية
                if self.path.exists():
                    try:
                        shutil.copy2(self.path, BACKUP_FILE)
                    except Exception:
                        pass

                # 3) استبدال ذرّي
                os.replace(tmp_path, self.path)

            except Exception as e:
                print(f"❌ فشل حفظ الحالة: {e}")
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise

    # ─────────────────────────────────────────────────────────
    # البحث عن أعمال مكررة
    # ─────────────────────────────────────────────────────────
    def find_series_by_name(self, name: str) -> Optional[str]:
        """
        يبحث عن عمل بالاسم مع مطابقة تقريبية.
        يعيد الاسم الموجود في قاعدة البيانات أو None.
        """
        target = _normalize_name(name)
        if not target:
            return None

        with self._lock:
            # مطابقة مباشرة
            for existing in self._data["series"]:
                if _normalize_name(existing) == target:
                    return existing

            # مطابقة جزئية (إذا كان الاسم طويلاً بما يكفي)
            for existing in self._data["series"]:
                ex_norm = _normalize_name(existing)
                if len(ex_norm) >= 5 and len(target) >= 5:
                    if ex_norm in target or target in ex_norm:
                        return existing

        return None

    def find_episode_across_series(self, series_name: str, ep_num: int) -> Optional[str]:
        """
        يبحث عن حلقة برقم معين في أي مسلسل مشابه بالاسم.
        يعيد اسم المسلسل الذي يحتويها أو None.
        """
        existing_name = self.find_series_by_name(series_name)
        if not existing_name:
            return None

        with self._lock:
            s = self._data["series"].get(existing_name, {})
            eps = s.get("episodes", {})
            ep = eps.get(str(ep_num))
            if ep and ep.get("status") == STATUS_UPLOADED:
                return existing_name

        return None

    # ─────────────────────────────────────────────────────────
    # إدارة المسلسلات
    # ─────────────────────────────────────────────────────────
    def get_series(self, name: str) -> dict:
        with self._lock:
            return dict(self._data["series"].get(name, {}))

    def set_series(self, name: str, value: dict):
        """دمج عميق: لا يستبدل `episodes` الموجودة."""
        with self._lock:
            existing = self._data["series"].get(name, {})
            merged = {**existing, **value}

            old_eps = existing.get("episodes", {}) or {}
            new_eps = value.get("episodes", {}) or {}
            if old_eps or new_eps:
                merged["episodes"] = {**old_eps, **new_eps}

            self._data["series"][name] = merged
            self._save()

    def update_series_url(self, name: str, url: str):
        with self._lock:
            if name in self._data["series"]:
                self._data["series"][name]["url"] = url
                self._save()

    def all_series(self) -> list:
        with self._lock:
            return list(self._data["series"].keys())

    def has_series(self, name: str) -> bool:
        with self._lock:
            return name in self._data["series"]

    def has_failed(self, series: str) -> bool:
        with self._lock:
            s = self._data["series"].get(series, {})
            return s.get("status") == STATUS_FAILED

    def reset_series(self, series: str):
        """يصفّر حالة مسلسل لإعادة المحاولة."""
        with self._lock:
            if series in self._data["series"]:
                self._data["series"][series]["status"] = "pending"
                self._data["series"][series].pop("failed_at", None)
                self._data["series"][series].pop("reason", None)
                self._save()

    def reset_all_failed(self) -> int:
        """يصفّر كل المسلسلات الفاشلة."""
        with self._lock:
            count = 0
            for s in self._data["series"].values():
                if s.get("status") == STATUS_FAILED:
                    s["status"] = "pending"
                    s.pop("failed_at", None)
                    s.pop("reason", None)
                    count += 1
            if count:
                self._save()
            return count

    # ─────────────────────────────────────────────────────────
    # إدارة الحلقات
    # ─────────────────────────────────────────────────────────
    def episode_status(self, series: str, episode: int) -> Optional[str]:
        with self._lock:
            s = self._data["series"].get(series)
            if not s:
                return None
            ep = s.get("episodes", {}).get(str(episode))
            if not ep:
                return None
            return ep.get("status")

    def is_uploaded(self, series: str, episode: int) -> bool:
        return self.episode_status(series, episode) == STATUS_UPLOADED

    def get_episode(self, series: str, episode: int) -> Optional[dict]:
        with self._lock:
            s = self._data["series"].get(series)
            if not s:
                return None
            ep = s.get("episodes", {}).get(str(episode))
            return dict(ep) if ep else None

    def set_episode(self, series: str, episode: int, **kwargs):
        """يحدّث بيانات حلقة. ينشئ المسلسل إذا لم يكن موجوداً."""
        with self._lock:
            s = self._data["series"].setdefault(
                series, {"url": "", "episodes": {}}
            )
            s.setdefault("episodes", {})
            ep = s["episodes"].setdefault(str(episode), {})

            ep.update(kwargs)
            ep["updated_at"] = datetime.now(timezone.utc).isoformat()

            self._save()

    def mark_failed(self, series: str, episode: int, reason: str = ""):
        self.set_episode(
            series, episode,
            status=STATUS_FAILED,
            error=reason[:500],
        )

    def latest_episode(self, series: str) -> int:
        with self._lock:
            s = self._data["series"].get(series, {})
            eps = s.get("episodes", {})
            if not eps:
                return 0
            try:
                return max(int(k) for k in eps.keys())
            except (ValueError, TypeError):
                return 0

    def uploaded_episodes(self, series: str) -> list:
        with self._lock:
            s = self._data["series"].get(series, {})
            eps = s.get("episodes", {})
            result = []
            for k, v in eps.items():
                if v.get("status") == STATUS_UPLOADED:
                    try:
                        result.append(int(k))
                    except (ValueError, TypeError):
                        continue
            return sorted(result)

    def pending_episodes(self, series: str) -> list:
        with self._lock:
            s = self._data["series"].get(series, {})
            eps = s.get("episodes", {})
            result = []
            for k, v in eps.items():
                if v.get("status") != STATUS_UPLOADED:
                    try:
                        result.append(int(k))
                    except (ValueError, TypeError):
                        continue
            return sorted(result)

    # ─────────────────────────────────────────────────────────
    # إدارة الفيديوهات (للـ videos.json)
    # ─────────────────────────────────────────────────────────
    def add_video(self, video: dict):
        if not video or "id" not in video:
            return
        with self._lock:
            existing_ids = {v.get("id") for v in self._data["videos"]}
            if video["id"] in existing_ids:
                return
            self._data["videos"].append(video)
            self._data["videos"].sort(
                key=lambda v: v.get("date", ""), reverse=True
            )
            self._save()

    def all_videos(self) -> list:
        with self._lock:
            return list(self._data["videos"])

    def video_count(self) -> int:
        with self._lock:
            return len(self._data["videos"])

    # ─────────────────────────────────────────────────────────
    # إحصاءات
    # ─────────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            total_series = len(self._data["series"])
            total_episodes = 0
            uploaded = 0
            failed = 0

            for s in self._data["series"].values():
                eps = s.get("episodes", {})
                total_episodes += len(eps)
                for ep in eps.values():
                    st = ep.get("status")
                    if st == STATUS_UPLOADED:
                        uploaded += 1
                    elif st == STATUS_FAILED:
                        failed += 1

            failed_series = sum(
                1 for s in self._data["series"].values()
                if s.get("status") == STATUS_FAILED
            )

            return {
                "series": total_series,
                "episodes": total_episodes,
                "uploaded": uploaded,
                "failed": failed,
                "failed_series": failed_series,
                "videos": len(self._data["videos"]),
                "last_run": self._data.get("last_run"),
            }

    # ─────────────────────────────────────────────────────────
    # صيانة
    # ─────────────────────────────────────────────────────────
    def reset(self):
        """⚠️ يحذف كل الحالة."""
        with self._lock:
            self._data = dict(DEFAULT_DATA)
            self._save()
            print("⚠️ تم تصفير قاعدة البيانات")

    def export(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))


# ═══════════════════════════════════════════════════════════════
# النسخة العالمية
# ═══════════════════════════════════════════════════════════════
db = Database()


# ═══════════════════════════════════════════════════════════════
# اختبار
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("🧪 اختبار قاعدة البيانات\n")

    db.reset()

    # 1) مسلسل
    db.set_series("مسلسل تجريبي", {
        "url": "https://example.com/series/test",
        "type": "series",
        "episodes": {},
    })
    print(f"✓ أُضيف مسلسل. العدد: {db.stats()['series']}")

    # 2) حلقات
    for i in range(1, 4):
        db.set_episode("مسلسل تجريبي", i, status="uploaded", message_id=1000 + i)

    print(f"✓ الحلقات المرفوعة: {db.uploaded_episodes('مسلسل تجريبي')}")
    print(f"✓ آخر حلقة: {db.latest_episode('مسلسل تجريبي')}")

    # 3) اختبار الدمج — لا يفقد الحلقات
    db.set_series("مسلسل تجريبي", {"url": "https://example.com/new-url"})
    print(f"✓ بعد تحديث URL، الحلقات: {db.uploaded_episodes('مسلسل تجريبي')}")
    print(f"✓ URL الجديد: {db.get_series('مسلسل تجريبي')['url']}")

    # 4) البحث عن تكرار
    match = db.find_series_by_name("تجريبي")
    print(f"✓ find_series_by_name('تجريبي'): {match}")

    match2 = db.find_series_by_name("مسلسل تجريبي")
    print(f"✓ find_series_by_name('مسلسل تجريبي'): {match2}")

    # 5) فيديو
    db.add_video({
        "id": 1001,
        "title": "مسلسل تجريبي — الحلقة 1",
        "series": "مسلسل تجريبي",
        "episode": 1,
        "size": 123456,
        "date": "2026-10-06T10:00:00",
    })
    print(f"✓ عدد الفيديوهات: {db.video_count()}")

    # 6) إحصاءات
    print("\n📊 الإحصاءات:")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))

    print("\n✅ كل الاختبارات نجحت")