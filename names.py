"""
names.py — تطبيع أسماء المسلسلات/الأفلام للمطابقة الموثوقة بين:
    - قنوات Telegram (أسماء مختصرة: "حياتي الرائعة الموسم 1")
    - المصادر الخارجية (u3seq / yam: "مسلسل حياتي الرائعة")
    - حالة الرفع state.json ("مسلسل حياتي الرائعة")

المشكلة الأساسية: نفس العمل يُكتب بطرق مختلفة:
    * بادئة "مسلسل"/"فيلم" موجودة أو لا
    * رقم الموسم بالأرقام "الموسم 1" أو بالترتيب "الموسم الأول"
    * لاحقة "مدبلج"/"مترجم"/"كامل"
    * اختلاف الهمزات/التاء المربوطة/الياء + التشكيل

norm(name)      → تطبيع كامل (الموسم → sN)
base_norm(name) → تطبيع بدون رقم الموسم (للمطابقة عند غياب الموسم)
"""
import re

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# الترتيب العربي → رقم
_ORDINALS = {
    "الاول": "1", "اول": "1",
    "الثاني": "2", "ثاني": "2",
    "الثالث": "3", "ثالث": "3",
    "الرابع": "4", "رابع": "4",
    "الخامس": "5", "خامس": "5",
    "السادس": "6", "سادس": "6",
    "السابع": "7", "سابع": "7",
    "الثامن": "8", "ثامن": "8",
    "التاسع": "9", "تاسع": "9",
    "العاشر": "10", "عاشر": "10",
    "الحادي عشر": "11", "الثاني عشر": "12",
}

_PREFIXES = (
    "مسلسل", "مسلسلة", "مسلسلات", "فيلم", "افلام", "أفلام",
    "انمي", "أنمي", "انيمي", "برنامج", "حلقات",
)

_NOISE = (
    "مدبلج", "مدبلجة", "مدبلجين", "مترجم", "مترجمة", "مترجمين",
    "كامل", "كاملة", "الجديد", "الجديدة", "جديد", "جديدة",
    "الحلقة", "حلقة", "الحلقات",
)

_SEASON_WORDS = ("الموسم", "موسم", "الجزء", "جزء", "season", "part")


def _clean(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.translate(_AR_DIGITS)
    # إزالة التشكيل (harakat) والتطويل (tatweel)
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s)
    # توحيد الحروف
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ٱ", "ا").replace("ى", "ي").replace("ئ", "ي")
          .replace("ؤ", "و").replace("ة", "ه"))
    # إبقاء الحروف العربية والأرقام واللاتينية فقط
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_prefix(s: str) -> str:
    for p in _PREFIXES:
        if s == p:
            return ""
        if s.startswith(p + " "):
            return s[len(p) + 1:].strip()
    return s


def norm(name: str) -> str:
    """تطبيع كامل: يحوّل كلمات الموسم إلى 's' والأرقام الترتيبية إلى أرقام."""
    s = _clean(name)
    s = _strip_prefix(s)

    # الموسم/الجزء → s
    for w in _SEASON_WORDS:
        s = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", " s ", s)

    # الترتيب العربي → رقم
    for w, d in _ORDINALS.items():
        s = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", f" {d} ", s)

    # إزالة الضوضاء
    for n in _NOISE:
        s = re.sub(rf"(?<!\w){re.escape(n)}(?!\w)", " ", s)

    # توحيد: s + رقم متلاصقين
    s = re.sub(r"\bs\s*(\d+)", r"s\1", s)
    s = re.sub(r"\s+", "", s)
    return s


def base_norm(name: str) -> str:
    """تطبيع بدون رقم الموسم — يُستخدم كاحتياطي للمطابقة."""
    s = norm(name)
    s = re.sub(r"s\d+", "", s)
    return s


# ══════════════════════════════════════════════════════════════════════════
# فصل الموسم عن اسم العمل (لدمج المواسم في عمل واحد بأزرار)
# ══════════════════════════════════════════════════════════════════════════
_SEASON_WORD_RE = re.compile(r"(?:ال)?(?:موسم|جزء|season|part)", re.IGNORECASE)


def _fold_letters(s: str) -> str:
    """توحيد الحروف العربية للمطابقة فقط (لا يُغيِّر النص المعروض).

    يوحّد الهمزات (أ/إ/آ/ٱ → ا) والألف المقصورة (ى → ي) والياء (ئ → ي)
    والواو (ؤ → و) والتاء المربوطة (ة → ه). ضروري للتعرف على الترتيب
    المكتوب بالهمزة مثل «الأول»/«الثاني» و«الثانى» (بالألف المقصورة).
    """
    return (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
             .replace("ٱ", "ا").replace("ى", "ي").replace("ئ", "ي")
             .replace("ؤ", "و").replace("ة", "ه"))

_NUM_WORDS = {
    "الاول": 1, "الاولى": 1, "اول": 1, "first": 1,
    "الثاني": 2, "الثانية": 2, "ثاني": 2, "second": 2,
    "الثالث": 3, "الثالثة": 3, "ثالث": 3, "third": 3,
    "الرابع": 4, "الرابعة": 4, "رابع": 4, "fourth": 4,
    "الخامس": 5, "الخامسة": 5, "خامس": 5, "fifth": 5,
    "السادس": 6, "السادسة": 6, "سادس": 6, "sixth": 6,
    "السابع": 7, "السابعة": 7, "سابع": 7, "seventh": 7,
    "الثامن": 8, "الثامنة": 8, "ثامن": 8, "eighth": 8,
    "التاسع": 9, "التاسعة": 9, "تاسع": 9, "ninth": 9,
    "العاشر": 10, "العاشرة": 10, "عاشر": 10, "tenth": 10,
    "الحادي عشر": 11, "الثاني عشر": 12,
}

# لواحق ضوضائية تُزال من نهاية اسم العمل
_NOISE_SUFFIX_RE = re.compile(
    r"\s*[-:]?\s*(?:مدبلج(?:ة|ين)?|مدبلجه|مترجم(?:ة|ين)?|كامل(?:ة)?|"
    r"الجديد(?:ة)?|جديد(?:ة)?|والاخيرة|والأخيرة)\s*$"
)


def split_season(name: str):
    """يفصل اسم العمل عن رقم الموسم.

    أمثلة:
        'حب للايجار الموسم 2'            → ('حب للايجار', 2)
        'انت من احببت 2 الموسم الثاني'   → ('انت من احببت', 2)
        'حياتي الرائعة الموسم 1'         → ('حياتي الرائعة', 1)
        'الخياط Terzi الموسم الثاني'     → ('الخياط Terzi', 2)

    يُعيد (اسم العمل, رقم الموسم). رقم الموسم = 1 إن لم يوجد موسم صريح.
    """
    if not name:
        return name or "", 1
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", str(name).strip()).translate(_AR_DIGITS)
    season = 1

    m = _SEASON_WORD_RE.search(s)
    if m:
        pos = m.end()
        while pos < len(s) and s[pos] in " \t-:،.":
            pos += 1
        num = None
        mm = re.match(r"(\d+)", s[pos:])
        if mm:
            num = int(mm.group(1))
            pos += mm.end()
        else:
            for w in ("الحادي عشر", "الثاني عشر"):
                if _fold_letters(s[pos:]).startswith(w):
                    num = _NUM_WORDS[w]
                    pos += len(w)
                    break
            if num is None:
                toks = s[pos:].split(None, 1)
                if toks:
                    # ★ نُطبّع الحروف للمطابقة حتى يُتعرف على «الأول/الثاني/الثالث»
                    #   المكتوبة بالهمزة أو «الثانى» بالألف المقصورة.
                    tok = _fold_letters(toks[0].strip(".,:،"))
                    if tok in _NUM_WORDS:
                        num = _NUM_WORDS[tok]
                        pos += len(toks[0])
        if num:
            season = num
        s = (s[:m.start()] + " " + s[pos:]).strip()

    s = re.sub(r"\s+", " ", s).strip(" -:،.")

    # رقم زائد في النهاية يطابق رقم الموسم (مثال: 'انت من احببت 2 الموسم الثاني')
    m2 = re.search(r"\s(\d+)\s*$", s)
    if m2 and int(m2.group(1)) == season:
        s = s[:m2.start()].strip()

    s = _NOISE_SUFFIX_RE.sub("", s).strip()
    return (s or name), season


def tokens(name: str) -> set:
    """كلمات الاسم (مطبَّعة) لقياس التشابه."""
    s = _clean(name)
    s = _strip_prefix(s)
    for n in _NOISE:
        s = re.sub(rf"(?<!\w){re.escape(n)}(?!\w)", " ", s)
    for w in _SEASON_WORDS:
        s = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", " ", s)
    for w, d in _ORDINALS.items():
        s = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", f" {d} ", s)
    return {t for t in re.split(r"\s+", s) if len(t) > 1}


def similarity(a: str, b: str) -> float:
    """معامل Jaccard بين كلمات الاسمين."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ══════════════════════════════════════════════════════════════════════════
# التصنيف الموحّد (canonical category)
#   يحوّل أي إشارة (اسم العمل أو تصنيف المصدر: slug عربي/إنجليزي) إلى وسم
#   عربي واحد ثابت، مع إعطاء الاسم الأولوية عند التعارض.
# ══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# التصنيف الموحّد (canonical category)
#   يحوّل أي إشارة (اسم العمل أو تصنيف المصدر: slug عربي/إنجليزي) إلى وسم
#   عربي واحد ثابت، مع إعطاء الاسم الأولوية عند التعارض.
# ═══════════════════════════════════════════════════════════════════════════

# ── أسماء الأقسام الموحّدة ──
CAT_TURK_DUB   = "مسلسلات تركية مدبلجة"
CAT_IND_DUB    = "مسلسلات هندية مدبلجة"
CAT_ANIME_DUB  = "مسلسلات أنمي مدبلجة"
CAT_KOR_DUB    = "مسلسلات كورية مدبلجة"
CAT_DUB        = "مسلسلات مدبلجة"
CAT_DUB_MOVIES = "أفلام مدبلجة"
CAT_TRANS      = "مسلسلات مترجمة"
CAT_ARABIC     = "مسلسلات عربية"
CAT_FOREIGN    = "مسلسلات أجنبية"
CAT_ASIAN      = "مسلسلات أسيوية"
CAT_MOVIES     = "أفلام"
CAT_GENERIC    = "مسلسلات وأفلام"

# ── ترتيب الأقسام في الموقع (الأصغر يُعرض أولاً) ──
CATEGORY_PRIORITY = {
    CAT_TURK_DUB:   1,
    CAT_IND_DUB:    2,
    CAT_ANIME_DUB:  3,
    CAT_KOR_DUB:    4,
    CAT_DUB:        5,
    CAT_TRANS:      6,
    CAT_ARABIC:     7,
    CAT_FOREIGN:    8,
    CAT_ASIAN:      9,
    CAT_DUB_MOVIES: 10,
    CAT_MOVIES:     11,
    CAT_GENERIC:    12,
}

ALL_CATEGORIES = list(CATEGORY_PRIORITY.keys())

# ── كلمات مفتاحية ──
_MOVIE_KEYS = ("فيلم", "أفلام", "افلام", "movie", "film", "aflam")
# تصنيفات المصدر التي تعني "أفلام" صراحةً (yam)
_MOVIE_SLUG_KEYS = ("aflam", "افلام", "أفلام", "movies", "films")
_TURK_KEYS = ("تركي", "turk")
_IND_KEYS = ("هندي", "hndia", "india")
_KOR_KEYS = ("كوري", "korea", "korean")
_ANIME_KEYS = ("انمي", "أنمي", "انيمى", "انيمي", "anime", "كرتون", "cartoon")
_ASIAN_KEYS = ("اسيوي", "أسيوي", "اسيوية", "asia", "asian")
_ARABIC_KEYS = (
    "عربي", "عربية", "مصري", "مصرية", "خليجي", "خليجية", "سعودي", "لبناني",
    "سوري", "عراقي", "مغربي", "جزائري", "تونسي", "كويتي", "اماراتي", "إماراتي",
    "اردني", "أردني", "فلسطيني", "يمني", "قطري", "بحريني", "عماني", "سوداني",
    "رمضان", "arabic", "arab", "egyptian", "masry", "khaleeji", "gulf",
    "arbyaa", "msriya",
)
_FOREIGN_KEYS = (
    "اجنبي", "أجنبي", "امريكي", "أمريكي", "بريطاني", "انجليزي", "إنجليزي",
    "فرنسي", "اسباني", "إسباني", "الماني", "ألماني", "ايطالي", "إيطالي",
    "روسي", "مكسيكي", "برازيلي", "كندي", "استرالي", "foreign", "english",
    "american", "british", "spanish", "mexican", "latin", "russian",
)
_DUB_KEYS = ("مدبلج", "dubbed", "modblga", "modblja", "modblge", "modbla", "dub")
_TRANS_KEYS = ("مترجم", "ترجم", "subtitle", "sub", "motrgma")

# ── إصلاحات صريحة لبعض الـ slugs التي لا تحمل كلمات مفتاحية ──
_SLUG_FIX = {
    "moslslat-": CAT_ARABIC,        # مسلسلات خليجية
    "moslslat-ahwak": CAT_GENERIC,
    "tv-show": CAT_GENERIC,
    "msr7yat": CAT_GENERIC,
    "moslslat-cartoon": CAT_ANIME_DUB,        # أنمي/كرتون
    "mosalslat-english": CAT_FOREIGN,         # أجنبي
    "moslslat-asia": CAT_ASIAN,               # آسيوي
    "moslslat-msriya": CAT_ARABIC,            # مصري
    "1moslslat-arbyaa": CAT_ARABIC,           # عربي
    "moslslat-turkiaa-motrgma": CAT_TRANS,    # تركي مترجم
    "moslslat-turkiaa1": CAT_TRANS,           # تركي مترجم
    "moslslat-turkiaa-modblga": CAT_TURK_DUB, # تركي مدبلج
    "moslslat-hndia-modblja": CAT_IND_DUB,    # هندي مدبلج
    "indian-series": CAT_IND_DUB,             # هندي
    "moslslat-modblga": CAT_DUB,              # مدبلج عام
    "goda-akbar-modblge": CAT_DUB,
    "mn-elnazra-elthania-modblge": CAT_DUB,
    # ── أفلام (yam) ──
    "aflam-dub": CAT_DUB_MOVIES,              # أفلام مدبلجة
    "aflam-arabya1": CAT_MOVIES,              # أفلام عربية
    "aflam-turky": CAT_MOVIES,                # أفلام تركية
    "aflam-hindi": CAT_MOVIES,                # أفلام هندية
}


def _has(hay: str, keys) -> bool:
    return any(k in hay for k in keys)


def canonical_category(name: str = "", category: str = "",
                       source: str = "") -> str:
    """يُرجِع تصنيفاً عربياً موحّداً واحداً من:
        مسلسلات تركية مدبلجة | مسلسلات هندية مدبلجة | مسلسلات أنمي مدبلجة |
        مسلسلات كورية مدبلجة | مسلسلات مدبلجة | أفلام مدبلجة | مسلسلات مترجمة |
        مسلسلات عربية | مسلسلات أجنبية | مسلسلات أسيوية | أفلام | مسلسلات وأفلام
    """
    n = (name or "").lower()
    c = (category or "").strip().lower()
    s = (source or "").lower()
    # التصنيف العام "مسلسلات وأفلام" محايد — لا يعني أن العمل فيلم
    c_match = "" if c == CAT_GENERIC.lower() else c
    hay = f"{n} {c_match}"

    # ── كشف الفيلم: من الاسم ("فيلم ...") أو من تصنيف المصدر (aflam-*) ──
    is_movie = _has(hay, _MOVIE_KEYS) or _has(c, _MOVIE_SLUG_KEYS)
    is_turk = _has(hay, _TURK_KEYS)
    is_ind = _has(hay, _IND_KEYS)
    is_kor = _has(hay, _KOR_KEYS)
    is_anime = _has(hay, _ANIME_KEYS)
    is_asian = _has(hay, _ASIAN_KEYS)
    is_arabic = _has(hay, _ARABIC_KEYS)
    is_foreign = _has(hay, _FOREIGN_KEYS)
    is_dub = _has(hay, _DUB_KEYS)
    is_trans = _has(hay, _TRANS_KEYS)
    has_latin = bool(re.search(r"[a-z]{3,}", n))

    # الاسم له الأولوية عند التعارض (مثال: "فيلينتا مدبلج" + تصنيف "مترجمة" ⇒ مدبلج)
    if _has(n, _DUB_KEYS):
        is_trans = False
    elif _has(n, _TRANS_KEYS):
        is_dub = False

    # مصدر عشق (u3seq) = مسلسلات تركية (مدبلجة افتراضياً)
    if s == "u3seq" and not is_movie:
        is_turk = True
        if not is_trans:
            is_dub = True

    # ── الترتيب ──
    if is_movie and is_dub:
        return CAT_DUB_MOVIES
    if is_movie:
        return CAT_MOVIES
    if is_dub:
        if is_turk:
            return CAT_TURK_DUB
        if is_ind:
            return CAT_IND_DUB
        if is_kor:
            return CAT_KOR_DUB
        if is_anime:
            return CAT_ANIME_DUB
        return CAT_DUB
    if is_trans:
        return CAT_TRANS
    if is_anime:
        return CAT_ANIME_DUB
    if is_arabic:
        return CAT_ARABIC
    if is_foreign or (has_latin and not (is_turk or is_ind or is_kor or is_anime)):
        return CAT_FOREIGN
    if is_turk:
        return CAT_TRANS
    if is_ind:
        return CAT_TRANS
    if is_asian:
        return CAT_ASIAN
    if c in _SLUG_FIX:
        return _SLUG_FIX[c]
    # yam: تصنيف فارغ (من صفحة القائمة الرئيسية) — نستنتج من نص الاسم.
    #   ملاحظة: المسلسلات التركية/الأجنبية/المدبلجة تُلتقط عبر تصنيفاتها المخصّصة،
    #   لذا المتبقّي هنا (نص عربي بلا كلمات مفتاحية) هو غالباً عربي (مصري/خليجي).
    # التصنيف العام "مسلسلات وأفلام" أو الفارغ محايد — نستنتج من نص الاسم.
    #   ملاحظة: عشق (u3seq) يعود مبكراً كتركي مدبلج، لذا لا يصل إلى هنا.
    if not c or c == CAT_GENERIC.lower():
        if re.search(r"[\u0600-\u06FF]", n):
            return CAT_ARABIC
        if n.strip():
            return CAT_FOREIGN
    return CAT_GENERIC


if __name__ == "__main__":
    tests = [
        ("حياتي الرائعة الموسم 1", "مسلسل حياتي الرائعة"),
        ("المشردون 2 الموسم الثاني", "مسلسل المشردون 2 الموسم الثاني"),
        ("احتمال حب", "مسلسل احتمال حب"),
        ("جودا اكبر مدبلج", "مسلسل جودا اكبر"),
        ("ابن سينا العبقري الصغير", "مسلسل ابن سينا العبقري الصغير"),
        ("مسلسل ما زلت في 17", "ما زلت في 17"),
        ("شراب التوت 2 الموسم الثاني", "مسلسل شراب التوت 2 الموسم الثاني"),
    ]
    for a, b in tests:
        print(f"norm      {a!r} -> {norm(a)!r} | {b!r} -> {norm(b)!r} | "
              f"eq={norm(a) == norm(b)}")
        print(f"base_norm {base_norm(a)!r} == {base_norm(b)!r} -> "
              f"{base_norm(a) == base_norm(b)} | sim={similarity(a, b):.2f}")
        print()
