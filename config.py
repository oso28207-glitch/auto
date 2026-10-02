import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DOCS_DIR = BASE_DIR / "docs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


class Config:
    # تليجرام
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    SESSION_STRING = os.environ.get("SESSION_STRING", "")
    CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

    # المصادر
    SOURCE_BASE_URL = os.environ.get("SOURCE_BASE_URL", "")
    SOURCE_SERIES_PATH = os.environ.get("SOURCE_SERIES_PATH", "/series")
    SOURCE_EPISODE_PATTERN = os.environ.get("SOURCE_EPISODE_PATTERN", "/episode/{episode}")
    SOURCE_LIST_SELECTOR = os.environ.get("SOURCE_LIST_SELECTOR", "a.series-link")
    SOURCE_EP_SELECTOR = os.environ.get("SOURCE_EP_SELECTOR", "a.episode-link")
    USER_AGENT = os.environ.get(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )

    # سلوك
    MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
    RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "10"))
    CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "0"))  # 0 = مرة واحدة
    AUTO_BUILD = os.environ.get("AUTO_BUILD", "true").lower() == "true"
    KEEP_MEDIA = os.environ.get("KEEP_MEDIA", "false").lower() == "true"
    API_BASE = os.environ.get("API_BASE", "http://localhost:8000")

    @classmethod
    def validate(cls):
        from errors import ConfigError
        required = ["API_ID", "API_HASH", "CHANNEL_ID"]
        missing = [k for k in required if not getattr(cls, k)]
        if missing:
            raise ConfigError(f"متغيرات مفقودة: {', '.join(missing)}")
        if not cls.SESSION_STRING and not cls.BOT_TOKEN:
            raise ConfigError("يجب توفير SESSION_STRING أو BOT_TOKEN")
        return True


config = Config()