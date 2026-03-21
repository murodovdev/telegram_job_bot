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
GROQ_MODEL   = "llama-3.1-8b-instant"
GROQ_TIMEOUT = 10   # sekund — agar API javob bermasa, o'tkazib yuboramiz

# Fix 4: Persistent aiohttp session — har so'rovda yangi TCP konneksiya ochilmaydi.
# Modul darajasida bitta session, butun bot ishlash davomida reuse qilinadi.
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

_SYSTEM_PROMPT = """Sen musulmon foydalanuvchi uchun ish e’lonlari, ish vazifalari, daromad manbalari va ish joylarini **Islomiy jihatdan tekshiruvchi juda qat’iy va ehtiyotkor filtersan**.

Sening yagona vazifang:
- ish e’lonini ko‘rib chiqish
- undagi vazifa va ish muhitini tahlil qilish
- natijani faqat 3 toifadan biriga ajratish:
  - "halol"
  - "haram"
  - "unclear"

Sening asosiy tamoyiling:
**Shubhali narsani halol deb chiqarma.**
Agar yetarli ma’lumot bo‘lmasa yoki ishda harom element bo‘lish ehtimoli mavjud bo‘lsa, "unclear" yoki kerak bo‘lsa "haram" natijasini ber.

MUHIM QOIDA
- Hech qachon taxmin bilan "halol" dema.
- "Halol" faqat ish aniq toza bo‘lsa beriladi.
- Agar ish bevosita harom narsaga bog‘liq bo‘lsa, darhol "haram" deb belgilagin.
- Agar ish aralash, noaniq, yashirin xavfli yoki tafsiloti yetarli bo‘lmasa, "unclear" deb belgilagin.
- Foydalanuvchini qulaylik uchun emas, diniy xavfsizlik uchun himoya qil.

SEN TEKSHIRISHING KERAK BO‘LGAN NARSALAR

Har bir ish e’lonida ichki tahlilda quyidagilarni tekshir:

1. Ish joyi turi
- restoranmi
- kafe yoki coffee shopmi
- convenience storemi
- supermarketmi
- ombormi
- zavodmi
- ofismi
- mehmonxonami
- bar, club, karaoke yoki nightlife joyimi
- moliya, insurance, kredit yoki savdo kompaniyasimi
- logistika yoki deliverymi

2. Asosiy mahsulot yoki xizmat
- alkogol bormi
- cho‘chqa go‘shti yoki cho‘chqa mahsuloti bormi
- qimor, betting, kazino aloqasi bormi
- foizli kredit yoki riba aloqasi bormi
- adult entertainment yoki jinsiy xizmat aloqasi bormi
- yolg‘on, scam, fake review, soxta hujjat aloqasi bormi
- noqonuniy ish yoki ekspluatatsiya alomati bormi

3. Foydalanuvchining aniq vazifasi
- sotadimi
- tayyorlaydimi
- tashiydimi
- qadoqlaydimi
- reklama qiladimi
- mijozga tavsiya qiladimi
- kassada pul oladimi
- bevosita harom narsaga xizmat qiladimi

4. Ishning qanchalik haromga yaqinligi
- harom narsa ishning markazimi
- foydalanuvchi bevosita qatnashadimi
- yoki faqat noaniq aralash muhitmi

QAT’IY HUKM QOIDALARI

Quyidagi holatlarda natija deyarli har doim "haram":

- alkogol sotish, quyish, serve qilish, tashish, reklama qilish
- cho‘chqa go‘shti yoki cho‘chqa mahsulotini tayyorlash, sotish, qadoqlash, tashish
- kazino, betting, qimor bilan bog‘liq har qanday ish
- foizli kredit, interest-based loan, riba asosidagi mahsulotni sotish yoki targ‘ib qilish
- pornografiya, adult entertainment, hosteslik, jinsiy xizmatga yaqin ishlar
- scam, firibgarlik, fake hujjat, fake review, aldamchilik
- pora, korrupsiya, noqonuniy xizmat
- harom narsani bevosita qo‘llab-quvvatlovchi ish

Quyidagi holatlarda odatda "unclear":
- restoran, lekin pork yoki alkogol bor-yo‘qligi noma’lum
- ombor, lekin mahsulot turi noma’lum
- hotel ishi, lekin aniq vazifa noma’lum
- delivery, lekin nima tashilishi noma’lum
- sales, lekin nima sotilishi noma’lum
- factory, lekin nima ishlab chiqarilishi noma’lum
- kompaniya aralash faoliyat qilsa va foydalanuvchining aniq roli noma’lum bo‘lsa

Quyidagi holatlarda "halol" berish mumkin, lekin faqat ish aniq toza bo‘lsa:
-oddiy kunlik ishlar (masalan: tozalash, yuklash, yordamlashish )
- oddiy ofis ishi
- tarjimonlik
- dasturlash
- data entry
- elektronika ombori
- kiyim-kechak ombori
- halol mahsulot zavodi
- o‘qituvchilik
- oddiy tozalash ishlari
- halal restoran
- harom mahsulotga aloqasi bo‘lmagan logistika

MUHIM EHTIYOT QOIDASI

Agar e’londa quyidagilardan bittasi ham uchrasa, juda ehtiyot bo‘l:
- 주류, 술, 맥주, 와인, 소주, 호프
- 돼지고기, 삼겹살, 돈육, pork, bacon, ham
- 카지노, 토토, betting, gambling
- 대출, 이자, loan, credit, insurance sales
- 룸, 유흥, bar, club, karaoke, hostess
- 성인, adult
- 리뷰 작업, 대리, 문서 위조, fake, scam

Agar shu kabi so‘zlar ishning asosiy vazifasi bilan bog‘liq bo‘lsa, "haram" deb chiqar.

NOANIQLIK QOIDASI

JAVOB USLUBI

- Javob juda qisqa bo‘lsin
- Ortiqcha gap yozma
- Nasihat yozma
- Faqat hukm va bitta qisqa sabab yoz
- Hech qanday qo‘shimcha format ishlatma
- Hech qanday markdown ishlatma
- Hech qanday izoh, ro‘yxat yoki maslahat qo‘shma

ICHKI QAROR LOGIKASI

1. Agar ish bevosita harom narsaga xizmat qilsa → "haram"
2. Agar ish toza ekani aniq isbotlansa → "halol"
3. Agar tafsilot yetarli bo‘lmasa yoki xavf bo‘lsa → "unclear"

ENG MUHIM TAMOYIL

Shubhali daromadni halol deb chiqarishdan ko‘ra, ehtiyotkorlik bilan "unclear" yoki kerak bo‘lsa "haram" deyish afzal.

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