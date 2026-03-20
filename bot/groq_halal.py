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

_SYSTEM_PROMPT = """You are a filter that reviews job postings for Muslim job seekers. You should only allow halal jobs to pass through.

Jobs considered haram:

Producing, selling, or serving alcohol (bar, beer house, wine shop, liquor store)

Direct work involving pork (samgyeopsal, jokbal, bossam restaurant, pig farm, pork factory)

Bar, nightclub, room salon, host bar, karaoke (alcohol/nightlife related)

Delivery work (tekpe / courier work)

Gambling, casino, betting, lottery sales

Adult entertainment, strip clubs

Interest-based loan or insurance sales (where the main job is selling interest-based financial products)

Convenience stores (편의점) — because alcohol and cigarettes are sold there

Food production factories (if pork or other haram products are involved)

Jobs considered halal:

Factory, warehouse, manufacturing (if not food-related)

Construction, cleaning, moving/carrying

Halal restaurant or kitchen (without pork and alcohol)

Shop/store (if alcohol and cigarettes are not sold)

IT, office, service jobs

Farm work (if no pigs are involved)

Respond only in JSON format and write nothing else:
{"verdict": "halol" or "haram" or "unclear", "reason": "qisqa sabab"}"""


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