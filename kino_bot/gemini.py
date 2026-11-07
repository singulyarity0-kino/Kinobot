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


async def _generate(prompt: str, system: str, max_tokens: int = 600, temperature: float = 0.6) -> Optional[str]:
    if not is_configured():
        return None
    model = _model()
    gen_cfg: dict = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if "2.5-flash" in model:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}  # fikrlash tokenlari javobni qisqartirib yubormasin
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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
