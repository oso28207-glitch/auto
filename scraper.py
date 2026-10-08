import requests
from bs4 import BeautifulSoup
from config import config

def scrape_external_metadata() -> list:
    """يجلب قائمة الأعمال، البوسترات، والتصنيفات من المصدر الخارجي"""
    print("🌐 جاري جلب البيانات من المصدر الخارجي...")
    series_list = []
    
    try:
        # مثال: جلب البيانات من ووردبريس أو موقع أفلام (يجب تعديل الرابط والمحددات حسب الموقع الفعلي)
        response = requests.get(f"{config.SOURCE_URL}/moslslat.php", timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # افترض أن كل عمل موجود في عنصر ذو class معين (عدّل هذا حسب الموقع المستهدف)
        for item in soup.select('.movie-item'): # غيّر '.movie-item' حسب هيكل الموقع
            title = item.select_one('.title').text.strip() if item.select_one('.title') else "عنوان غير معروف"
            poster = item.select_one('img')['src'] if item.select_one('img') else ""
            category = item.select_one('.category').text.strip() if item.select_one('.category') else "عام"
            url = item.select_one('a')['href'] if item.select_one('a') else ""
            
            series_list.append({
                "name": title,
                "poster": poster,
                "category": category,
                "source_url": url
            })
            
        print(f"✅ تم جلب {len(series_list)} عمل من المصدر الخارجي")
    except Exception as e:
        print(f"⚠️ فشل جلب البيانات الخارجية: {e}")
        
    return series_list