import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DOCS_DIR = BASE_DIR / "docs"
ASSETS_DIR = BASE_DIR / "assets"

for d in (DATA_DIR, MEDIA_DIR, DOCS_DIR, DOCS_DIR / "posters", DOCS_DIR / "watch"):
    d.mkdir(parents=True, exist_ok=True)


class Config:
    # Telegram
    API_ID = int(os.environ.get("API_ID", "0") or "0")
    API_HASH = os.environ.get("API_HASH", "").strip()
    SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
    CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()

    CHECK_CHANNELS = os.environ.get(
        "CHECK_CHANNELS", "shoofcima,shoofFilm"
    ).strip()
    CHECK_CHANNEL_LIMIT = int(os.environ.get("CHECK_CHANNEL_LIMIT", "3000"))
    CHECK_CACHE_TTL = int(os.environ.get("CHECK_CACHE_TTL", "3600"))

    ENABLED_SOURCES = os.environ.get("ENABLED_SOURCES", "u3seq,yam").strip()

    U3SEQ_BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.cam").rstrip("/")
    U3SEQ_CATEGORY = int(os.environ.get("SOURCE_CATEGORY", "0"))
    U3SEQ_MAX_PAGES = int(os.environ.get("SOURCE_MAX_PAGES", "20"))
    SOURCE_BASE_URL = U3SEQ_BASE_URL
    SOURCE_CATEGORY = U3SEQ_CATEGORY
    SOURCE_MAX_PAGES = U3SEQ_MAX_PAGES

    YAM_BASE_URL = os.environ.get("YAM_BASE_URL", "https://yam.ahwaktv.net").rstrip("/")
    YAM_SERIES_PATH = os.environ.get("YAM_SERIES_PATH", "/moslslat.php")
    YAM_MAX_PAGES = int(os.environ.get("YAM_MAX_PAGES", "95"))
    YAM_CATEGORY_PAGES = int(os.environ.get("YAM_CATEGORY_PAGES", "40"))
    YAM_SERIES_FETCH_LIMIT = int(os.environ.get("YAM_SERIES_FETCH_LIMIT", "900"))
    YAM_WORKERS = int(os.environ.get("YAM_WORKERS", "10"))
    # تصنيفات المصدر (yam) — تُغطّي كل الأقسام المطلوبة:
    #   مدبلج تركي / مدبلج هندي / مدبلج / أنمي / كوري / مترجم تركي /
    #   عربي (خليجي/مصري) / أجنبي / آسيوي / أفلام (عربي/تركي/هندي/مدبلج)
    YAM_CATEGORIES = os.environ.get(
        "YAM_CATEGORIES",
        ",".join([
            # ── مدبلج تركي ──
            "moslslat-turkiaa-modblga",
            # ── مدبلج هندي ──
            "moslslat-hndia-modblja",
            "indian-series",
            # ── مدبلج (عام) ──
            "moslslat-modblga",
            "goda-akbar-modblge",
            "mn-elnazra-elthania-modblge",
            # ── أنمي / كرتون ──
            "moslslat-cartoon",
            # ── مترجم (تركي) ──
            "moslslat-turkiaa-motrgma",
            "moslslat-turkiaa1",
            # ── عربي (خليجي / مصري) ──
            "1moslslat-arbyaa",
            "moslslat-msriya",
            "moslslat-",
            # ── أجنبي ──
            "mosalslat-english",
            # ── آسيوي ──
            "moslslat-asia",
            # ── أفلام ──
            "aflam-dub",
            "aflam-arabya1",
            "aflam-turky",
            "aflam-hindi",
        ]),
    ).strip()

    EGYBEST_BASE_URL = os.environ.get("EGYBEST_BASE_URL", "https://egybesstt.baby").rstrip("/")
    EGYBEST_SERIES_PATH = os.environ.get("EGYBEST_SERIES_PATH", "/all-series.php")

    MAX_EPISODES_PER_RUN = int(os.environ.get("MAX_EPISODES_PER_RUN", "0"))
    MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "9900"))
    SKIP_COMPRESS = os.environ.get("SKIP_COMPRESS", "false").lower() == "true"
    KEEP_MEDIA = os.environ.get("KEEP_MEDIA", "false").lower() == "true"

    # ── خصائص السكربتات القديمة (run.py / orchestrator.py / builder.py) ──
    API_BASE = os.environ.get(
        "API_BASE", "https://auto-production-08b0.up.railway.app"
    ).rstrip("/")
    AUTO_BUILD = os.environ.get("AUTO_BUILD", "true").lower() == "true"
    CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))
    REALTIME_PUSH = os.environ.get("REALTIME_PUSH", "false").lower() == "true"
    REALTIME_PUSH_INTERVAL = int(os.environ.get("REALTIME_PUSH_INTERVAL", "600"))

    # إعدادات الضغط الثابتة
    COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")
    COMPRESS_CRF = int(os.environ.get("COMPRESS_CRF", "28"))
    COMPRESS_THREADS = int(os.environ.get("COMPRESS_THREADS", "2"))
    COMPRESS_SCALE = int(os.environ.get("COMPRESS_SCALE", "240"))
    COMPRESS_AUDIO_BITRATE = os.environ.get("COMPRESS_AUDIO_BITRATE", "32k")
    COMPRESS_MAX_SIZE_MB = int(os.environ.get("COMPRESS_MAX_SIZE_MB", "100"))

    M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "25"))
    OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "10"))
    EPISODE_TIMEOUT = int(os.environ.get("EPISODE_TIMEOUT", "2400"))

    @classmethod
    def priority_of(cls, name: str) -> int:
        """
        أولوية العرض/الترتيب (1 = الأعلى) مبنية على التصنيف الموحّد.
        ترتيب الأقسام:
          1 مسلسلات تركية مدبلجة | 2 مسلسلات هندية مدبلجة | 3 مسلسلات أنمي مدبلجة
          4 مسلسلات كورية مدبلجة | 5 مسلسلات مدبلجة | 6 مسلسلات مترجمة
          7 مسلسلات عربية | 8 مسلسلات أجنبية | 9 مسلسلات أسيوية
          10 أفلام مدبلجة | 11 أفلام | 12 مسلسلات وأفلام
        """
        try:
            from names import canonical_category, CATEGORY_PRIORITY
            return CATEGORY_PRIORITY.get(canonical_category(name=name), 12)
        except Exception:
            return 12

    @classmethod
    def priority_of_series(cls, series) -> int:
        """
        أولوية حسب الاسم + التصنيف (category) + النوع (genre) عبر التصنيف الموحّد.
        ★ يضمن أن المسلسلات التركية المدبلجة تأتي أولاً حتى لو لم يحتوِ اسمها كلمة
          "تركي"، بالاعتماد على تصنيف المصدر مثل: moslslat-turkiaa-modblga.
        """
        if isinstance(series, dict):
            name = series.get("name") or series.get("display_name") or ""
            category = series.get("category") or ""
            source = series.get("source") or ""
        else:
            name, category, source = str(series or ""), "", ""
        try:
            from names import canonical_category, CATEGORY_PRIORITY
            cat = canonical_category(name=name, category=category, source=source)
            return CATEGORY_PRIORITY.get(cat, 12)
        except Exception:
            return 12

    @classmethod
    def category_of_series(cls, series) -> str:
        """يُرجِع اسم التصنيف الموحّد لسلسلة (dict)."""
        if isinstance(series, dict):
            name = series.get("name") or series.get("display_name") or ""
            category = series.get("category") or ""
            source = series.get("source") or ""
        else:
            name, category, source = str(series or ""), "", ""
        try:
            from names import canonical_category
            return canonical_category(name=name, category=category, source=source)
        except Exception:
            return "مسلسلات وأفلام"

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID: missing.append("API_ID")
        if not cls.API_HASH: missing.append("API_HASH")
        if not cls.SESSION_STRING: missing.append("SESSION_STRING")
        if not cls.CHANNEL_ID: missing.append("CHANNEL_ID")
        if missing:
            raise Exception(f"متغيرات مفقودة: {', '.join(missing)}")
        return True


config = Config()