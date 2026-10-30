# 🎬 Kino Bot

Kino va seriallar kutubxonasi, VIP obuna tizimi bilan Telegram bot (aiogram 3 + SQLite).
Anime botdan o'zgartirilgan: eski barcha imkoniyatlar (VIP, referal, zaxira, kashf qilish, broadcast) saqlangan.

## Ishga tushirish
1. `pip install -r requirements.txt`
2. `.env.example` → `.env`, `BOT_TOKEN`, `SUPER_ADMIN_IDS`, `MAIN_CHANNEL`, `TMDB_API_KEY` ni to'ldiring.
3. `python main_polling.py` (lokal) yoki `python main.py` (Render webhook, `RENDER_EXTERNAL_URL` kerak).

Baza endi `kino.db` (`DB_PATH`). Eski `anime.db` ni ishlatmoqchi bo'lsangiz `.env` da `DB_PATH=anime.db` yozing —
yangi ustunlar avtomatik qo'shiladi, eski ma'lumotlar saqlanadi.

## Yangi imkoniyatlar
**Kino ma'lumotlari.** TMDB'dan avto-to'ldirish (poster, tavsif, yil, davlat, davomiylik, yosh chegarasi, reyting,
rejissyor, aktyorlar, treyler). Kino sahifasida qo'shimcha maydonlar: 🎬 Rejissyor/aktyor/davomiylik… tugmasi.
Kino yoki serial turi (`kind`). Janrlar, davlatlar, kayfiyatlar (🎯 Kashf qilish) kinoga moslangan (`config.py`).

**Til/sifat variantlari.** Kino sahifasi → 🎙 Til/sifat variantlari: har variant alohida video
(🇺🇿 O'zbekcha · 1080p). Foydalanuvchi o'zi tanlaydi.

**Qidiruv.** Menyusiz: kod (`1234`) yoki nom yozing. Nom, rejissyor, aktyor bo'yicha; xato yozilgan nomni ham topadi.
**Ro'yxatlar.** 🏆 Top 10 (30 kun ko'rilishi), 🎟 Premyeralar (30 kun), 🎞 To'plamlar (admin yaratadi).
**Foydalanuvchi.** 📝 Kino so'rash (kuniga 3 ta), ⚠️ Muammo haqida xabar.

## 💎 VIP voronka (⚙️ Admin panel → 💎 VIP voronka)
- 🎟 Kunlik bepul limit (standart 2; 0 = cheksiz). Bir kinoni qayta ochish limitga qo'shilmaydi.
- 🎁 Yangi foydalanuvchiga bepul VIP sinov (standart 1 kun, majburiy obunadan keyin, bir marta).
- 🔒 VIP kino kartasi qulflanadi: poster/tavsif/treyler ko'rinadi, video yo'q.
- Narx xabarida «kuniga necha so'm»; 3 oylik tarif ⭐ bilan belgilanadi.
- 🎉 VIP'ni qayta olganlarga chegirma (standart 10%).
- ⏳ VIP tugashiga 3 va 1 kun qolganda eslatma (faqat pul to'lagan/admin bergan VIP'larga).
- 📊 Statistikada VIP daromad (bugun / 7 / 30 kun / jami).

## Admin panelga qo'shilganlar
💎 VIP voronka · 🎞 To'plamlar · 📝 So'rovlar (✅/❌ → foydalanuvchiga xabar) · ⚠️ Muammolar (✅ Tuzatildi → xabar).

## Cheklovlar (qilinmagan)
Click/Payme avtomatik to'lov (merchant kalitlari kerak), Telegram Mini App, balansdan kino ijarasi
(balansni to'ldirish usuli yo'q), FTS qidiruv indeksi. Variant videolari `file_id` bilan saqlanadi
(zaxira kanalga ko'chirish faqat oddiy qismlar uchun ishlaydi).
Telegram fayl limiti: 2 GB (Premium 4 GB).
