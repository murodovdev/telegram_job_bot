"""
groq_halal.py – Groq API orqali kontekstga asoslangan halollik tekshiruvi.

Qanday ishlaydi
---------------
halal_filter.py (kalit so'z filtri) dan O'TGAN xabarlar uchun ishlatiladi.
Kalit so'z filtridan o'tgan, lekin konteksti noaniq bo'lgan holatlarni
Groq LLM orqali tekshiradi.

Qaror mantiqi
-------------
    "halol"   → guruhga yuboriladi ✅
    "haram"   → bloklaydi ❌
    "unclear" → guruhga yuboriladi ✅  (noaniq = xavfsiz tomonga xato)

API xatosi yoki timeout bo'lsa → o'tkazib yuboriladi ✅
(API ishlamay qolsa bot to'xtab qolmasligi uchun)

Railway Variables
-----------------
    GROQ_API_KEY = gsk_...
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from bot.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 10   # sekund — agar API javob bermasa, o'tkazib yuboramiz

# Module-level singleton session — reused for the bot's entire lifetime.
_session: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    """Persistent aiohttp session qaytaradi. Yo'q bo'lsa yaratadi."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    """Bot yopilayotganda sessionni to'g'ri yopish uchun. main.py dan chaqiriladi."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None

_SYSTEM_PROMPT = """Sen musulmon foydalanuvchi uchun ish e’lonlari va daromad manbalarini Islomiy jihatdan tekshiruvchi juda qat’iy va ehtiyotkor filtersan.

Vazifang:
Ish e’lonini tahlil qil va faqat bitta hukm chiqar:
- "halol"
- "haram"
- "unclear"

Asosiy tamoyil:
Shubhali yoki yetarli ma’lumot bo‘lmagan ishni hech qachon "halol" deb chiqarma.
Default hukm: "unclear"

Qat’iy qaror ustuvorligi:
1. Agar ish bevosita yoki aniq ravishda harom narsaga xizmat qilsa -> "haram"
2. Agar ish toza ekani aniq va yetarli ma’lumot bilan isbotlansa -> "halol"
3. Qolgan barcha holatlarda -> "unclear"


"halol" faqat quyidagi holatda beriladi:
- ish vazifasi aniq yozilgan bo‘lsa
- mahsulot/xizmat turi aniq yozilgan bo‘lsa
- harom unsur yo‘qligi aniq ko‘rinsa

Agar quyidagilardan biri noma’lum bo‘lsa, odatda "unclear":
- nima sotilishi
- nima tashilishi
- nima ishlab chiqarilishi
- restoranda qanday ovqat borligi
- ombordagi mahsulot turi
- hoteldagi aniq vazifa
- sales ishida nima sotilishi

Har doim "haram" deb bahola, agar ish quyidagilarga bevosita bog‘liq bo‘lsa:
- alkogolni sotish, quyish, serve qilish, tashish, reklama qilish
- cho‘chqa go‘shti yoki cho‘chqa mahsulotini tayyorlash, sotish, qadoqlash, tashish
- kazino, betting, qimor
- foizli kredit, interest, riba asosidagi mahsulot yoki xizmat
- insurance sales agar asosiy model riba/foizga bog‘liq bo‘lsa
- pornografiya, adult entertainment, hostess, room salon, nightlife
- scam, fake review, fake document, document forgery, account rental, fraud
- noqonuniy yoki aldamchi ish
- harom narsani bevosita qo‘llab-quvvatlaydigan ish

Odatda "unclear" deb bahola, agar:
- restoran/kafe bor, lekin pork yoki alkogol bor-yo‘qligi noma’lum
- ombor bor, lekin mahsulot noma’lum
- delivery bor, lekin nima tashilishi noma’lum
- factory bor, lekin nima ishlab chiqarilishi noma’lum
- sales bor, lekin nima sotilishi noma’lum
- hotel bor, lekin vazifa aniq emas
- kompaniya aralash faoliyat yuritadi va foydalanuvchining roli noaniq

"halol" faqat aniq toza holatlarda berilishi mumkin, masalan:
- dasturlash
- data entry
- tarjimonlik
- o‘qituvchilik
- oddiy ofis ishi
- kiyim-kechak ombori
- elektronika ombori
- sabzavot/meva bilan ishlash
- oddiy tozalash
- halol restoran
- harom mahsulotga aloqasi bo‘lmagan logistika
- aniq halol mahsulot zavodi

Muhim ehtiyot signallari:
Agar e’londa quyidagi so‘zlar ish vazifasi yoki asosiy mahsulot bilan bog‘liq bo‘lsa, juda ehtiyot bo‘l va ko‘pincha "haram" deb bahola:
- 주류, 술, 맥주, 와인, 소주, 호프
- 돼지고기, 삼겹살, 돈육, pork, bacon, ham
- 카지노, 토토, betting, gambling
- 대출, 이자, loan, credit
- 보험영업, insurance sales
- 룸, 유흥, bar, club, karaoke, hostess
- 성인, adult
- 리뷰 작업, 대리, 문서 위조, fake, scam, 계좌 대여

Qo‘shimcha qoidalar:
- Foydalanuvchini qulaylik uchun emas, diniy xavfsizlik uchun himoya qil
- Taxmin qilib "halol" dema
- Ish aralash bo‘lsa, lekin foydalanuvchining vazifasi aniq bo‘lmasa -> "unclear"
- E’londa halal va haram signal birga uchrasa, harom signal ustun turadi
- Yetarli dalilsiz "halol" berma
- Juda qisqa e’lonlarda ehtiyotkor bo‘l
- Noma’lum kompaniya nomining o‘zi "halol" uchun yetarli emas

Javob qoidasi:
- Faqat JSON qaytar
- Hech qanday qo‘shimcha matn yozma
- Hech qanday markdown yozma
- "reason" juda qisqa bo‘lsin
- "reason" 3 dan 12 tagacha so‘zdan oshmasin

Faqat mana shu formatda javob ber:
{"verdict":"halol|haram|unclear","reason":"qisqa sabab"}"""


@dataclass
class GroqHalalResult:
    verdict: str          # "halol" | "haram" | "unclear"
    reason: str = ""
    api_error: bool = False


async def check_halal_with_groq(text: str) -> GroqHalalResult:
    """
    Groq API orqali ish e'lonining halolligini tekshiradi.

    Returns:
        GroqHalalResult(verdict="halol")   — halol, yuborish mumkin
        GroqHalalResult(verdict="haram")   — harom, bloklash kerak
        GroqHalalResult(verdict="unclear") — noaniq, bloklash kerak
        GroqHalalResult(api_error=True)    — API xatosi, o'tkazib yuborish
    """
    if not GROQ_API_KEY:
        logger.warning("[groq_halal] GROQ_API_KEY not set — skipping AI check")
        return GroqHalalResult(verdict="halol", reason="api key not configured")

    # Truncate to 800 chars to keep token usage reasonable
    truncated = text[:800]

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Ish e'loni:\n{truncated}"},
        ],
        "max_tokens": 100,
        "temperature": 0.1,   # past temperature = izchil natija
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        session = await _get_session()
        async with session.post(
            GROQ_API_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=GROQ_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "[groq_halal] API error %d: %s", resp.status, body[:200]
                )
                return GroqHalalResult(verdict="halol", api_error=True)

            data = await resp.json()

    except asyncio.TimeoutError:
        logger.warning("[groq_halal] Timeout after %ds — passing through", GROQ_TIMEOUT)
        return GroqHalalResult(verdict="halol", api_error=True)

    except Exception as exc:
        logger.warning("[groq_halal] Request failed: %s — passing through", exc)
        return GroqHalalResult(verdict="halol", api_error=True)

    # Javobni parse qilish
    try:
        content = data["choices"][0]["message"]["content"].strip()
        # JSON ni ajratib olish (ba'zan model ```json ... ``` qo'shadi)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        parsed = json.loads(content)
        verdict = parsed.get("verdict", "unclear").lower().strip()
        reason  = parsed.get("reason", "")

        # Faqat ruxsat etilgan qiymatlar
        if verdict not in ("halol", "haram", "unclear"):
            verdict = "unclear"

        logger.info(
            "[groq_halal] verdict=%s | reason=%s | preview=%r",
            verdict, reason, text[:60],
        )
        return GroqHalalResult(verdict=verdict, reason=reason)

    except Exception as exc:
        logger.warning(
            "[groq_halal] Failed to parse response: %s | raw=%s",
            exc, str(data)[:200],
        )
        return GroqHalalResult(verdict="halol", api_error=True)