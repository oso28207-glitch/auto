import asyncio
from pathlib import Path
from pyrogram import Client
from pyrogram.errors import FloodWait
from config import config

async def upload_to_telegram(file_path: str, series_name: str, ep_num: int) -> dict:
    app = Client("uploader_session", api_id=config.API_ID, api_hash=config.API_HASH, session_string=config.SESSION_STRING, in_memory=True)
    await app.start()
    
    caption = f"🎬 {series_name}\n📺 الحلقة: {ep_num}\n📥 الجودة: 240p"
    
    try:
        msg = await app.send_video(
            chat_id=config.CHANNEL_ID,
            video=file_path,
            caption=caption,
            supports_streaming=True,
            progress=lambda current, total: print(f"📤 رفع: {current/total*100:.1f}%", end='\r')
        )
        print("\n✅ تم الرفع بنجاح!")
        return {"message_id": msg.id, "file_id": msg.video.file_id}
    except FloodWait as e:
        print(f"\n⏳ انتظار FloodWait: {e.value} ثانية")
        await asyncio.sleep(e.value)
        return await upload_to_telegram(file_path, series_name, ep_num) # إعادة المحاولة
    except Exception as e:
        print(f"\n❌ فشل الرفع: {e}")
        return None
    finally:
        await app.stop()