import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DOCS_DIR = BASE_DIR / "docs"

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

    EGYBEST_BASE_URL = os.environ.get("EGYBEST_BASE_URL", "https://egybesstt.baby").rstrip("/")
    EGYBEST_SERIES_PATH = os.environ.get("EGYBEST_SERIES_PATH", "/all-series.php")

    MAX_EPISODES_PER_RUN = int(os.environ.get("MAX_EPISODES_PER_RUN", "0"))
    MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "9900"))
    SKIP_COMPRESS = os.environ.get("SKIP_COMPRESS", "false").lower() == "true"
    KEEP_MEDIA = os.environ.get("KEEP_MEDIA", "false").lower() == "true"

    # إعدادات الضغط الثابتة
    COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")
    COMPRESS_CRF = int(os.environ.get("COMPRESS_CRF", "28"))
    COMPRESS_THREADS = int(os.environ.get("COMPRESS_THREADS", "2"))
    COMPRESS_SCALE = int(os.environ.get("COMPRESS_SCALE", "144"))
    COMPRESS_AUDIO_BITRATE = os.environ.get("COMPRESS_AUDIO_BITRATE", "32k")
    COMPRESS_MAX_SIZE_MB = int(os.environ.get("COMPRESS_MAX_SIZE_MB", "100"))

    M3U8_SEARCH_TIMEOUT = int(os.environ.get("M3U8_SEARCH_TIMEOUT", "25"))
    OPEN_TIMEOUT = int(os.environ.get("OPEN_TIMEOUT", "10"))
    EPISODE_TIMEOUT = int(os.environ.get("EPISODE_TIMEOUT", "2400"))

    @classmethod
    def priority_of(cls, name: str) -> int:
        """
        1 = مسلسلات تركية مدبلجة (الأولوية القصوى)
        2 = مسلسلات مدبلجة أخرى
        3 = أفلام مدبلجة
        4 = مسلسلات عادية
        5 = أفلام عادية
        """
        n = (name or "").strip()
        is_dubbed = ("مدبلج" in n)
        is_turkish = ("تركي" in n) or ("تركية" in n) or ("turkish" in n.lower())
        is_movie = (
            n.startswith("فيلم") or n.startswith("افلام") or
            n.startswith("أفلام") or "افلام" in n[:10] or "أفلام" in n[:10]
        )
        if is_turkish and is_dubbed and not is_movie:
            return 1
        if is_dubbed and not is_movie:
            return 2
        if is_dubbed and is_movie:
            return 3
        if not is_movie:
            return 4
        return 5

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