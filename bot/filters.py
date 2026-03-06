"""
filters.py – Multi-language job-keyword detection engine.

Changes in v3 (fuzzy matching)
-------------------------------
Old approach: exact phrase matching only.
  "ishchi kerak" → only matched that exact phrase.
  "1 kishi kerak zavod" → MISSED because "kishi kerak" is not adjacent to trigger.

New approach: TWO-TIER matching.
  Tier 1 — Strong single keywords: words that alone are strong job signals.
    e.g. "vakansiya", "алба", "구인" → immediate match, high confidence.
  Tier 2 — Weak signal words + proximity check: words that are job-related
    but only meaningful when combined with another signal word nearby.
    e.g. "kerak" alone is not enough, but "kerak" + "ish" anywhere in the
    same message = job post.

This catches real-world messy writing like:
  "1 kishiga ish bor"  →  tier-1 match on "ish bor"  ✅
  "kishi kerak bugun"  →  tier-2: "kerak" + "kishi"  ✅
  "zavod uchun odam"   →  tier-2: "odam" + "zavod"   ✅
  "kerak"              →  alone, not a job post       ✅ (no false positive)
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#  TIER 1 — Strong standalone keywords
#  Any single match here → confirmed job post
# ══════════════════════════════════════════════════════════════════

STRONG_KEYWORDS: dict[str, list[str]] = {
    "uzbek": [
        "ish bor",
        "ish bor edi",
        "ishchi kerak",
        "ishchilar kerak",
        "odam kerak",
        "odamlar kerak",
        "kishi kerak",
        "kishiga ish",
        "kishilar kerak",
        "ish taklif",
        "ish o'rni",
        "ish joyi",
        "ish o'rinlari",
        "ishga taklif",
        "ishga olinadi",
        "ishga olaman",
        "ishga olamiz",
        "ish beraman",
        "ish beramiz",
        "ish beriladi",
        "xodim kerak",
        "xodimlar kerak",
        "hamkor kerak",
        "yordamchi kerak",
        "vakansiya",
        "arbayt",          # very common Uzbek-Korean loanword
        "arbait",
        "tekpe",           # Korean "part-time" used by Uzbeks
        "ishxona",
        "ish haqi",
        "ish xaqi",        # common misspelling
        "ish haki",        # another variant
        "ish vaqti",
        "иш бор",
        "joy bor",
        "iw bor"
        
    ],
    "russian": [
        "работа есть",
        "нужен работник",
        "нужны работники",
        "ищем сотрудника",
        "ищем работника",
        "требуется работник",
        "требуются работники",
        "вакансия",
        "подработка",
        "нужен человек",
        "нужны люди",
        "трудоустройство",
        "на работу",
        "найм",
        "зарплата",
        "арбайт",
        "иш бор",
        
    ],
    "korean": [
        "일자리",
        "알바",
        "직원",
        "구합니다",
        "구인",
        "채용",
        "모집",
        "아르바이트",
        "취업",
        "일 있어요",
        "일 있음",
        "사람 구해요",
        "사람 구함",
        "시급",
        "월급",
        "주급",
        "급여",
        "일당",
        "구직",
    ],
}


# ══════════════════════════════════════════════════════════════════
#  TIER 2 — Weak signal words (proximity matching)
#  A message must contain AT LEAST ONE word from EACH sub-list
#  within the SAME message to be considered a job post.
# ══════════════════════════════════════════════════════════════════

WEAK_COMBOS: list[dict] = [
    # Uzbek: "kerak" + a person/job word
    {
        "lang": "uzbek",
        "label": "kerak+subject",
        "groups": [
            ["kerak"],
            ["odam", "kishi", "kishilar", "xodim", "ishchi", "bola",
             "yigit", "qiz", "usta", "haydovchi", "driver"],
        ],
    },
    # Uzbek: "ish" + a location/type word (catches "zavod uchun ish")
    {
        "lang": "uzbek",
        "label": "ish+location",
        "groups": [
            ["ish"],
            ["zavod", "fabrika", "ombor", "sklat", "qurilish", "ferma",
             "restoran", "dokon", "magazin", "kafe", "ofis", "bor"],
        ],
    },
    # Russian: "работа" + verb or descriptor
    {
        "lang": "russian",
        "label": "работа+context",
        "groups": [
            ["работа", "работу", "работы"],
            ["нужн", "ищем", "есть", "завод", "склад", "срочно"],
        ],
    },
]


# ── Pre-compile strong patterns ───────────────────────────────────

def _build_strong_patterns() -> dict[str, re.Pattern]:
    patterns: dict[str, re.Pattern] = {}
    for lang, words in STRONG_KEYWORDS.items():
        if lang == "korean":
            combined = "|".join(re.escape(w) for w in words)
        else:
            combined = "|".join(
                r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words
            )
        patterns[lang] = re.compile(combined, re.IGNORECASE | re.UNICODE)
    return patterns


_STRONG_PATTERNS: dict[str, re.Pattern] = _build_strong_patterns()


# ── Result type ───────────────────────────────────────────────────

@dataclass
class FilterResult:
    is_job: bool
    matched_lang: Optional[str] = None
    matched_keywords: List[str] = field(default_factory=list)
    confidence: float = 0.0
    match_tier: int = 0   # 1 = strong keyword, 2 = weak combo


# ── Public API ────────────────────────────────────────────────────

def is_job_message(text: str) -> FilterResult:
    """
    Two-tier job detection.

    Returns FilterResult with is_job=True if either:
      • Tier 1: a strong keyword is found (high confidence), OR
      • Tier 2: a weak-signal combo is satisfied (medium confidence).
    """
    if not text or not text.strip():
        return FilterResult(is_job=False)

    normalised = _normalise(text)

    # ── Tier 1: strong keyword scan ───────────────────────────────
    for lang, pattern in _STRONG_PATTERNS.items():
        matches = pattern.findall(normalised)
        if matches:
            unique = list(dict.fromkeys(m.strip() for m in matches))
            logger.debug("[filter] T1 hit · lang=%s · kw=%s", lang, unique)
            return FilterResult(
                is_job=True,
                matched_lang=lang,
                matched_keywords=unique,
                confidence=min(1.0, 0.6 + 0.1 * len(unique)),
                match_tier=1,
            )

    # ── Tier 2: weak combo scan ───────────────────────────────────
    for combo in WEAK_COMBOS:
        matched_words: List[str] = []
        all_groups_hit = True
        for group in combo["groups"]:
            pattern = re.compile(
                "|".join(r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in group),
                re.IGNORECASE | re.UNICODE,
            )
            found = pattern.findall(normalised)
            if found:
                matched_words.extend(found)
            else:
                all_groups_hit = False
                break

        if all_groups_hit:
            unique = list(dict.fromkeys(w.strip() for w in matched_words))
            logger.debug(
                "[filter] T2 hit · combo=%s · words=%s",
                combo["label"], unique,
            )
            return FilterResult(
                is_job=True,
                matched_lang=combo["lang"],
                matched_keywords=unique,
                confidence=0.5,
                match_tier=2,
            )

    return FilterResult(is_job=False)


def add_strong_keyword(lang: str, keyword: str) -> None:
    """Dynamically add a strong keyword and rebuild patterns."""
    lang = lang.lower()
    keyword = keyword.lower()
    if lang not in STRONG_KEYWORDS:
        STRONG_KEYWORDS[lang] = []
    if keyword not in STRONG_KEYWORDS[lang]:
        STRONG_KEYWORDS[lang].append(keyword)
        _STRONG_PATTERNS.update(_build_strong_patterns())
        logger.info("[filter] Strong keyword added: [%s] '%s'", lang, keyword)


def list_keywords() -> dict[str, list[str]]:
    """Return a copy of the strong keyword registry."""
    return {lang: list(words) for lang, words in STRONG_KEYWORDS.items()}


def _normalise(text: str) -> str:
    """Lower-case and collapse excessive whitespace."""
    return re.sub(r"\s+", " ", text.lower()).strip()