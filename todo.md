# TODO — إصلاح مشروع Shoof (auto-main)

## 1. التحليل والفحص
- [x] قراءة كل السكربتات الأساسية (run_all, build_site, config, sources/*, downloader, uploader)
- [x] فحص ملفات data/*.json و docs/ الحالية
- [x] اختبار المصادر مباشرة
- [x] تحديد كل الأخطاء

## 2. توحيد ضغط الفيديو إلى 240 بكسل
- [x] config.py: COMPRESS_SCALE = 240
- [x] automation.yml: COMPRESS_SCALE=240
- [x] import_channels.yml: التأكد من 240

## 3. إصلاح جلب الصور من المواقع الخارجية (u3seq, yam)
- [x] sources/yam.py: استخراج البوستر من data-echo (lazy-load) + إصلاح الزحف
- [x] sources/u3seq.py: curl_cffi + safari17_0 لتجاوز Cloudflare + بوستر من صفحة المنشور
- [x] مصادر/yam v6: ترقيم كامل 95 صفحة + زحف التصنيفات المدبلجة → 1176 مسلسل بصور (95992 حلقة)
- [x] تحسين المطابقة بين أسماء تليجرام وأسماء المصادر (names.py) + search.php fallback

## 4. إصلاح بناء الموقع (build_site.py + عرض الموقع)
- [x] إصلاح مسارات الصور (posters/... للـ index، ../posters/... للـ watch)
- [x] إنشاء docs/posters/placeholder.jpg حقيقي
- [x] ضمان وجود style.css + watch.js في docs/
- [x] إعادة كتابة صفحات المشاهدة لتطابق watch.js
- [x] ربط الحلقات بالمشغل (stream عبر API_BASE باستخدام file_id)
- [x] كتابة docs/videos.json + docs/config.js
- [x] إصلاح تصنيف الأقسام (slug مدبلج/تركي) + شارات نظيفة
- [x] **إصلاح خطأ JS `e.trim is not a function`** (watch.js: تمرير كائن للحلقة غير القابلة للبث إلى hls.js)
- [x] إضافة رسالة "الحلقة غير متاحة" بدل انهيار المشغل
- [x] إضافة cache-busting (?v=3) لكل الأصول

## 5. إصلاح الأتمتة على GitHub Actions
- [x] توحيد automation.yml و import_channels.yml
- [x] إصلاح requirements.txt (curl_cffi, requests)
- [x] التأكد من عمل run_all.py تلقائياً

## 6. إصلاح السكربتات القديمة/المتعارضة
- [x] config.py: إضافة الخصائص الناقصة
- [x] إصلاح تعارض uploader signature في orchestrator.py
- [x] إصلاح builder.py / import_from_channels.py

## 7. الاختبار والتسليم
- [x] اختبار yam fetch (1176 مسلسل، كلها بصور)
- [x] اختبار u3seq fetch (32 مسلسل، كلها بصور)
- [x] بناء الموقع محلياً والتحقق من العرض (43 عمل، 1508 حلقة، 36 صورة)
- [x] التحقق من إصلاح خطأ watch.js في المتصفح (لا أخطاء JS)
- [x] فحص صحة كل سكربتات Python
- [ ] ضغط المستودع وتسليمه
