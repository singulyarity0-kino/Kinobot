"""
Gemini (Google AI Studio) — bepul kalit bilan:
  * tavsifni o'zbekchaga tarjima qilish va qiziqarli qilib qayta yozish (bitta so'rovda)
  * kanal posti uchun qisqa jozibali jumla
  * qo'lda qo'shilgan kino uchun janr taklifi

Sozlash (.env):
  GEMINI_API_KEY=...            https://aistudio.google.com dan bepul kalit
  GEMINI_MODEL=gemini-2.5-flash-lite   (standart; bepul limiti eng yuqori model)

Kalit yo'q yoki xato bo'lsa funksiyalar None qaytaradi — bot to'xtamaydi, boshqa usullarga o'tadi.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("kino_bot.gemini")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def _model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").strip() or "gemini-2.5-flash-lite"


def is_configured() -> bool:
    return bool(_key())


async def _generate(prompt: str, system: str, max_tokens: int = 600, temperature: float = 0.6,
                    image: Optional[tuple[bytes, str]] = None, json_mode: bool = False) -> Optional[str]:
    """image = (baytlar, mime) — rasm bilan so'rov (chek tekshiruvi); json_mode — faqat JSON javob."""
    if not is_configured():
        return None
    model = _model()
    gen_cfg: dict = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    if "2.5-flash" in model:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # fikrlash tokenlari javobni qisqartirib yubormasin
    user_parts: list = []
    if image is not None:
        user_parts.append({"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode("ascii")}})
    user_parts.append({"text": prompt})
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": gen_cfg,
    }
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                API.format(model=model), json=body,
                headers={"x-goog-api-key": _key(), "content-type": "application/json"},
            ) as resp:
                if resp.status == 429:
                    raise RuntimeError("Gemini limiti tugadi (429)")
                if resp.status in (400, 403):
                    raise RuntimeError(f"Gemini kaliti/so'rovi noto'g'ri ({resp.status})")
                if resp.status != 200:
                    raise RuntimeError(f"Gemini HTTP {resp.status}")
                data = await resp.json()
    except Exception as e:  # noqa: BLE001 — Gemini ixtiyoriy, xatoda boshqa usullarga o'tiladi
        logger.warning("Gemini xatosi: %s", e)
        return None
    parts = (((data.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    return text or None


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


_SYS_UZ = (
    "You are an editor of a Uzbek movie Telegram bot. Write natural, fluent Uzbek in the Latin alphabet "
    "(O'zbek tili, lotin yozuvi). Keep film, person and place names as commonly written. "
    "Never reveal spoilers. Output only the requested text: no quotes, no markdown, no explanations."
)


async def translate(text: str) -> Optional[str]:
    """Matnni o'zbekchaga (lotin) o'giradi. Ichki format: oddiy matn."""
    out = await _generate(f"Translate into Uzbek (Latin):\n\n{text}", _SYS_UZ, max_tokens=900, temperature=0.2)
    return out


async def rewrite_description(title: str, year: str, text: str, genres: list[str]) -> Optional[str]:
    """Asl tavsifni (istalgan tilda) 2–3 gaplik qiziqarli, spoilersiz o'zbekcha matnga aylantiradi."""
    prompt = (
        f"Film: {title} ({year or 'yil noma\'lum'}). Janrlar: {', '.join(genres) or '—'}.\n"
        f"Original description:\n{text}\n\n"
        "Write a catchy, engaging description in Uzbek, 2-3 sentences, at most 380 characters. "
        "Start with the most intriguing idea. No spoilers, no hashtags, no emojis."
    )
    out = await _generate(prompt, _SYS_UZ, max_tokens=500, temperature=0.7)
    return _cut(out, 420)


async def hook_line(title: str, text: str) -> Optional[str]:
    """Kanal posti tepasidagi 1 ta qisqa jozibali jumla (maks 110 belgi)."""
    prompt = (
        f"Film: {title}\nDescription: {text}\n\n"
        "Write ONE short, catchy hook sentence in Uzbek for a Telegram channel post (max 100 characters). "
        "No hashtags, no emojis, no quotes, no spoilers."
    )
    out = await _generate(prompt, _SYS_UZ, max_tokens=120, temperature=0.9)
    if not out:
        return None
    out = out.strip().splitlines()[0].strip().strip('"«»“”').strip()
    return _cut(out, 110)


async def suggest_genres(title: str, text: str, allowed: list[str], limit: int = 3) -> list[str]:
    """Faqat ruxsat etilgan janr nomlaridan eng mosini tanlaydi."""
    prompt = (
        f"Film: {title}\nDescription: {text}\n\n"
        f"Allowed genres: {json.dumps(allowed, ensure_ascii=False)}\n"
        f"Pick the {limit} most fitting genres from the allowed list only. "
        'Reply with a JSON array of exact genre names, for example ["Drama","Triller"].'
    )
    out = await _generate(prompt, "You classify movies. Reply with JSON only.", max_tokens=120, temperature=0.1)
    if not out:
        return []
    m = re.search(r"\[.*?\]", out, flags=re.S)
    if not m:
        return []
    try:
        picked = json.loads(m.group(0))
    except ValueError:
        return []
    by_lower = {a.lower(): a for a in allowed}
    result: list[str] = []
    for g in picked:
        name = by_lower.get(str(g).strip().lower())
        if name and name not in result:
            result.append(name)
    return result[:limit]


def _json_obj(text: Optional[str]) -> Optional[dict]:
    """Javobdan birinchi JSON obyektni ajratib oladi (```json qobig'i bo'lsa ham)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


async def verify_receipt(image: bytes, mime: str, expected_amount: int, card_number: str, card_holder: str) -> Optional[dict]:
    """
    To'lov cheki rasmini o'qiydi. Qaytadi (yoki None — Gemini ishlamasa):
    {is_receipt, success, amount, card_tail, date, confidence, notes}
    """
    prompt = (
        "This image should be a payment receipt / transfer screenshot from an Uzbek banking or payment app "
        "(Click, Payme, Uzum, Humo, UzCard, etc.). Read it carefully and reply with ONE JSON object only:\n"
        '{"is_receipt": true|false, "success": true|false|null, "amount": integer sum in UZS or null, '
        '"card_tail": last 4 digits of the RECIPIENT card as string or null, "date": "YYYY-MM-DD" or null, '
        '"confidence": number 0..1, "notes": "short reason in Uzbek, max 120 chars"}\n'
        "Rules: amount is the transferred amount in so'm as a plain integer (no spaces, no tiyin). "
        "is_receipt is false for anything that is not a real payment receipt (selfie, meme, random screenshot, "
        "edited or suspicious image). success is false if the status shows failed/cancelled/pending. "
        f"Context: expected amount {expected_amount}, expected recipient card ending {str(card_number)[-4:] or 'unknown'}, "
        f"holder '{card_holder}'. Do NOT assume the expected values; report only what is visible."
    )
    out = await _generate(
        prompt, "You are a careful payment receipt reader. Reply with JSON only.",
        max_tokens=300, temperature=0.0, image=(image, mime), json_mode=True,
    )
    return _json_obj(out)


async def describe_movie(title: str, allowed_genres: list[str]) -> Optional[dict]:
    """
    Nom bo'yicha kino ma'lumotlarini taklif qiladi (TMDB o'rniga). Natija TAXMINIY — admin tasdiqlaydi.
    {title, year, country, director, actors, duration, age_rating, description, genres}
    """
    prompt = (
        f'Movie or series title given by the admin: "{title}".\n'
        "If you do not confidently recognise this exact title, reply with {\"unknown\": true}.\n"
        "Otherwise reply with ONE JSON object only:\n"
        '{"title": "original title", "year": "YYYY", "country": "main country in Uzbek (Latin)", '
        '"director": "name", "actors": "up to 4 main actors, comma separated", '
        '"duration": "in Uzbek, e.g. 2 soat 15 daqiqa", "age_rating": one of "0+","6+","12+","16+","18+", '
        '"description": "engaging Uzbek (Latin) description, 2-3 sentences, max 380 chars, no spoilers", '
        f'"genres": up to 3 names chosen ONLY from {json.dumps(allowed_genres, ensure_ascii=False)}, '
        '"is_series": true|false}\n'
        "Never invent facts: leave a field as an empty string if unsure."
    )
    out = await _generate(prompt, _SYS_UZ, max_tokens=700, temperature=0.2, json_mode=True)
    obj = _json_obj(out)
    if not obj or obj.get("unknown"):
        return None
    by_lower = {a.lower(): a for a in allowed_genres}
    genres: list[str] = []
    for g in obj.get("genres") or []:
        name = by_lower.get(str(g).strip().lower())
        if name and name not in genres:
            genres.append(name)
    obj["genres"] = genres[:3]
    if obj.get("age_rating") not in ("0+", "6+", "12+", "16+", "18+"):
        obj["age_rating"] = ""
    obj["description"] = _cut(str(obj.get("description") or ""), 420) or ""
    return obj


def _cut(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > limit * 0.5 else cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return text


def escaped(text: Optional[str]) -> Optional[str]:
    """Telegram HTML uchun escape (kartada ishlatiladi)."""
    return _esc(text) if text else None
