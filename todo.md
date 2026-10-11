# TODO — إصلاح مستودع Shoof (auto) — الجولة الثالثة

## 0. التشخيص (مكتمل)
- [x] فحص المستودع + سجلات GitHub Actions (run 38109547359)
- [x] السبب الجذري لعشق (u3seq): مشغّل vidsp.net يرفض التضمين بدون Referer صحيح
      → يعيد "Embeds disabled2" → لا m3u8 → فشل 236/236 تنزيل
- [x] تأكيد الحل: curl_cffi + Referer = صفحة المشغّل الكاملة + رابط m3u8 مباشر في HTML
- [x] استخراج كل السيرفرات عبر temp/ajax/iframe2.php (vidsp/vinovo/vids/vidaraa)
- [x] تشخيص التصنيفات: 879 بلا تصنيف + slugs خام + أقسام ناقصة (عربي/أجنبي/مترجم/هندي)
- [x] تشخيص السجلات: 1147 "بدون URL" = في الحقيقة download_failed (كلها عشق)

## 1. إصلاح جلب سيرفرات عشق (u3seq) — downloader.py ✅
- [x] إضافة مسار مباشر `_u3seq_direct()` عبر curl_cffi (watch → servers → iframe2.php → m3u8)
- [x] دالة `_player_m3u8()` لاستخراج m3u8 من HTML المشغّل مع Referer
- [x] دالة `_download_m3u8_direct()` (yt-dlp → ffmpeg → curl_cffi يدوي)
- [x] دالة `_download_m3u8_manual()` (جلسة ثابتة + إعادة محاولة لكل مقطع)
- [x] إصلاح `_open()` لدعم تمرير Referer إلى Page.navigate
- [x] إصلاح `_u3seq()` لتجربة كل السيرفرات (لا الأول فقط)
- [x] ربط المسار المباشر في `_run_browser` كخيار أول (المتصفح احتياطي)
- [x] إصلاح `_cffi_get`: إضافة X-Requested-With + خفض حد الطول (AJAX يعيد 203 حرف)
- [x] اختبار محلي: استخراج m3u8 (master + variant) من عشق بنجاح ✅

## 2. إصلاح التصنيفات — names.py + build_site.py + config.py
- [x] names.canonical_category: إضافة عربي/أجنبي/مترجم/هندي/أنمي مترجم
- [x] names: خريطة أولويات موحّدة CATEGORY_PRIORITY
- [x] names: fallback لتصنيفات yam الفارغة (نص عربي → عربية)
- [x] config: توسيع YAM_CATEGORIES (18 قسم) + توحيد priority_of/priority_of_series
- [x] build_site: أقسام جديدة (تركي مدبلج/هندي مدبلج/مدبلج/أنمي/كوري/مترجم/عربي/أجنبي/أفلام مدبلجة)
- [x] build_site: _section_of/_priority_of/_cat_label/_index_html محدّثة
- [x] run_all: _sort_for_download + prio_counts (12 قسم)
- [x] .github/workflows: تحديث YAM_CATEGORIES
- [x] تطبيع الـ slugs الخام في البيانات + بناء الموقع من جديد
- [x] تحسين: التصنيف العام "مسلسلات وأفلام" يُستنتج من نص الاسم (عربي/أجنبي) بدل الفئة العامة

## 3. مراجعة سجلات الأكشن وإصلاح الأخطاء
- [x] تصحيح رسالة "بدون URL" → تفصيل حسب السبب (بدون رابط/فشل التنزيل/فشل الرفع)
- [x] معالجة egybest (معطّل) بشكل واضح دون أخطاء + احترام ENABLED_SOURCES
- [x] تنظيف سجل الحلقات المتخطّاة بعد نجاح الرفع (سجل دقيق)
- [x] yam: جلب التصنيفات بالتوازي (18 قسم) + CAT_WORKERS

## 4. التحقق والتسليم
- [x] اختبار محلي: استخراج m3u8 من عشق + تنزيل مقطع (watch→servers→iframe2→m3u8=200→variant=200→segment)
- [x] اختبار محلي: التصنيفات (18/18 حالة ناجحة + 0 فئة عامة متبقية)
- [x] بناء الموقع محلياً والتحقق من الأقسام (4 أقسام مأهولة)
- [ ] commit + push إلى GitHub
- [ ] تسليم الملخص
