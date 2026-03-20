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
    "unclear" → bloklaydi ❌  (faqat aniq halol bo'lsa o'tadi)

API xatosi yoki timeout bo'lsa → o'tkazib yuboriladi ✅
(API ishlamay qolsa bot to'xtab qolmasligi uchun)

Railway Variables
-----------------
    GROQ_API_KEY = gsk_...
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-8b-instant"
GROQ_TIMEOUT = 10   # sekund — agar API javob bermasa, o'tkazib yuboramiz

_SYSTEM_PROMPT = """Sen Koreyadagi o'zbek ishchilar uchun ish e'lonlarini tekshiruvchi filtrsаn.
Ish e'lonlari asosan O'ZBEK tilida yoziladi. O'zbek tilini yaxshi tushunishing shart.
Faqat aniq harom ishlarni bloklaysan.

=== O'ZBEK TILI LEKSIKASI ===
Bu so'zlar va iboralar O'ZBEK ARGOSI — harom emas:
- "shitr", "shtur", "shitrr", "shturr" = pul yaxshi, maosh yaxshi
- "kerak", "odam kerak", "kishi kerak" = ishchi qidirilmoqda
- "arbayt" = aytbay (qo'shimcha ish)
- "tekpe" = HAROM (alohida ishlash turi, halol emas)
- "zavod", "fabrika", "sklad" = zavod, fabrika, ombor (HALOL)
- "svarka", "yirtaman" = payvandlash ishi (HALOL)
- "yuk", "tashish" = yuk tashish (HALOL)
- "tozalash", "klinining" = tozalik xizmati (HALOL)
- "qurilish" = qurilish ishi (HALOL)
- "haydovchi", "driver" = haydovchi (HALOL)

=== QOIDALAR ===
1. Agar matnda aniq harom belgi YO'Q bo'lsa — "halol" de.
2. Faqat "haram" de agar ANIQ harom ish nomi bo'lsa.
3. Shubha bo'lsa — "unclear" de, "haram" dema.
4. O'zbek argosi va noaniq so'zlarni harom deb hisoblama.

=== HAROM ISHLAR ===
- Alkogol sotish/ishlab chiqarish (bar, pivoxona, 술집, 주류)
- Cho'chqa go'shti bilan bevosita ish (삼겹살집, 족발집, cho'chqa ferma)
- Tungi klub, room salon (룸살롱, 유흥업소, ночной клуб)
- Tekpe ishi (텍페, tekpe, tekpa)
- Kazino, qimor (카지노, 도박, kazino)
- Kattalar ko'ngilocha (성인업소, стриптиз)
- Foizga asoslangan kredit/sug'urta savdosi (보험영업, 대출영업)
- Convenience store (편의점, CU, GS25)
- Harom deb hisoblangan mahsulot yoki xizmatlarni taklif qiladigan ishlar
- Harom ovqat yoki alkogol kabi mahsulotlarni yetkazib berish
- Oziq-ovqat ishlab chiqaradigan yoki ularni qadoqlaydigan ishlar (sabzavot va mevalar bularning ichiga kirmaydi)

=== REAL NAMUNALAR ===

Namuna 1 (HALOL):
"Ertaga SVARKA ni yirtaman degan 2 kishiga ish bor. Puli SHITRRRR"
Javob: {"verdict": "halol", "reason": "svarka — payvandlash ishi, halol. Shitr — o'zbek argosida pul yaxshi degani"}

Namuna 2 (HALOL):
"Sklad ishiga odam kerak. Kuniga 130,000 won. Busan. Erkak 35 yoshgacha"
Javob: {"verdict": "halol", "reason": "ombor ishi, hech qanday harom element yo'q"}

Namuna 3 (HALOL):
"Zavod uchun 5 nafar ishchi kerak. D2 visa bo'lsa yaxshi. Ish haqi oyiga 2.8 million"
Javob: {"verdict": "halol", "reason": "zavod ishi, halol"}

Namuna 4 (HALOL):
"Qurilishga kuchli yigitlar kerak. Yashash joyi bor. Pul har kuni"
Javob: {"verdict": "halol", "reason": "qurilish ishi, halol"}

Namuna 5 (HAROM):
"편의점 알바 구합니다. 야간 가능하신 분"
Javob: {"verdict": "haram", "reason": "convenience store (편의점) — alkogol va sigaret sotiladi"}

Namuna 6 (HAROM):
"Tekpe ishiga odam kerak. Koreyscha bilmasa ham bo'ladi"
Javob: {"verdict": "haram", "reason": "tekpe ishi harom"}

Namuna 7 (HAROM):
"삼겹살집 서빙 알바. 주 5일, 시급 12,000원"
Javob: {"verdict": "haram", "reason": "삼겹살집 — cho'chqa go'shti restorani"}

Namuna 8 (HALOL):
"Moshinali odamga ish bor. Yuk tashish. Seul ichida"
Javob: {"verdict": "halol", "reason": "haydovchi yoki yuk tashish ishi, halol"}

Faqat JSON formatda javob ber:
{"verdict": "halol" yoki "haram" yoki "unclear", "reason": "qisqa sabab"}"""


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

    # Matnni 800 ta belgiga qisqartirish — token tejash uchun
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
        async with aiohttp.ClientSession() as session:
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