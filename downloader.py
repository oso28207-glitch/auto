import os
import subprocess
from pathlib import Path
from config import config, MEDIA_DIR

def compress_to_240p(input_path: str, output_path: str) -> bool:
    """ضغط الفيديو إلى 240p مع الحفاظ على نسبة الأبعاد"""
    print(f"🗜️ جاري الضغط إلى {config.COMPRESS_SCALE}p...")
    
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale=-2:{config.COMPRESS_SCALE}",
        "-c:v", "libx264",
        "-preset", config.COMPRESS_PRESET,
        "-crf", str(config.COMPRESS_CRF),
        "-c:a", "aac",
        "-b:a", config.COMPRESS_AUDIO_BITRATE,
        "-movflags", "+faststart",
        output_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200) # ساعتين كحد أقصى
        if result.returncode == 0 and Path(output_path).exists():
            size_mb = Path(output_path).stat().st_size / (1024 * 1024)
            print(f"✅ تم الضغط بنجاح. الحجم الجديد: {size_mb:.1f} MB")
            return True
        else:
            print(f"❌ فشل FFmpeg: {result.stderr[-200:]}")
            return False
    except subprocess.TimeoutExpired:
        print("❌ تجاوز الوقت المحدد للضغط")
        return False
    except Exception as e:
        print(f"❌ خطأ أثناء الضغط: {e}")
        return False

# ملاحظة: دالة التنزيل الفعلية (yt-dlp أو selenium) يمكن إضافتها هنا
# للتبسيط، نفترض أن الملف تم تنزيله مسبقاً أو ستضيف دالة download_url(url, out_path)