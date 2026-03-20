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

_SYSTEM_PROMPT = """Sen musulmon ish qidiruvchilar uchun ish e'lonlarini 
tekshiruvchi filtrsан. Faqat halol ishlarni o'tkazib yuborasan.

Harom hisoblanadigan ishlar:
- Alkogol ishlab chiqarish, sotish yoki xizmat ko'rsatish (bar, pivoxona, 
  vinoteka, spirtli ichimliklar do'koni)
- Cho'chqa go'shti bilan bevosita ish (samgyeopsal, jokbal, bossam 
  restoran, cho'chqa ferma, cho'chqa zavodi)
- Bar, tungi klub, room salon, host bar, karaoke (spirtli, tungi)
- Tekpe (tekpe ishi)
- Qimor, kazino, stavka, loterеya sotish
- Kattalar ko'ngilocha, striptiz
- Foizga asoslangan kredit yoki sug'urta savdosi (asosiy ish foiz sotish)
-편의점 (convenience store) — alkogol va sigaret sotiladi

Halol hisoblanadigan ishlar:
- Zavod, ombor, fabrika (oziq-ovqat bo'lmasa)
- Qurilish, tozalash, yuk tashish
- Halol restoran, oshxona (cho'chqa va alkogolsiz)
- Do'kon (alkogol va sigaret sotilmasa)
- IT, ofis, xizmat ko'rsatish
- Ferma (cho'chqa bo'lmasa)

Faqat JSON formatda javob ber, boshqa hech narsa yozma:
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