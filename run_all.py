import asyncio
import time
from config import config
from telegram_checker import scan_telegram_channels
from scraper import scrape_external_metadata
from builder import build_netflix_site
# from downloader import download_and_compress # قم بتفعيلها عند اكتمال دالة التنزيل
# from uploader import upload_to_telegram

async def main():
    print("🚀 بدء تشغيل نظام الأتمتة المتكامل...")
    config.validate()
    start_time = time.time()
    
    # 1. فحص تليجرام
    tg_data = await scan_telegram_channels()
    
    # 2. جلب البيانات الخارجية
    scraped_data = scrape_external_metadata()
    
    # 3. مقارنة وتحديد النواقص (هنا يمكنك إضافة منطق التنزيل والرفع للحلقات الناقصة)
    # for item in scraped_data:
    #     check_missing_and_download_upload(item, tg_data)
    
    # 4. بناء الموقع
    build_netflix_site(tg_data, scraped_data)
    
    # 5. دفع التحديثات لـ GitHub (يتم عبر GitHub Actions تلقائياً عند commit)
    print("💡 قم بعمل git add . && git commit -m 'update' && git push لتحديث الموقع")
    
    elapsed = (time.time() - start_time) / 60
    print(f"🏁 انتهت العملية في {elapsed:.1f} دقيقة")

if __name__ == "__main__":
    asyncio.run(main())