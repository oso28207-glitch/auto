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
_MOVIE_KEYS = ("فيلم", "أفلام", "افلام", "movie", "film", "aflam")
_TURK_KEYS = ("تركي", "turk")
_IND_KEYS = ("هندي", "hndia", "india")
_KOR_KEYS = ("كوري", "korea", "korean")
_LATIN_KEYS = ("لاتيني", "latin")
_ANIME_KEYS = ("انمي", "أنمي", "انيمى", "anime", "كرتون", "cartoon")
_DUB_KEYS = ("مدبلج", "dubbed", "modblga", "modblja", "modblge", "modbla", "dub")
_TRANS_KEYS = ("مترجم", "ترجم", "subtitle", "sub")


def _has(hay: str, keys) -> bool:
    return any(k in hay for k in keys)


def canonical_category(name: str = "", category: str = "") -> str:
    """يُرجع تصنيفاً عربياً موحّداً واحداً من:
        مسلسلات تركية مدبلجة | مسلسلات مدبلجة | مسلسلات هندية مدبلجة |
        مسلسلات أنمي مدبلجة | مسلسلات كورية مدبلجة | أفلام مدبلجة |
        مسلسلات مترجمة | أفلام | مسلسلات وأفلام
    """
    n = (name or "").lower()
    c = (category or "").lower()

    # إشارات الاسم (الأعلى موثوقية)
    n_movie = _has(n, _MOVIE_KEYS)
    n_turk = _has(n, _TURK_KEYS)
    n_ind = _has(n, _IND_KEYS)
    n_kor = _has(n, _KOR_KEYS)
    n_anime = _has(n, _ANIME_KEYS)
    n_dub = _has(n, _DUB_KEYS)
    n_trans = _has(n, _TRANS_KEYS)

    # إشارات تصنيف المصدر
    c_movie = _has(c, _MOVIE_KEYS)
    c_turk = _has(c, _TURK_KEYS)
    c_ind = _has(c, _IND_KEYS)
    c_kor = _has(c, _KOR_KEYS)
    c_anime = _has(c, _ANIME_KEYS)
    c_dub = _has(c, _DUB_KEYS)
    c_trans = _has(c, _TRANS_KEYS)

    is_movie = n_movie or c_movie
    is_turk = n_turk or c_turk
    is_ind = n_ind or c_ind
    is_kor = n_kor or c_kor
    is_anime = n_anime or c_anime
    is_dub = n_dub or c_dub
    is_trans = n_trans or c_trans

    # الاسم له الأولوية عند التعارض (مثال: "فيلينتا مدبلج" + تصنيف "مترجمة" ⇒ مدبلج)
    if n_dub:
        is_trans = False
    if n_trans and not n_dub:
        is_dub = False

    if is_movie and is_dub:
        return "أفلام مدبلجة"
    if is_turk and is_dub:
        return "مسلسلات تركية مدبلجة"
    if is_ind and is_dub:
        return "مسلسلات هندية مدبلجة"
    if is_kor and is_dub:
        return "مسلسلات كورية مدبلجة"
    if is_anime and is_dub:
        return "مسلسلات أنمي مدبلجة"
    if is_dub:
        return "مسلسلات مدبلجة"
    if is_trans:
        return "مسلسلات مترجمة"
    if is_movie:
        return "أفلام"
    return "مسلسلات وأفلام"


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
