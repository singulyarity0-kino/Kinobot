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

**Avto-tarjima.** TMDB tavsifi (ruscha/inglizcha) o'zbekchaga o'giriladi. Standart: Google (bepul, kalitsiz); `ANTHROPIC_API_KEY` yozsangiz Claude (sifatliroq). Admin panel → 💎 VIP voronka → 🌐 tugmasi bilan o'chiriladi.

**Til/sifat variantlari.** Kino sahifasi → 🎙 Til/sifat variantlari: har variant alohida video
(🇺🇿 O'zbekcha · 1080p). Foydalanuvchi o'zi tanlaydi.

**Qidiruv.** Menyusiz: kod (`1234`) yoki nom yozing. Nom, rejissyor, aktyor bo'yicha; xato yozilgan nomni ham topadi.
**Ro'yxatlar.** 🏆 Top 10 (30 kun ko'rilishi), 🎟 Premyeralar (30 kun), 🎞 To'plamlar (admin yaratadi).
**Foydalanuvchi.** 📝 Kino so'rash (kuniga 3 ta), ⚠️ Muammo haqida xabar.

## 💎 VIP voronka (⚙️ Admin panel → 💎 VIP voronka)
- ⏳ **Erta kirish:** yangi kino VIP'ga darhol, oddiylarga N soatdan keyin (standart 72, 0 = hammaga darhol).
  Oddiylar kartani ko'radi, tugmada qolgan vaqt va «VIP bilan hoziroq» chiqadi. Alohida kinoni
  «🎬 Rejissyor/aktyor… → ⏳ Erta kirish» orqali hammaga darhol ochish mumkin.
  Hisob kino **e'lon qilingan** paytdan boshlanadi (e'lon qilinmagan bo'lsa — qo'shilgan paytdan).
- 📀 **Sifat:** variant videosining va oddiy kino/qism videosining sifatini bot o'lchamidan o'zi aniqlaydi (kartadagi «📀 Sifat» qatori ham o'zi to'ladi) (480/720/1080/2160).
  Oddiylar uchun eng yuqori sifat sozlanadi (standart 720p); undan yuqorisi 🔒 VIP.
- 🎟 Kunlik bepul limit (standart 2; 0 = cheksiz). Bir kinoni qayta ochish limitga qo'shilmaydi.
- 🎁 Yangi foydalanuvchiga bepul VIP sinov (standart 1 kun, bir marta).
- 🔒 VIP/yangi kino kartasi qulflanadi: poster/tavsif/treyler ko'rinadi, video yo'q.
- Narx xabarida «kuniga necha so'm»; 🎉 qayta xaridga chegirma; ⏳ tugashiga 3 va 1 kun qolganda eslatma.
- 📈 Kino statistikasi: eng ko'p ko'rilgan, tanlangan sifatlar, **qaysi kino VIP sotishga sabab bo'lgani**;
  oddiy statistikada VIP daromad.

## 📣 Bir nechta kanal (Admin panel → 📣 E'lon kanallari)
- Kanalni ulash/uzish admin paneldan: `@username`, `-100…` ID yoki kanaldan forward. Bot kanalda admin
  bo'lishi shart (ulashda tekshiriladi). Eski `MAIN_CHANNEL` birinchi kanal bo'lib avtomatik o'tadi.
- **Yangi kino e'loni** (kino sahifasi → «📢 Hozir e'lon qilish») barcha ✅ kanallarga boradi;
  reyting o'zgarganda hamma kanaldagi post yangilanadi.
- **Kunlik random:** har kanalning o'z vaqti va yoqish/o'chirish tugmasi. Bir kunda ikki kanalga bir xil kino
  chiqmaydi; shu kanalga oxirgi 30 kunda yuborilgan kino qaytmaydi. «🧪 Sinov» tugmasi bor.
- **🕐 Rejalashtirilgan post:** kino sahifasi → «🕐 Kanalga rejalashtirish» (bugun/ertaga/istalgan vaqt).

## 🔔 Janrga obuna
Foydalanuvchi menyudan janrlarni belgilaydi. Yangi kino chiqqanda VIP obunachilarga darhol,
oddiylarga kechikish tugagach xabar boradi.

## 🤖 Gemini (ixtiyoriy, `GEMINI_API_KEY`)
Tavsifni o'zbekcha qiziqarli qilib yozadi, kanal posti uchun qisqa jumla yaratadi, qo'lda qo'shilgan kinoga
janr taklif qiladi (kino sahifasi → «🎬 Rejissyor… → 🤖 Janrni taklif qilish»), tarjima zanjirida birinchi turadi.
Kalit bo'lmasa yoki limit tugasa bot to'xtamaydi: Google → MyMemory → asl tavsif.

## 💳 Avtomatik to'lov (Admin panel → 💎 VIP voronka)
- **Karta (so'm):** BotFather → Payments orqali provayder (masalan Click) ulanadi, tokenni `PAYMENT_PROVIDER_TOKEN`
  ga yozasiz. To'lov tushishi bilan VIP o'zi yoqiladi. Provayder bilan shartnoma kerak.
- **⭐ Telegram Stars:** token kerak emas; admin paneldan har tarif uchun yulduz narxi belgilanadi (0 = o'chiq).
- **Qo'lda usul** (kartaga o'tkazib chek yuborish) saqlanadi; avtomatik usul ishlagach o'chirib qo'yish mumkin.
- Har to'lov bir marta hisoblanadi (takroriy xabar VIP'ni ikki marta bermaydi); xato bo'lsa adminlarga xabar boradi.

## 🛡 Obuna tekshiruvi
Majburiy kanallar natijasi 90 soniya keshlanadi (har xabarda kanallarga so'rov ketmaydi).
«✅ Tekshirish» tugmasi keshni chetlab o'tadi.

## Render uchun muhit o'zgaruvchilari
`BOT_TOKEN`, `SUPER_ADMIN_IDS`, `TMDB_API_KEY`, `GEMINI_API_KEY` (ixtiyoriy), `DB_PATH` (doimiy disk bo'lsa).
Render'ning bepul rejasida fayl tizimi doimiy emas: `kino.db` qayta ishga tushganda yo'qolishi mumkin —
doimiy disk (Disk) ulang yoki zaxira kanalga avtomatik backup'ga tayaning.

## Hali qilinmagan
18+ yosh tasdiqlash, kontent menejeri roli, Mini App,
kino qismlarga bo'lingan variantlar (hozir har qism alohida variant sifatida qo'shiladi).
Telegram fayl limiti: 2 GB (Premium 4 GB).
