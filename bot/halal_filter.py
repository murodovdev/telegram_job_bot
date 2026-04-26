"""
halal_filter.py – Haram job post detection engine.

Maqsad
------
Ish e'loni sifatida aniqlangan postni (Gate 5 dan o'tgan) halol yoki
harom ekanligini tekshiradi.  Haram kalit so'z topilsa — post guruhga
yuborilmaydi.

Gate tartibida: Gate 5 (job filtr) → Gate 5.2 (halal filtr) → Gate 5.5 (dedup)

Bloklangan toifalar (foydalanuvchi ko'rsatmasi asosida):
  1. Convenience store (편의점 va brend nomlari)
  2. Alkogol bilan bevosita ish
  3. Cho'chqa mahsuloti bilan bevosita ish
  4. Bar, klub, tungi ko'ngilocha, karaoke (tungi)
  5. Foizga asoslangan moliya/sug'urta/kredit savdosi
  6. Qimor, betting, kazino
  7. Kattalar ko'ngilocha
  8. Tekpe
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── Haram keyword registry ────────────────────────────────────────
#
# Har bir toifa alohida — shu tarzda log xabarida aniq ko'rinadi
# qaysi toifa tufayli post bloklangani.
#
# Qoidalar:
#   • Barcha kalit so'zlar kichik harfda
#   • Lotin/Kirill — so'z chegarasi (\b) bilan mos keladi
#   • Koreys/CJK   — substring sifatida mos keladi (chegara yo'q)

HARAM_CATEGORIES: dict[str, list[str]] = {

    # 1. Convenience store ─────────────────────────────────────────
    # Koreyadagi barcha yirik zanjirlar — alkogol, sigaret va ba'zan
    # cho'chqa mahsulotlari sotilishi sababli bloklangan.
    "편의점": [
        "편의점",
        "씨유",        # CU koreys yozuvi
        "지에스",      # GS koreys yozuvi
        "cu 알바",
        "CU",
        "cu",
        "gs25",
        "gs",
        "GS25",
        "gs 25",
        "cu편의점",
        "gs편의점",
        "세븐일레븐",
        "seven eleven",
        "7-eleven",
        "7eleven",
        "이마트24",
        "emart24",
        "미니스톱",
        "ministop",
        "편의점 알바",
        "편의점 직원",
        "convenience store",
    ],

    # 2. Alkogol ──────────────────────────────────────────────────
    "alkogol": [
        # Koreys
        "술집",
        "주류",
        "주류판매",
        "맥주",
        "소주",
        "막걸리",
        "와인",
        "양주",
        "술 판매",
        "주점",
        "호프",         # hof — pivo bari
        "포차",         # pojangmacha — ko'cha bari
        "이자카야",     # yapon bari
        # O'zbek
        "alkogol",
        "spirtli",
        "pivo",
        "vino",
        "арбайт пиво",
        # Rus
        "алкоголь",
        "пиво",
        "вино",
        "спиртное",
        "бар работа",
        "работа бар",
    ],

    # 3. Cho'chqa mahsuloti ───────────────────────────────────────
    "cho'chqa": [
        # Koreys — aniq cho'chqa taomlari
        "삼겹살",       # qorinbog' cho'chqa
        "족발",         # cho'chqa oyoq
        "곱창",         # ichak (odatda cho'chqa)
        "막창",         # oshqozon (cho'chqa)
        "돼지",         # cho'chqa (umumiy)
        "돼지고기",
        "돈까스",       # pork cutlet
        "보쌈",         # qaynatilgan cho'chqa
        "돼지 관련",
        # O'zbek
        "cho'chqa",
        "chochqa",
        # Rus
        "свинина",
        "свиной",
        "свинья",
    ],

    # 4. Bar, klub, tungi ko'ngilocha ─────────────────────────────
    "nightlife": [
        # Koreys
        "룸살롱",
        "유흥",
        "유흥업",
        "유흥업소",
        "나이트클럽",
        "나이트",
        "호스트바",
        "호빠",         # host bar qisqartmasi
        "단란주점",
        "클럽 알바",
        "클럽 직원",
        "바텐더",
        "바 알바",
        "가라오케",
        # O'zbek/Rus
        "bar alb",
        "klub alb",
        "бар алба",
        "ночной клуб",
        "стриптиз",
        "ночной бар",
    ],

    # 5. Foizga asoslangan moliya / sug'urta savdosi ──────────────
    # Shartnomaning o'zi foizga qurilgan bo'lsa — riba hisoblanadi.
    "riba": [
        # Koreys
        "보험영업",
        "보험 영업",
        "보험설계사",
        "보험 설계사",
        "대출영업",
        "대출 영업",
        "카드영업",
        "카드 영업",
        "금융영업",
        "대부업",        # qarz berish biznesi
        "사채",          # norasmiy qarz (yuqori foiz)
        # O'zbek
        "sugurta savdo",
        "sug'urta savdo",
        "kredit savdo",
        # Rus
        "страховка продажа",
        "страховой агент",
        "кредит продажа",
        "финансовые продажи",
    ],

    # 6. Qimor, betting, kazino ───────────────────────────────────
    "qimor": [
        # Koreys
        "카지노",
        "도박",
        "경마",
        "복권",
        "배팅",
        "베팅",
        "스포츠 베팅",
        "토토",
        "경륜",
        # O'zbek
        "kazino",
        "qimor",
        "betting",
        # Rus
        "казино",
        "азартные",
        "ставки",
        "букмекер",
    ],

    # 7. Kattalar ko'ngilocha ─────────────────────────────────────
    "adult": [
        # Koreys
        "성인",
        "성인업소",
        "안마",          # massaj (ko'pincha adult biznes)
        "마사지 알바",
        "퇴폐",
        # O'zbek/Rus
        "adult",
        "стриптиз",
        "эскорт",
    ],

    # 8. Tekpe ────────────────────────────────────────────────────
    # Foydalanuvchi ko'rsatmasi: tekpe ishlari harom hisoblanadi.
    "tekpe": [
        "텍페",
        "텍페알바",
        "tekpe",
        "tekpega",
        "tekpeda",
        "tekpeni",
        "tekpe uchun",
        "tekpa",
        "текпе",
        "CJGA",
        "CJ"
        "Dejon CJ"
        "Hanjin",
        "lotte tekpe"
    ],
}


# ── Pre-compile patterns (once at import time) ────────────────────

def _build_haram_patterns() -> dict[str, re.Pattern]:
    patterns: dict[str, re.Pattern] = {}
    for category, keywords in HARAM_CATEGORIES.items():
        # Koreys/CJK toifalari substring matching
        # Boshqa toifalar — so'z chegara
        parts = []
        for kw in keywords:
            escaped = re.escape(kw)
            # Agar kalit so'zda koreys harfi bo'lsa — chegara qo'ymaymiz
            if re.search(r"[\u3131-\uD7A3]", kw):
                parts.append(escaped)
            else:
                parts.append(r"(?<!\w)" + escaped + r"(?!\w)")
        patterns[category] = re.compile(
            "|".join(parts), re.IGNORECASE | re.UNICODE
        )
    return patterns


_HARAM_PATTERNS: dict[str, re.Pattern] = _build_haram_patterns()


# ── Result type ───────────────────────────────────────────────────

@dataclass
class HalalFilterResult:
    is_haram: bool
    category: Optional[str] = None      # qaysi toifa tufayli bloklandi
    matched_keywords: list = None        # qaysi kalit so'zlar topildi

    def __post_init__(self):
        if self.matched_keywords is None:
            self.matched_keywords = []


# ── Public API ────────────────────────────────────────────────────

def is_haram_job(text: str) -> HalalFilterResult:
    """
    Matnda haram ish e'loniga ishora qiluvchi kalit so'z bor-yo'qligini
    tekshiradi.

    Qaytaradi:
        HalalFilterResult(is_haram=True, ...)  — post bloklash kerak
        HalalFilterResult(is_haram=False)      — post o'tkazish mumkin
    """
    if not text or not text.strip():
        return HalalFilterResult(is_haram=False)

    normalised = _normalise(text)

    for category, pattern in _HARAM_PATTERNS.items():
        matches = pattern.findall(normalised)
        if matches:
            unique = list(dict.fromkeys(m.strip() for m in matches))
            logger.info(
                "[halal_filter] HARAM DETECTED | category=%s | keywords=%s",
                category, unique,
            )
            return HalalFilterResult(
                is_haram=True,
                category=category,
                matched_keywords=unique,
            )

    return HalalFilterResult(is_haram=False)


# ── Internal helpers ──────────────────────────────────────────────

_SEPARATOR_RE = re.compile(r"[-_./|\\]")
_NOISE_RE     = re.compile(r"[^\w\s']", re.UNICODE)


def _normalise(text: str) -> str:
    """filters.py dagi _normalise bilan bir xil pipeline."""
    text = text.lower()
    text = _SEPARATOR_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()