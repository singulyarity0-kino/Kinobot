"""
Test2 Bot — konfiguratsiya fayli.

Bot tokenni va boshqa maxfiy qiymatlarni .env faylidan o'qiydi.
Ishga tushirishdan oldin .env.example asosida .env fayl yarating.
"""
import datetime
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv o'rnatilmagan bo'lsa ham bot ishga tushaveradi,
    # lekin bu holda BOT_TOKEN ni muhit o'zgaruvchisi sifatida berish kerak.
    pass

# --- Majburiy sozlamalar ---
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Botni birinchi marta ishga tushirganda super-admin bo'lib kiradigan
# Telegram ID(lar). Vergul bilan ajratib bir nechtasini yozish mumkin:
# SUPER_ADMIN_IDS=123456789,987654321
_super_admins_raw = os.getenv("SUPER_ADMIN_IDS", "")
SUPER_ADMIN_IDS: list[int] = [
    int(x.strip()) for x in _super_admins_raw.split(",") if x.strip().isdigit()
]

# Asosiy kino kanali (yangi kino e'lonlari shu yerga yuboriladi).
# @kanal_username yoki -100... ko'rinishidagi ID bo'lishi mumkin.
MAIN_CHANNEL: str = os.getenv("MAIN_CHANNEL", "")

# --- Fayl yo'llari ---
DB_PATH: str = os.getenv("DB_PATH", "kino.db")
BACKUP_DIR: str = os.getenv("BACKUP_DIR", "backups")
# Video zaxira kanali (ixtiyoriy). Admin paneldan ham ulash mumkin: 💾 Zaxira nusxa → 📦 Zaxira kanali.
# Bu o'zgaruvchi berilsa, baza o'chib ketgan taqdirda ham kanal sozlamasi saqlanib qoladi.
# @kanal_username yoki -100... ko'rinishida.
BACKUP_CHANNEL: str = os.getenv("BACKUP_CHANNEL", "")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")

# --- Sahifalash sozlamalari ---
EPISODES_PER_PAGE = 12
EPISODE_BUTTONS_COLUMNS = 6  # 6 x 2 = 12 ta tugma bitta sahifada
USERS_PER_PAGE = 10

# --- VIP muddatlari (kunlarda) ---
VIP_DAYS = {
    1: 30,
    2: 60,
    3: 90,
}

# --- Oddiy janrlar (emoji, nomi) ---
NORMAL_GENRES: list[tuple[str, str]] = [
    ("🔫", "Jangari"),
    ("🧭", "Sarguzasht"),
    ("😂", "Komediya"),
    ("🎭", "Drama"),
    ("🧙", "Fantastika"),
    ("👻", "Qo'rqinchli"),
    ("🔍", "Detektiv"),
    ("🔥", "Triller"),
    ("❤️", "Melodrama"),
    ("🚀", "Ilmiy fantastika"),
    ("🏛", "Tarixiy"),
    ("🕵️", "Kriminal"),
    ("🪖", "Urush"),
    ("🤠", "Vestern"),
    ("🎵", "Musiqiy"),
    ("👨‍👩‍👧", "Oilaviy"),
    ("🧸", "Multfilm"),
    ("📽", "Hujjatli"),
    ("🏆", "Sport"),
    ("🧠", "Psixologik"),
    # Bo'limlar (janr tizimi orqali)
    ("🇮🇳", "Hind kinolari"),
    ("🇹🇷", "Turk seriallari"),
    ("🇰🇷", "Koreys dramalari"),
]

# --- VIP janrlar ---
VIP_GENRES: list[tuple[str, str]] = [
    ("💎", "Premyera"),
    ("💎", "Oskar g'oliblari"),
    ("💎", "Eksklyuziv"),
]

# --- Kino qo'shishda inline tugma bilan tanlanadigan variantlar (emoji, nom) ---
COUNTRY_OPTIONS: list[tuple[str, str]] = [
    ("🇺🇸", "AQSh"),
    ("🇮🇳", "Hindiston"),
    ("🇹🇷", "Turkiya"),
    ("🇰🇷", "Koreya"),
    ("🇷🇺", "Rossiya"),
    ("🇺🇿", "O'zbekiston"),
    ("🇪🇺", "Yevropa"),
    ("🇯🇵", "Yaponiya"),
]
AUDIO_LANG_OPTIONS: list[tuple[str, str]] = [   # ovoz (dublyaj) tili
    ("🇺🇿", "O'zbekcha"),
    ("🇷🇺", "Ruscha"),
    ("🇬🇧", "Inglizcha"),
    ("🇹🇷", "Turkcha"),
    ("🇮🇳", "Hindcha"),
    ("🇰🇷", "Koreyscha"),
]
SUB_LANG_OPTIONS: list[tuple[str, str]] = [     # subtitr tili
    ("🇺🇿", "O'zbekcha"),
    ("🇷🇺", "Ruscha"),
    ("🇬🇧", "Inglizcha"),
]

# --- 🎯 Kashf qilish: kayfiyat -> qaysi janrlardagi animelar ko'rsatiladi (janr nomlari bazadagi bilan bir xil bo'lishi kerak) ---
# (kalit, emoji, nomi, janrlar). Janrlar bo'sh ro'yxat = hamma janr ("Aralash").
MOODS: list[tuple[str, str, str, list[str]]] = [
    ("laugh", "😂", "Kulgim kelyapti", ["Komediya", "Oilaviy"]),
    ("cry", "😭", "Yig'lagim kelyapti", ["Drama", "Melodrama"]),
    ("action", "🔥", "Adrenalin", ["Jangari", "Sarguzasht", "Triller"]),
    ("scary", "😱", "Qo'rqinch", ["Qo'rqinchli", "Triller", "Detektiv"]),
    ("think", "🧠", "O'ylantiradigan", ["Psixologik", "Ilmiy fantastika"]),
    ("mix", "🎲", "Aralash", []),
]

# --- Vaqt mintaqasi (kunlik e'lon vaqti shu bo'yicha). O'zbekiston = UTC+5 ---
TZ_OFFSET_HOURS: int = int(os.getenv("TZ_OFFSET_HOURS", "5"))
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=TZ_OFFSET_HOURS))

# Standart sozlamalar (settings jadvaliga birinchi ishga tushirishda yoziladi)
DEFAULT_SETTINGS = {
    "start_text": (
        "🎬 <b>Kino Bot</b>ga xush kelibsiz!\n\n"
        "Bu yerda siz minglab kinolar va seriallarni o'zbek tilida "
        "tomosha qilishingiz mumkin. Kino kodini yuboring yoki pastdagi menyudan foydalaning 👇"
    ),
    "start_photo_file_id": "",
    "help_text": (
        "🆘 <b>Yordam</b>\n\n"
        "Botdan foydalanish bo'yicha savollaringiz bo'lsa, admin bilan bog'laning."
    ),
    "help_admin_username": "",
    "vip_price_1": "0",
    "vip_price_2": "0",
    "vip_price_3": "0",
    "payment_card_number": "",
    "payment_card_holder": "",
    # Kunlik tasodifiy anime e'loni (admin paneldan boshqariladi)
    "daily_post_enabled": "1",
    "daily_post_time": "12:00",
    "daily_post_last_date": "",
    # Referal tizimi (admin paneldan boshqariladi)
    "referral_enabled": "1",
    "referral_inviter_days": "7",    # taklif qilgan odamga beriladigan VIP kunlari
    "referral_invitee_days": "2",    # taklif qilingan do'stga beriladigan VIP kunlari
    "referral_monthly_limit": "30",  # bir odam bir oyda ko'pi bilan nechta do'st uchun mukofot olishi (0 = chegarasiz)
    # Avtomatik zaxira nusxa (admin paneldan boshqariladi: 💾 Zaxira nusxa → ⏱ Avto-zaxira)
    "backup_enabled": "1",
    "backup_interval_hours": "2",
    "backup_keep": "10",
    "backup_last_ts": "0",
    "backup_last_attempt_ts": "0",
    # --- 💎 VIP voronka (admin paneldan boshqariladi: ⚙️ Admin panel → 💎 VIP voronka) ---
    "free_daily_limit": "2",         # oddiy foydalanuvchi kuniga nechta kino ocha oladi (0 = cheklovsiz)
    "trial_enabled": "1",            # yangi foydalanuvchiga bepul VIP sinov
    "trial_days": "1",
    "renewal_discount": "10",        # VIP ni qayta olganlarga chegirma, % (0 = yo'q)
    "vip_reminders_enabled": "1",    # VIP tugashiga 3 va 1 kun qolganda eslatma
    "preview_enabled": "1",          # VIP kino kartasida treyler ko'rsatiladi
    "movie_requests_enabled": "1",   # 📝 Kino so'rash
    "translate_description": "1",    # TMDB tavsifini avtomatik o'zbekchaga tarjima qilish
    "early_access_hours": "72",      # yangi kino oddiy foydalanuvchiga necha soatdan keyin ochiladi (0 = darhol)
    "free_max_quality": "720",       # oddiy foydalanuvchi ko'ra oladigan eng yuqori sifat (0 = cheklov yo'q)
    "manual_pay_enabled": "1",       # kartaga o'tkazib chek yuborish (qo'lda tasdiqlash) usuli
    "stars_price_1": "0",            # Telegram Stars narxi (yulduz), 0 = shu tarif uchun Stars o'chiq
    "stars_price_2": "0",
    "stars_price_3": "0",
    "ai_rewrite_description": "1",   # Gemini tavsifni o'zbekcha qiziqarli qilib yozadi (kalit bo'lsa)
    "ai_channel_hook": "1",          # Gemini kanal posti uchun jozibali jumla yozadi (kalit bo'lsa)
    # Video zaxira kanali (admin paneldan ulanadi)
    "storage_channel_id": "",
    "storage_channel_title": "",
}
