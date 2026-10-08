# TODO — إصلاح مستودع Shoof (auto) — الجولة الثانية

## 1. مقارنة الأعمال بالتيليجرام (إصلاح رسالة "الحلقة لم تُرفع")
- [x] telegram_checker: التقاط file_id + size لكل حلقة أثناء فحص القنوات (shoofcima, shoofFilm)
- [x] telegram_checker: إضافة file_ids/sizes إلى البنية + to_serializable + get_file_id/get_size
- [x] run_all._build_tg_series: تضمين file_id + size + mid في كل حلقة
- [x] build_site._load_stream_map: قراءة tg_series.json كمصدر إضافي للبث (file_id)
- [x] telegram_checker._normalize_name: توحيد الهمزات/الياء/التاء المربوطة + إزالة التشكيل (مطابقة أقوى)
- [x] التحقق: كل حلقة موجودة على التيليجرام تصبح قابلة للتشغيل

## 2. إصلاح التصنيفات
- [x] names.canonical_category: تصنيف عربي موحّد (تركي مدبلج/مدبلج/هندي/أنمي/كوري/أفلام مدبلجة/مترجم)
- [x] run_all._build_tg_series: استخدام canonical_category(name, source_category)
- [x] build_site._cat_label: ترتيب صحيح (فيلم قبل مدبلج، هندي/أنمي)
- [x] التحقق من التصنيفات (11/11 حالة صحيحة)
- [x] إصلاح التصنيفات الخاطئة الفعلية: كانت 13 بلا تصنيف + 5 بـ slug خام (moslslat-turkiaa-modblga/goda-akbar-modblge) + 13 فارغة
- [x] إصلاح جذر المشكلة: _find_source_match_with_search كان يُسقط التصنيف إن توفّرت الصورة فقط
- [x] إصلاح جذر المشكلة: حفظ category في STATE (كان يُحذف بين التشغيلات)
- [x] إثراء data/tg_series.json بالتصنيفات الصحيحة (9 أعمال → مسلسلات مترجمة)

## 3. ترتيب التنزيل + إكمال الحلقات الناقصة
- [x] التأكد من ترتيب الحلقات تصاعدياً (1,2,3...) داخل كل مسلسل
- [x] _filter_new_episodes: تسجيل file_id/size/mid عند اعتبار الحلقة موجودة على التيليجرام
- [x] التحقق من المنطق الكامل (فرز تصاعدي + مقارنة التيليجرام)

## 4. دمج تغييرات المستودع البعيدة (remote)
- [x] git fetch + تحليل الفرق (remote: 500068f, 6891380)
- [x] git merge — حل التعارضات في الملفات المُولَّدة فقط (tg_series.json, index.html, series.json)
- [x] التأكد من أن run_all.py دُمج سليماً (منطقنا + منطق remote معاً)

## 5. التحقق والتسليم
- [x] اختبار محلي (محاكاة فحص التيليجرام + بناء الخريطة) — 4/4 نجحت
- [x] التحقق من سلامة بيانات data/ الحقيقية (tg_series.json = 43 مسلسل)
- [x] حذف الملفات المؤقتة (_diag.py, _verify.py)
- [x] عمل commit للتحسينات الجديدة (تصنيف + دمج)
- [x] إعادة بناء الموقع (43 عمل | 1508 حلقة قابلة للبث | 40 صورة)
- [x] دفع التغييرات إلى GitHub (تم الدفع بنجاح: c6f3718)
- [x] تسليم الملخص
