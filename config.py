import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DOCS_DIR = BASE_DIR / "docs"
ASSETS_DIR = BASE_DIR / "assets"  # تم إصلاح الخطأ هنا

for d in (DATA_DIR, MEDIA_DIR, DOCS_DIR, ASSETS_DIR):
    d.mkdir(parents=True, exist_ok=True)

class Config:
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH", "").strip()
    SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
    CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
    CHECK_CHANNELS = os.environ.get("CHECK_CHANNELS", "shoofcima,shoofFilm").strip()
    
    # إعدادات المصادر الخارجية
    SOURCE_URL = os.environ.get("SOURCE_URL", "https://example.com").rstrip("/")
    
    # إعدادات الضغط (240p)
    COMPRESS_SCALE = int(os.environ.get("COMPRESS_SCALE", "240"))
    COMPRESS_CRF = int(os.environ.get("COMPRESS_CRF", "28"))
    COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")
    COMPRESS_AUDIO_BITRATE = os.environ.get("COMPRESS_AUDIO_BITRATE", "48k")
    COMPRESS_MAX_SIZE_MB = int(os.environ.get("COMPRESS_MAX_SIZE_MB", "80"))
    
    MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "10800")) # 3 ساعات

    @classmethod
    def validate(cls):
        missing = [k for k, v in {"API_ID": cls.API_ID, "API_HASH": cls.API_HASH, 
                                   "SESSION_STRING": cls.SESSION_STRING, "CHANNEL_ID": cls.CHANNEL_ID}.items() if not v]
        if missing:
            raise Exception(f"متغيرات بيئة مفقودة: {', '.join(missing)}")

config = Config()