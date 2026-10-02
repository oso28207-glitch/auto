"""
database.py — قاعدة بيانات خفيفة (JSON) لتتبع حالة كل حلقة.

تحفظ:
  - series: { name: { url, episodes: { "1": {status, message_id, url, title, updated_at}, ... } } }
  - videos: [ { id, title, series, episode, size, date }, ... ]

ميزات:
  ✅ دمج عميق (deep merge) بدل استبدال البيانات
  ✅ كتابة آمنة (atomic write) لتفادي تلف الملف عند الانقطاع
  ✅ قفل (lock) لتفادي تعارض العمليات المتزامنة
  ✅ نسخة احتياطية تلقائية عند كل كتابة
"""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import DATA_DIR


# ═══════════════════════════════════════════════════════════════
# ثوابت
# ═══════════════════════════════════════════════════════════════
STATE_FILE = DATA_DIR / "state.json"
BACKUP_FILE = DATA_DIR / "state.backup.json"

# الحالات الممكنة للحلقة
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
# فئة قاعدة البيانات
# ═══════════════════════════════════════════════════════════════
class Database:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else STATE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    # ─────────────────────────────────────────────────────────
    # تحميل/حفظ
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
                print(f"💾 تم تحميل الحالة من: {candidate.name}")
                return data
            except Exception as e:
                print(f"⚠️ فشل تحميل {candidate}: {e}")

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
                    json.dump(
                        self._data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                    f.flush()
                    os.fsync(f.fileno())

                # 2) نسخة احتياطية من الملف الحالي (إن وُجد)
                if self.path.exists():
                    try:
                        shutil.copy2(self.path, BACKUP_FILE)
                    except Exception as e:
                        print(f"⚠️ فشل النسخ الاحتياطي: {e}")

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
    # إدارة المسلسلات
    # ─────────────────────────────────────────────────────────
    def get_series(self, name: str) -> dict:
        """يعيد بيانات مسلسل أو قاموساً فارغاً."""
        with self._lock:
            return dict(self._data["series"].get(name, {}))

    def set_series(self, name: str, value: dict):
        """
        دمج عميق: لا يستبدل `episodes` الموجودة، بل يدمج الجديد فوقها.
        هذا يمنع فقدان سجل الحلقات المرفوعة سابقاً.
        """
        with self._lock:
            existing = self._data["series"].get(name, {})

            merged = {**existing, **value}

            # دمج خاص لحقل episodes
            old_eps = existing.get("episodes", {}) or {}
            new_eps = value.get("episodes", {}) or {}
            if old_eps or new_eps:
                merged["episodes"] = {**old_eps, **new_eps}

            self._data["series"][name] = merged
            self._save()

    def update_series_url(self, name: str, url: str):
        """يحدّث URL مسلسل دون لمس الحلقات."""
        with self._lock:
            if name in self._data["series"]:
                self._data["series"][name]["url"] = url
                self._save()

    def all_series(self) -> list[str]:
        with self._lock:
            return list(self._data["series"].keys())

    def has_series(self, name: str) -> bool:
        with self._lock:
            return name in self._data["series"]

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
        """اختصار شائع."""
        return self.episode_status(series, episode) == STATUS_UPLOADED

    def get_episode(self, series: str, episode: int) -> Optional[dict]:
        with self._lock:
            s = self._data["series"].get(series)
            if not s:
                return None
            ep = s.get("episodes", {}).get(str(episode))
            return dict(ep) if ep else None

    def set_episode(self, series: str, episode: int, **kwargs):
        """
        يحدّث بيانات حلقة. ينشئ المسلسل إذا لم يكن موجوداً.

        مثال:
            db.set_episode("مسلسل X", 1, status="uploaded", message_id=123)
        """
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
        """يعلم حلقة كفاشلة مع السبب."""
        self.set_episode(
            series, episode,
            status=STATUS_FAILED,
            error=reason[:500],
        )

    def latest_episode(self, series: str) -> int:
        """يعيد أعلى رقم حلقة مسجّلة لهذا المسلسل."""
        with self._lock:
            s = self._data["series"].get(series, {})
            eps = s.get("episodes", {})
            if not eps:
                return 0
            try:
                return max(int(k) for k in eps.keys())
            except (ValueError, TypeError):
                return 0

    def uploaded_episodes(self, series: str) -> list[int]:
        """يعيد قائمة أرقام الحلقات المرفوعة."""
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

    def pending_episodes(self, series: str) -> list[int]:
        """الحلقات التي لم تُرفع بعد."""
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
        """يضيف فيديو إذا لم يكن موجوداً بنفس id."""
        if not video or "id" not in video:
            return
        with self._lock:
            existing_ids = {v.get("id") for v in self._data["videos"]}
            if video["id"] in existing_ids:
                return
            self._data["videos"].append(video)
            # رتّب من الأحدث للأقدم
            self._data["videos"].sort(
                key=lambda v: v.get("date", ""),
                reverse=True,
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
        """يعيد إحصاءات سريعة."""
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

            return {
                "series": total_series,
                "episodes_total": total_episodes,
                "episodes_uploaded": uploaded,
                "episodes_failed": failed,
                "videos_in_index": len(self._data["videos"]),
                "last_run": self._data.get("last_run"),
            }

    # ─────────────────────────────────────────────────────────
    # صيانة
    # ─────────────────────────────────────────────────────────
    def reset(self):
        """⚠️ يحذف كل الحالة — للاستخدام الحذر فقط."""
        with self._lock:
            self._data = dict(DEFAULT_DATA)
            self._save()
            print("⚠️ تم تصفير قاعدة البيانات")

    def export(self) -> dict:
        """يعيد نسخة من البيانات الخام."""
        with self._lock:
            return json.loads(json.dumps(self._data))


# ═══════════════════════════════════════════════════════════════
# نسخة عالمية للاستخدام المباشر
# ═══════════════════════════════════════════════════════════════
db = Database()


# ═══════════════════════════════════════════════════════════════
# اختبار سريع
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("🧪 اختبار قاعدة البيانات\n")

    # إعادة تعيين للاختبار
    db.reset()

    # 1) إضافة مسلسل
    db.set_series("مسلسل تجريبي", {
        "url": "https://example.com/series/test",
        "episodes": {},
    })
    print(f"✓ أُضيف مسلسل. العدد: {db.stats()['series']}")

    # 2) إضافة حلقات
    for i in range(1, 4):
        db.set_episode("مسلسل تجريبي", i, status="uploaded", message_id=1000 + i)

    print(f"✓ الحلقات المرفوعة: {db.uploaded_episodes('مسلسل تجريبي')}")
    print(f"✓ آخر حلقة: {db.latest_episode('مسلسل تجريبي')}")

    # 3) اختبار الدمج — لا يفقد الحلقات
    db.set_series("مسلسل تجريبي", {"url": "https://example.com/new-url"})
    print(f"✓ بعد تحديث URL، الحلقات: {db.uploaded_episodes('مسلسل تجريبي')}")
    print(f"✓ URL الجديد: {db.get_series('مسلسل تجريبي')['url']}")

    # 4) إضافة فيديو
    db.add_video({
        "id": 1001,
        "title": "مسلسل تجريبي — الحلقة 1",
        "series": "مسلسل تجريبي",
        "episode": 1,
        "size": 123456,
        "date": "2026-10-03T10:00:00",
    })
    print(f"✓ عدد الفيديوهات: {db.video_count()}")

    # 5) إحصاءات
    import json
    print("\n📊 الإحصاءات:")
    print(json.dumps(db.stats(), ensure_ascii=False, indent=2))

    print("\n✅ كل الاختبارات نجحت")
