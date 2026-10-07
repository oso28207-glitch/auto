#!/usr/bin/env python3
"""
rebuild_uploaded.py — يعيد بناء uploaded.json من قنوات Telegram
يشغَّل مرة واحدة فقط بعد التحديث.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import config, DATA_DIR

UPLOADED = DATA_DIR / "uploaded.json"


def _extract_series_episode(caption: str):
    if not caption:
        return None, None
    ep_num = None
    for p in [r"الحلقة\s*(\d+)", r"[Ee]pisode\s*(\d+)", r"حلقة\s*(\d+)"]:
        m = re.search(p, caption)
        if m:
            ep_num = int(m.group(1))
            break
    if ep_num is None:
        return None, None

    lines = caption.split("\n")
    series_name = None
    for line in lines:
        clean = re.sub(r"^[📺🎬🎥🎞️🔹\-•\s]+", "", line).strip()
        if "الحلقة" in clean or "حلقة" in clean.lower():
            continue
        if "episode" in clean.lower() or "ep." in clean.lower():
            continue
        if clean and len(clean) > 2:
            series_name = clean
            break
    return series_name, ep_num


def _normalize(name: str) -> str:
    if not name:
        return ""
    n = re.sub(r"\s+", " ", name.strip())
    n = re.sub(r"^مسلسل\s+", "", n)
    n = re.sub(r"\s+الموسم\s+.*$", "", n)
    n = re.sub(r"\s+\d+\s*$", "", n)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return n.strip().lower()


async def main():
    channels = [c.strip().replace("@", "")
                for c in config.CHECK_CHANNELS.split(",") if c.strip()]
    print(f"📡 القنوات: {channels}")

    uploaded = {}
    total = 0

    app = Client(
        "rebuild_uploaded",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        in_memory=True,
        no_updates=True,
    )

    await app.start()
    try:
        for channel in channels:
            print(f"\n📡 فحص: @{channel}")
            count = 0
            async for msg in app.get_chat_history(
                channel, limit=config.CHECK_CHANNEL_LIMIT
            ):
                if not (msg.video or msg.document or msg.animation):
                    continue
                caption = msg.caption or msg.text or ""
                name, ep = _extract_series_episode(caption)
                if not name or ep is None:
                    continue
                key = f"{name}|ep{ep}"
                if key in uploaded:
                    continue
                media = msg.video or msg.document or msg.animation
                uploaded[key] = {
                    "url": f"https://t.me/{channel}/{msg.id}",
                    "done": True,
                    "source": "telegram",
                    "message_id": msg.id,
                    "file_id": getattr(media, "file_id", ""),
                    "size": getattr(media, "file_size", 0),
                    "at": msg.date.isoformat() if msg.date else "",
                }
                count += 1
                total += 1
            print(f"   ✅ {count} حلقة")
    finally:
        await app.stop()

    # حفظ
    UPLOADED.write_text(
        json.dumps(uploaded, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ إجمالي: {total} حلقة في {len(uploaded)} مفتاح")


if __name__ == "__main__":
    asyncio.run(main())