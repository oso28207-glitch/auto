import json
from pathlib import Path
from config import config, DOCS_DIR, DATA_DIR

def build_netflix_site(tg_data: dict, scraped_data: list):
    print("🏗️ جاري بناء موقع نتفليكس...")
    
    merged_series = []
    for item in scraped_data:
        norm_name = _normalize_name_for_match(item["name"])
        tg_info = tg_data.get(norm_name, {})
        
        episodes = []
        for ep_num, msg_id in sorted(tg_info.get("episodes", {}).items()):
            episodes.append({
                "ep_num": ep_num,
                "msg_id": msg_id,
                "stream_url": f"https://t.me/c/{config.CHANNEL_ID.replace('-100', '')}/{msg_id}" # رابط مباشر أو استخدم بوت
            })
            
        if episodes: # نضيف فقط الأعمال التي لها حلقات مرفوعة
            merged_series.append({
                "name": item["name"],
                "poster": item["poster"],
                "category": item["category"],
                "episodes": episodes
            })
            
    # حفظ البيانات للموقع
    with open(DOCS_DIR / "series.json", "w", encoding="utf-8") as f:
        json.dump(merged_series, f, ensure_ascii=False, indent=2)
        
    print(f"✅ تم بناء الموقع بنجاح لـ {len(merged_series)} عمل")

def _normalize_name_for_match(name: str) -> str:
    import re
    n = re.sub(r"^(مسلسل|فيلم)\s+", "", name, flags=re.IGNORECASE)
    n = re.sub(r"\s+الموسم\s+\d+", "", n, flags=re.IGNORECASE)
    n = re.sub(r"[^\w\s\u0600-\u06FF]", "", n)
    return re.sub(r"\s+", " ", n).strip().lower()