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
