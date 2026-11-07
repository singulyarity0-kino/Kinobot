"""
Kino qo'shishda TMDB'dan avtomatik to'ldirish (admin uchun).

Oqim: nom kiritiladi -> [🔎 TMDB'dan to'ldirish] / [✍️ Qo'lda kiritish]
      TMDB: natijalar ro'yxati -> bittasini tanlaydi -> tavsif, poster, janr, yil, davlat, davomiylik,
      yosh chegarasi, reyting, rejissyor, aktyorlar va treyler avtomatik olinadi
      -> admin janrlarni tekshiradi (✅ Tayyor) -> til va subtitr tanlanadi -> kino saqlanadi.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import gemini
import tmdb
import translate
import keyboards as kb
from database import Database
from handlers.common import IsAdminFilter
from states import AddAnimeStates

logger = logging.getLogger("kino_bot.tmdb_ui")
router = Router(name="tmdb_ui")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

STATES_OK = (AddAnimeStates.waiting_source, AddAnimeStates.waiting_al_query)


async def _continue_manual(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAnimeStates.waiting_description)
    await message.answer("📝 Kino tavsifini kiriting (yoki «-» deb yozing, tavsifsiz qo'shish uchun):")


async def _run_search(message: Message, state: FSMContext, query: str) -> None:
    wait = await message.answer(f"🔎 TMDB'da «{html.escape(query)}» qidirilmoqda...")
    try:
        results = await tmdb.search(query, limit=5)
    except Exception as e:
        logger.warning("TMDB qidiruvi xato: %s", e)
        await wait.edit_text(
            f"❌ TMDB xatosi: <code>{html.escape(str(e) or type(e).__name__)}</code>\n\n"
            "Qo'lda kiritishingiz yoki sozlamani to'g'rilab qayta urinib ko'rishingiz mumkin.",
            reply_markup=kb.anilist_retry_keyboard(),
        )
        await state.set_state(AddAnimeStates.waiting_source)
        return
    if not results:
        await wait.edit_text(
            f"😕 «{html.escape(query)}» bo'yicha hech narsa topilmadi. "
            "Inglizcha (asl) nomini yozib ko'ring.",
            reply_markup=kb.anilist_retry_keyboard(),
        )
        await state.set_state(AddAnimeStates.waiting_source)
        return
    await state.update_data(al_results={str(r["id"]): r for r in results})
    await state.set_state(AddAnimeStates.waiting_source)
    await wait.edit_text(
        "Mos keladiganini tanlang:",
        reply_markup=kb.anilist_results_keyboard([(r["id"], tmdb.button_label(r)) for r in results]),
    )


@router.callback_query(AddAnimeStates.waiting_source, F.data == "al:search")
async def al_search(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await call.answer()
    await _run_search(call.message, state, data.get("new_title", ""))


@router.callback_query(AddAnimeStates.waiting_source, F.data == "al:retry")
async def al_retry(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddAnimeStates.waiting_al_query)
    await call.message.answer("✍️ Kino nomini (inglizcha, asl nomi bilan yaxshiroq topiladi) yozing:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AddAnimeStates.waiting_al_query)
async def al_query_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("✏️ Iltimos, nomni matn ko'rinishida yuboring.")
        return
    await _run_search(message, state, message.text.strip())


@router.callback_query(AddAnimeStates.waiting_source, F.data == "al:manual")
async def al_manual(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _continue_manual(call.message, state)


@router.message(AddAnimeStates.waiting_source)
async def al_source_wrong(message: Message):
    await message.answer(
        "👆 Iltimos, yuqoridagi tugmalardan birini tanlang: TMDB'dan to'ldirish yoki qo'lda kiritish.",
        reply_markup=kb.anilist_source_keyboard(),
    )


async def _poster_file_id(bot: Bot, chat_id: int, url: str) -> Optional[str]:
    """Plakat rasmini Telegramga yuklab, file_id oladi (admin ko'rishi uchun chatga ham tushadi)."""
    if not url:
        return None
    try:
        msg = await bot.send_photo(chat_id, url, caption="🖼 TMDB posteri (kino kartasida shu rasm bo'ladi)")
        return msg.photo[-1].file_id
    except Exception as e:
        logger.warning("Plakatni yuklab bo'lmadi: %s", e)
        return None


@router.callback_query(AddAnimeStates.waiting_source, F.data.startswith("al:pick:"))
async def al_pick(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    al_id = call.data.split(":")[2]
    data = await state.get_data()
    item = (data.get("al_results") or {}).get(al_id)
    if not item:
        await call.answer("Natija eskirgan. Qaytadan qidiring.", show_alert=True)
        return
    await call.answer()
    chat_id = call.message.chat.id

    poster = await _poster_file_id(bot, chat_id, item["cover_url"])
    details = await tmdb.get_details(item)

    genre_names = details.get("genres") or item["genres"]
    genres = await db.list_genres()
    genre_ids = tmdb.match_genre_ids(genre_names, genres)
    description = item["description"] or details.get("description") or ""
    country = details.get("country") or item["country"]

    # Tavsif: avval Gemini (o'zbekcha qiziqarli matn, bitta so'rovda), bo'lmasa tarjima zanjiri
    translated_ok = rewritten = False
    if description and await db.get_setting("ai_rewrite_description", "1") == "1" and gemini.is_configured():
        out = await gemini.rewrite_description(item["title"], item["year"], html.unescape(description), genre_names)
        if out:
            description, rewritten = html.escape(out, quote=False), True
    if description and not rewritten and await db.get_setting("translate_description", "1") == "1":
        uz = await translate.to_uzbek_escaped(description)
        if uz:
            description, translated_ok = uz, True

    extra = {
        "kind": item["kind"],
        "tmdb_id": item["id"],
        **{k: details[k] for k in ("duration", "age_rating", "imdb_rating", "director", "actors", "trailer_url")
           if details.get(k)},
    }
    if not extra.get("imdb_rating") and item.get("rating"):
        extra["imdb_rating"] = item["rating"]

    await state.update_data(
        anilist_id=None,
        al_extra=extra,
        new_description=description,
        new_poster=poster,
        al_country=country,
        al_year=item["year"] or None,
        selected_genres=genre_ids,
        al_results=None,
    )

    names = {g["id"]: g["name"] for g in genres}
    lines = [f"✅ «{html.escape(item['title'])}» ma'lumotlari TMDB'dan olindi ({item['format']}):"]
    if description:
        if rewritten:
            lines.append("📖 Tavsif: Gemini o'zbekcha qilib yozdi 🤖")
        elif translated_ok:
            lines.append("📖 Tavsif: o'zbekchaga tarjima qilindi")
        else:
            lines.append(f"📖 Tavsif: {len(description)} belgi (tarjimasiz)")
    else:
        lines.append("📖 Tavsif: topilmadi")
    lines.append("🖼 Poster: " + ("olindi" if poster else "olib bo'lmadi (keyin «🖼 Poster» orqali qo'shing)"))
    lines.append("📅 Yil: " + (item["year"] or "—") + " · 🌍 Davlat: " + (country or "tanlanadi"))
    if extra.get("duration"):
        lines.append(f"⏱ Davomiylik: {extra['duration']}")
    if extra.get("age_rating"):
        lines.append(f"🔞 Yosh chegarasi: {extra['age_rating']}")
    if extra.get("director"):
        lines.append(f"🎬 Rejissyor: {html.escape(extra['director'])}")
    if extra.get("actors"):
        lines.append(f"👥 Aktyorlar: {html.escape(extra['actors'])}")
    if extra.get("trailer_url"):
        lines.append("🎞 Treyler: topildi")
    if genre_ids:
        lines.append("🎭 Janr: " + ", ".join(names[i] for i in genre_ids))
    lines.append("")
    lines.append(
        "ℹ️ Avto-tarjima aniq bo'lmasligi mumkin — kerak bo'lsa, kino saqlangandan keyin "
        "«✏️ Tavsifni tahrirlash» orqali to'g'rilang."
    )
    await bot.send_message(chat_id, "\n".join(lines))

    await state.set_state(AddAnimeStates.waiting_genres)
    await bot.send_message(
        chat_id,
        "🎭 Janrlarni tekshiring (kerak bo'lsa o'zgartiring), so'ng ✅ Tayyor bosing:",
        reply_markup=kb.genres_menu(genres, selected_ids=set(genre_ids), for_admin_add=True),
    )
