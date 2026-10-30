"""
TMDB (https://www.themoviedb.org) dan kino/serial ma'lumotlarini olish: qidiruv va to'liq ma'lumot
(tavsif, plakat, janr, yil, davlat, davomiylik, yosh chegarasi, reyting, rejissyor, aktyorlar, treyler).

Sozlash (.env):
  TMDB_API_KEY=...          (v3 API kalit)  YOKI
  TMDB_READ_TOKEN=...       (v4 "API Read Access Token")
  TMDB_LANGUAGE=ru-RU       (tavsif tili; o'zbekcha tavsif TMDB'da kam, ruscha yoki en-US qulay)

Kalit bo'lmasa yoki internet ishlamasa bot to'xtamaydi — faqat avto-to'ldirish ishlamaydi (qo'lda kiritish bor).
"""
from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("kino_bot.tmdb")

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w780"

API_KEY = os.getenv("TMDB_API_KEY", "").strip()
READ_TOKEN = os.getenv("TMDB_READ_TOKEN", "").strip()
LANGUAGE = os.getenv("TMDB_LANGUAGE", "ru-RU").strip() or "ru-RU"


def is_configured() -> bool:
    return bool(API_KEY or READ_TOKEN)


# TMDB janr ID -> botdagi janr nomlari (config.NORMAL_GENRES dagi nomlar bilan bir xil bo'lishi kerak)
GENRE_IDS: dict[int, list[str]] = {
    28: ["Jangari"], 12: ["Sarguzasht"], 16: ["Multfilm"], 35: ["Komediya"], 80: ["Kriminal"],
    99: ["Hujjatli"], 18: ["Drama"], 10751: ["Oilaviy"], 14: ["Fantastika"], 36: ["Tarixiy"],
    27: ["Qo'rqinchli"], 10402: ["Musiqiy"], 9648: ["Detektiv"], 10749: ["Melodrama"],
    878: ["Ilmiy fantastika"], 53: ["Triller"], 10752: ["Urush"], 37: ["Vestern"],
    # serial janrlari
    10759: ["Jangari", "Sarguzasht"], 10762: ["Oilaviy"], 10765: ["Ilmiy fantastika", "Fantastika"],
    10766: ["Melodrama"], 10768: ["Urush"],
}

COUNTRY_NAMES: dict[str, str] = {
    "US": "AQSh", "IN": "Hindiston", "TR": "Turkiya", "KR": "Koreya", "RU": "Rossiya", "UZ": "O'zbekiston",
    "JP": "Yaponiya", "GB": "Buyuk Britaniya", "FR": "Fransiya", "DE": "Germaniya", "IT": "Italiya",
    "ES": "Ispaniya", "CN": "Xitoy", "CA": "Kanada", "AU": "Avstraliya", "BR": "Braziliya", "KZ": "Qozog'iston",
    "UA": "Ukraina", "PL": "Polsha", "SE": "Shvetsiya", "MX": "Meksika", "HK": "Gonkong", "TH": "Tailand",
}
EUROPE = {"GB", "FR", "DE", "IT", "ES", "PL", "SE", "UA"}

CERT_TO_AGE: dict[str, str] = {
    "G": "0+", "PG": "6+", "PG-13": "12+", "R": "16+", "NC-17": "18+",
    "TV-Y": "0+", "TV-Y7": "6+", "TV-G": "0+", "TV-PG": "12+", "TV-14": "16+", "TV-MA": "18+",
}


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if READ_TOKEN:
        h["Authorization"] = f"Bearer {READ_TOKEN}"
    return h


async def _get(path: str, params: Optional[dict[str, Any]] = None, timeout: float = 8.0) -> dict[str, Any]:
    if not is_configured():
        raise RuntimeError("TMDB kaliti sozlanmagan (.env: TMDB_API_KEY yoki TMDB_READ_TOKEN)")
    q = dict(params or {})
    if API_KEY and not READ_TOKEN:
        q["api_key"] = API_KEY
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.get(f"{API}{path}", params=q, headers=_headers()) as resp:
            if resp.status == 401:
                raise RuntimeError("TMDB kaliti noto'g'ri (401)")
            if resp.status != 200:
                raise RuntimeError(f"TMDB HTTP {resp.status}")
            return await resp.json()


def clean_description(raw: Optional[str], limit: int = 600) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > limit * 0.5 else cut.rstrip() + "…"
    return html.escape(text.strip(), quote=False)


def format_runtime(minutes: Optional[int]) -> str:
    if not minutes or minutes <= 0:
        return ""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} soat {m} daqiqa"
    if h:
        return f"{h} soat"
    return f"{m} daqiqa"


def _genres_from_ids(ids: list[int]) -> list[str]:
    out: list[str] = []
    for gid in ids:
        for name in GENRE_IDS.get(int(gid), []):
            if name not in out:
                out.append(name)
    return out


def _country_from_codes(codes: list[str]) -> Optional[str]:
    for code in codes:
        if code in COUNTRY_NAMES:
            return COUNTRY_NAMES[code]
    return None


def _extra_sections(kind: str, codes: list[str], genres: list[str]) -> list[str]:
    """Bo'limlar: Hind kinolari / Turk seriallari / Koreys dramalari."""
    extra = []
    if "IN" in codes:
        extra.append("Hind kinolari")
    if kind == "series" and "TR" in codes:
        extra.append("Turk seriallari")
    if kind == "series" and "KR" in codes:
        extra.append("Koreys dramalari")
    return [e for e in extra if e not in genres]


def normalize(m: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Qidiruv natijasidagi bitta yozuvni botning ichki ko'rinishiga o'tkazadi (movie/tv bo'lmasa None)."""
    media_type = m.get("media_type")
    if media_type not in ("movie", "tv"):
        return None
    kind = "movie" if media_type == "movie" else "series"
    title = m.get("title") or m.get("name") or m.get("original_title") or m.get("original_name") or "—"
    date = m.get("release_date") or m.get("first_air_date") or ""
    codes = list(m.get("origin_country") or [])
    genres = _genres_from_ids(m.get("genre_ids") or [])
    genres += _extra_sections(kind, codes, genres)
    poster = m.get("poster_path")
    return {
        "id": int(m["id"]),
        "media_type": media_type,
        "kind": kind,
        "title": title,
        "original_title": m.get("original_title") or m.get("original_name") or "",
        "year": date[:4] if date else "",
        "format": "Serial" if kind == "series" else "Kino",
        "genres": genres,
        "country": _country_from_codes(codes),
        "country_codes": codes,
        "description": clean_description(m.get("overview")),
        "cover_url": f"{IMG}{poster}" if poster else "",
        "rating": f"{m['vote_average']:.1f}" if m.get("vote_average") else "",
    }


async def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    data = await _get("/search/multi", {"query": query, "language": LANGUAGE, "include_adult": "false"})
    items = [normalize(m) for m in (data.get("results") or [])]
    items = [i for i in items if i]
    # Tavsifi bo'sh bo'lsa (ruscha tarjima yo'q) — inglizcha bilan to'ldiramiz
    if any(not i["description"] for i in items[:limit]) and LANGUAGE != "en-US":
        try:
            en = await _get("/search/multi", {"query": query, "language": "en-US", "include_adult": "false"})
            en_by_id = {(m.get("media_type"), m.get("id")): m for m in (en.get("results") or [])}
            for i in items:
                if not i["description"]:
                    m = en_by_id.get((i["media_type"], i["id"]))
                    if m:
                        i["description"] = clean_description(m.get("overview"))
        except Exception as e:  # noqa: BLE001 — qo'shimcha urinish, xato bo'lsa jim o'tamiz
            logger.info("TMDB inglizcha tavsif olinmadi: %s", e)
    return items[:limit]


async def get_details(item: dict[str, Any]) -> dict[str, Any]:
    """
    Tanlangan kino/serial uchun to'liq ma'lumot: davomiylik, yosh chegarasi, rejissyor, aktyorlar, treyler.
    Xato bo'lsa bo'sh dict qaytaradi (asosiy ma'lumot baribir saqlanadi).
    """
    path = f"/{item['media_type']}/{item['id']}"
    try:
        data = await _get(
            path,
            {
                "language": LANGUAGE,
                "append_to_response": "credits,videos,release_dates,content_ratings",
                "include_video_language": "en,ru,null",
            },
            timeout=10.0,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("TMDB tafsilot olinmadi: %s", e)
        return {}

    out: dict[str, Any] = {}

    # Davomiylik
    runtime = data.get("runtime")
    if not runtime:
        ert = data.get("episode_run_time") or []
        runtime = ert[0] if ert else None
    if runtime:
        out["duration"] = format_runtime(runtime)
        if item["kind"] == "series":
            out["duration"] = f"{runtime} daqiqa (1 qism)"

    # Davlat (production_countries aniqroq)
    codes = [c.get("iso_3166_1") for c in (data.get("production_countries") or []) if c.get("iso_3166_1")]
    codes = codes or list(data.get("origin_country") or [])
    if codes:
        country = _country_from_codes(codes)
        if country:
            out["country"] = country
        if any(c in EUROPE for c in codes) and not out.get("country"):
            out["country"] = "Yevropa"
        out["country_codes"] = codes

    # Yosh chegarasi
    age = ""
    if item["media_type"] == "movie":
        for rel in (data.get("release_dates") or {}).get("results", []):
            if rel.get("iso_3166_1") == "US":
                for r in rel.get("release_dates", []):
                    cert = (r.get("certification") or "").strip()
                    if cert in CERT_TO_AGE:
                        age = CERT_TO_AGE[cert]
                        break
            if age:
                break
    else:
        for rt in (data.get("content_ratings") or {}).get("results", []):
            cert = (rt.get("rating") or "").strip()
            if rt.get("iso_3166_1") == "US" and cert in CERT_TO_AGE:
                age = CERT_TO_AGE[cert]
                break
    if age:
        out["age_rating"] = age

    # Rejissyor va aktyorlar
    credits = data.get("credits") or {}
    directors = [c["name"] for c in (credits.get("crew") or []) if c.get("job") == "Director"][:2]
    if not directors and item["media_type"] == "tv":
        directors = [c["name"] for c in (data.get("created_by") or [])][:2]
    if directors:
        out["director"] = ", ".join(directors)
    actors = [c["name"] for c in (credits.get("cast") or [])[:6] if c.get("name")]
    if actors:
        out["actors"] = ", ".join(actors)

    # Treyler (YouTube)
    vids = [v for v in (data.get("videos") or {}).get("results", []) if v.get("site") == "YouTube"]
    vids.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official", False)))
    if vids:
        out["trailer_url"] = f"https://www.youtube.com/watch?v={vids[0]['key']}"

    if data.get("vote_average"):
        out["imdb_rating"] = f"{data['vote_average']:.1f}"

    # Aniqroq janrlar (details'da to'liq ro'yxat bor) + bo'limlar
    gids = [g["id"] for g in (data.get("genres") or []) if g.get("id")]
    if gids:
        genres = _genres_from_ids(gids)
        genres += _extra_sections(item["kind"], out.get("country_codes", []), genres)
        out["genres"] = genres

    # Tavsif bo'sh bo'lsa — inglizchasi
    if not item.get("description") and data.get("overview"):
        out["description"] = clean_description(data["overview"])
    return out


def match_genre_ids(names: list[str], db_genres: list[Any]) -> list[int]:
    """TMDB'dan kelgan (allaqachon o'zbekcha) janr nomlarini bazadagi janrlarga moslaydi."""
    by_name = {str(g["name"]).strip().lower(): g["id"] for g in db_genres}
    result: list[int] = []
    for name in names:
        gid = by_name.get(name.strip().lower())
        if gid and gid not in result:
            result.append(gid)
    return result


def button_label(item: dict[str, Any]) -> str:
    parts = [item["title"]]
    extra = ", ".join(x for x in (item["year"], item["format"]) if x)
    if extra:
        parts.append(f"({extra})")
    label = " ".join(parts)
    return label if len(label) <= 60 else label[:57] + "…"
