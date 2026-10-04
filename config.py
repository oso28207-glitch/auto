import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DOCS_DIR = BASE_DIR / "docs"
ASSETS_DIR = BASE_DIR / "assets"

for d in (DATA_DIR, MEDIA_DIR, DOCS_DIR):
    d.mkdir(parents=True, exist_ok=True)


class Config:
    # ═══ Telegram ═══
    API_ID = int(os.environ.get("API_ID", "0") or "0")
    API_HASH = os.environ.get("API_HASH", "").strip()
    SESSION_STRING = os.environ.get(
        "SESSION_STRING", os.environ.get("STRING_SESSION", "")
    ).strip()
    CHANNEL_ID = os.environ.get(
        "CHANNEL_ID", os.environ.get("CHANNEL", "")
    ).strip()

    # ═══ المصدر ═══
    SOURCE_BASE_URL = os.environ.get("SOURCE_BASE_URL", "https://u.3seq.com").rstrip("/")
    SOURCE_CATEGORY = int(os.environ.get("SOURCE_CATEGORY", "712"))
    SOURCE_MAX_PAGES = int(os.environ.get("SOURCE_MAX_PAGES", "20"))

    # ═══ API للبث ═══
    API_BASE = os.environ.get("API_BASE", "")

    # ═══ سلوك ═══
    MAX_EPISODES_PER_RUN = int(os.environ.get("MAX_EPISODES_PER_RUN", "0"))
    # الحد الزمني الافتراضي: 2س 45د = 9900 ثانية
    MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "9900"))
    KEEP_MEDIA = os.environ.get("KEEP_MEDIA", "false").lower() == "true"
    SKIP_COMPRESS = os.environ.get("SKIP_COMPRESS", "false").lower() == "true"
    AUTO_BUILD = os.environ.get("AUTO_BUILD", "true").lower() == "true"

    # ═══ التحديث الفوري للموقع ═══
    REALTIME_PUSH = os.environ.get("REALTIME_PUSH", "true").lower() == "true"
    REALTIME_PUSH_INTERVAL = int(os.environ.get("REALTIME_PUSH_INTERVAL", "60"))

    # ═══ فحص قنوات Telegram ═══
    CHECK_CHANNELS = os.environ.get(
        "CHECK_CHANNELS", "shoofcima,shoofFilm"
    ).strip()
    CHECK_CHANNEL_LIMIT = int(os.environ.get("CHECK_CHANNEL_LIMIT", "3000"))
    CHECK_CACHE_TTL = int(os.environ.get("CHECK_CACHE_TTL", "1800"))

    # ═══ الضغط ═══
    COMPRESS_SCALE = int(os.environ.get("COMPRESS_SCALE", "240"))
    COMPRESS_CRF = int(os.environ.get("COMPRESS_CRF", "28"))
    COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")

    # ═══ yt-dlp متوازي ═══
    YTDLP_CONCURRENT = int(os.environ.get("YTDLP_CONCURRENT", "8"))
    YTDLP_LIMIT_RATE = os.environ.get("YTDLP_LIMIT_RATE", "5M")

    @classmethod
    def validate(cls):
        from errors import ConfigError
        missing = []
        if not cls.API_ID: missing.append("API_ID")
        if not cls.API_HASH: missing.append("API_HASH")
        if not cls.SESSION_STRING: missing.append("SESSION_STRING")
        if not cls.CHANNEL_ID: missing.append("CHANNEL_ID")
        if not cls.SOURCE_BASE_URL: missing.append("SOURCE_BASE_URL")
        if missing:
            raise ConfigError(f"متغيرات مفقودة: {', '.join(missing)}")
        return True


config = Config()