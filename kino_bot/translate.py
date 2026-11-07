"""
Kino tavsifini avtomatik o'zbekchaga (lotin) tarjima qilish.

Tarjimon tanlovi (.env: TRANSLATE_PROVIDER):
  auto     — zanjir: Gemini (GEMINI_API_KEY bo'lsa) → Claude (ANTHROPIC_API_KEY bo'lsa) → Google → MyMemory  [standart]
  gemini / claude / google / mymemory — faqat bittasi
  off      — tarjima o'chiq

Tarjima ishlamasa bot to'xtamaydi: asl (ruscha/inglizcha) tavsif saqlanadi.
"""
from __future__ import annotations

import html
import logging
import os
import re
from typing import Optional

import aiohttp

logger = logging.getLogger("kino_bot.translate")

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_SYSTEM = (
    "You translate movie and TV descriptions into natural, fluent Uzbek written in the Latin alphabet "
    "(O'zbek tili, lotin yozuvi). Keep names of people, films and places as they are commonly written. "
    "Reply with the translation only: no quotes, no comments, no explanations."
)


def provider() -> str:
    """off | auto (zanjir) | gemini | claude | google | mymemory — .env: TRANSLATE_PROVIDER."""
    p = os.getenv("TRANSLATE_PROVIDER", "auto").strip().lower()
    return p if p in ("auto", "gemini", "claude", "google", "mymemory", "off") else "auto"


async def _google(text: str) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            GOOGLE_URL,
            params={"client": "gtx", "sl": "auto", "tl": "uz", "dt": "t"},
            data={"q": text},
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Google Translate HTTP {resp.status}")
            data = await resp.json(content_type=None)
    parts = [seg[0] for seg in (data[0] or []) if seg and seg[0]]
    return "".join(parts).strip() or None


async def _claude(text: str) -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model = os.getenv("TRANSLATE_CLAUDE_MODEL", "claude-haiku-5-5").strip()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            CLAUDE_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": model,
                "max_tokens": 1000,
                "system": CLAUDE_SYSTEM,
                "messages": [{"role": "user", "content": text}],
            },
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Claude API HTTP {resp.status}")
            data = await resp.json()
    out = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return out.strip() or None


async def _mymemory(text: str) -> Optional[str]:
    """MyMemory: bepul, kalitsiz; so'rovga ~500 belgi. Matnni gaplar bo'yicha bo'laklab yuboramiz."""
    chunks: list[str] = []
    cur = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(cur) + len(sentence) + 1 > 450 and cur:
            chunks.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        chunks.append(cur)
    timeout = aiohttp.ClientTimeout(total=15)
    out: list[str] = []
    email = os.getenv("MYMEMORY_EMAIL", "").strip()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for ch in chunks:
            params = {"q": ch[:480], "langpair": "autodetect|uz"}
            if email:
                params["de"] = email
            async with session.get("https://api.mymemory.translated.net/get", params=params) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"MyMemory HTTP {resp.status}")
                data = await resp.json(content_type=None)
            if str(data.get("responseStatus")) != "200":
                raise RuntimeError(f"MyMemory: {data.get('responseDetails') or data.get('responseStatus')}")
            part = (data.get("responseData") or {}).get("translatedText")
            if not part:
                raise RuntimeError("MyMemory bo'sh javob berdi")
            out.append(part)
    return " ".join(out).strip() or None


async def _gemini(text: str) -> Optional[str]:
    import gemini  # kech import: Gemini kaliti bo'lmasa ham bu modul ishlayveradi
    return await gemini.translate(text)


_PROVIDERS = {"gemini": _gemini, "claude": _claude, "google": _google, "mymemory": _mymemory}


def _chain() -> list[str]:
    prov = provider()
    if prov == "off":
        return []
    if prov != "auto":
        return [prov]
    chain = []
    if os.getenv("GEMINI_API_KEY", "").strip():
        chain.append("gemini")
    if os.getenv("ANTHROPIC_API_KEY", "").strip():
        chain.append("claude")
    return chain + ["google", "mymemory"]


async def to_uzbek(text: str) -> Optional[str]:
    """
    Oddiy matnni o'zbekchaga o'giradi. Zanjir: Gemini → Claude (kalit bo'lsa) → Google → MyMemory.
    Birinchi ishlagani qaytariladi; hammasi xato bersa None (asl matn saqlanadi).
    """
    text = (text or "").strip()
    if not text:
        return None
    for name in _chain():
        try:
            out = await _PROVIDERS[name](text)
        except Exception as e:  # noqa: BLE001 — keyingi xizmatga o'tamiz
            logger.warning("Tarjima xatosi (%s): %s", name, e)
            continue
        if out:
            return out
    return None


async def to_uzbek_escaped(escaped_text: str) -> Optional[str]:
    """
    TMDB tavsifi HTML-escape qilingan holda saqlanadi (&amp; kabi). Avval oddiy matnga qaytarib,
    tarjima qilib, yana escape qiladi.
    """
    plain = html.unescape(escaped_text or "")
    out = await to_uzbek(plain)
    return html.escape(out, quote=False) if out else None
