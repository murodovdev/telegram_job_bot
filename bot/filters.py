"""
filters.py – Job keyword detection engine.

Strategy (v4 — Tier 1 only)
-----------------------------
Tier 2 (fuzzy word-combination matching) was removed because loose signal
words like "kerak" or "ish" appear in everyday conversation and caused too
many false positives — unrelated messages were being forwarded to the target
group.

The filter now uses ONLY Tier 1: a curated list of strong, specific keyword
phrases that are unambiguous job signals on their own.  This trades a small
amount of recall for a large gain in precision.

To catch a message that is currently being missed, add the specific phrase
it uses to STRONG_KEYWORDS below and restart the bot.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Strong keyword registry ───────────────────────────────────────
#
# Rules
#   • All entries must be lowercase.
#   • Latin / Cyrillic keywords use word-boundary matching (\b).
#   • Korean / CJK keywords use substring matching (no word boundaries).
#   • A single match from any entry is enough to classify as a job post.

STRONG_KEYWORDS: dict[str, list[str]] = {
    # ── Uzbek ──────────────────────────────────────────────────────
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
        "arbayt",        # Korean part-time loanword used by Uzbeks
        "arbait",
        "tekpe",         # Korean 텍배 (delivery work), used by Uzbeks
    ],

    # ── Russian ────────────────────────────────────────────────────
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
        "нужен человек на работу",
        "нужны люди на работу",
        "трудоустройство",
        "на работу срочно",
        "арбайт",
        "зарплата от",
    ],

    # ── Korean ─────────────────────────────────────────────────────
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


# ── Pre-compile patterns (once at import time) ────────────────────

def _build_patterns() -> dict[str, re.Pattern]:
    patterns: dict[str, re.Pattern] = {}
    for lang, words in STRONG_KEYWORDS.items():
        if lang == "korean":
            # Korean has no word spaces — use plain substring match
            combined = "|".join(re.escape(w) for w in words)
        else:
            # Latin / Cyrillic — word-boundary anchors for precision
            combined = "|".join(
                r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words
            )
        patterns[lang] = re.compile(combined, re.IGNORECASE | re.UNICODE)
    return patterns


_PATTERNS: dict[str, re.Pattern] = _build_patterns()


# ── Result type ───────────────────────────────────────────────────

@dataclass
class FilterResult:
    is_job: bool
    matched_lang: Optional[str] = None
    matched_keywords: List[str] = field(default_factory=list)
    confidence: float = 0.0
    match_tier: int = 1   # always 1 now — kept for DB compatibility


# ── Public API ────────────────────────────────────────────────────

def is_job_message(text: str) -> FilterResult:
    """
    Return a FilterResult indicating whether *text* is a job announcement.

    Scans all three language keyword sets. Returns on the first match found.
    """
    if not text or not text.strip():
        return FilterResult(is_job=False)

    normalised = _normalise(text)

    for lang, pattern in _PATTERNS.items():
        matches = pattern.findall(normalised)
        if matches:
            unique = list(dict.fromkeys(m.strip() for m in matches))
            logger.debug("[filter] Match · lang=%s · kw=%s", lang, unique)
            return FilterResult(
                is_job=True,
                matched_lang=lang,
                matched_keywords=unique,
                confidence=min(1.0, 0.6 + 0.1 * len(unique)),
                match_tier=1,
            )

    return FilterResult(is_job=False)


def add_keyword(lang: str, keyword: str) -> None:
    """Dynamically add a keyword and rebuild the compiled pattern."""
    lang    = lang.lower()
    keyword = keyword.lower()
    if lang not in STRONG_KEYWORDS:
        STRONG_KEYWORDS[lang] = []
    if keyword not in STRONG_KEYWORDS[lang]:
        STRONG_KEYWORDS[lang].append(keyword)
        _PATTERNS.update(_build_patterns())
        logger.info("[filter] Keyword added: [%s] '%s'", lang, keyword)


def list_keywords() -> dict[str, list[str]]:
    """Return a copy of the keyword registry."""
    return {lang: list(words) for lang, words in STRONG_KEYWORDS.items()}


# ── Internal helpers ──────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower()).strip()