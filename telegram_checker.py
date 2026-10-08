import re
import asyncio
from pyrogram import Client
from pyrogram.errors import FloodWait
from config import config

def _normalize_name(name: str) -> str:
    n = re.sub(r"^(مسلسل|فيلم)\s+", "", name, flags=re.IGNORECASE)
    n = re.sub(r"\s+الموسم\s+\d+", "", n, flags=re.IGNORECASE)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return re.sub(r"\s+", " ", n).strip().lower()

async def scan_telegram_channels() -> dict:
    """يفحص القنوات ويعيد قاموس: {اسم_مسلسل_منظم: {رقم_حلقة: message_id}}"""
    app = Client("checker_session", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.SESSION_STRING, in_memory=True)
    await app.start()
    
    result = {}
    channels = [c.strip() for c in config.CHECK_CHANNELS.split(",") if c.strip()]
    
    for channel in channels:
        print(f"🔍 جاري فحص القناة: @{channel}")
        count = 0
        try:
            async for msg in app.get_chat_history(channel, limit=5000):
                if not (msg.video or msg.document):
                    continue
                
                caption = (msg.caption or "").strip()
                match = re.search(r"(.*?)\s+(?:الموسم\s+\d+\s+)?الحلقة\s+(\d+)", caption, re.IGNORECASE)
                if match:
                    series_name = match.group(1).strip()
                    ep_num = int(match.group(2))
                    norm_name = _normalize_name(series_name)
                    
                    if norm_name not in result:
                        result[norm_name] = {"display_name": series_name, "episodes": {}}
                    
                    result[norm_name]["episodes"][ep_num] = msg.id
                    count += 1
        except FloodWait as e:
            print(f"⏳ انتظار FloodWait لمدة {e.value} ثانية...")
            await asyncio.sleep(e.value)
        except Exception as e:
            print(f"⚠️ خطأ في فحص @{channel}: {str(e)[:100]}")
            
    await app.stop()
    print(f"✅ تم فحص تليجرام: تم العثور على {len(result)} عمل")
    return result