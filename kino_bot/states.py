from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    waiting_title = State()
    waiting_code = State()


class VipPaymentStates(StatesGroup):
    waiting_screenshot = State()


class AddAnimeStates(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_poster = State()
    waiting_genres = State()
    waiting_source = State()    # TMDB'dan to'ldirishmi yoki qo'lda kiritishmi — tanlash
    waiting_al_query = State()  # TMDB uchun boshqa nom yozish


class EditAnimeStates(StatesGroup):
    waiting_new_title = State()
    waiting_new_description = State()
    waiting_genres = State()
    waiting_new_country = State()
    waiting_new_year = State()
    waiting_new_language = State()
    waiting_new_poster = State()


class AddEpisodeStates(StatesGroup):
    waiting_video = State()
    waiting_filler_count = State()


class BroadcastStates(StatesGroup):
    waiting_content = State()


class AdminTextStates(StatesGroup):
    waiting_start_text = State()
    waiting_start_photo = State()
    waiting_help_text = State()
    waiting_help_admin_username = State()
    waiting_vip_price_1 = State()
    waiting_vip_price_2 = State()
    waiting_vip_price_3 = State()
    waiting_card_number = State()
    waiting_card_holder = State()
    waiting_required_channel = State()
    waiting_grant_vip_search = State()
    waiting_user_search = State()
    waiting_backup_file = State()
    waiting_backup_channel = State()


class OptionStates(StatesGroup):
    """Kino qo'shish/tahrirlashda davlat, til, subtitr, yilni inline tugma bilan tanlash."""
    picking = State()         # inline tugmalardan tanlash
    waiting_custom = State()  # "✍️ Boshqa" bosilganda qo'lda yozish


class GenreStates(StatesGroup):
    waiting_new_name = State()
    waiting_new_emoji = State()
    waiting_edit_name = State()
    waiting_edit_emoji = State()


class DailyPostStates(StatesGroup):
    waiting_time = State()


class ReferralAdminStates(StatesGroup):
    waiting_value = State()


class RequestStates(StatesGroup):
    """Foydalanuvchi: 📝 Kino so'rash."""
    waiting_text = State()


class ReportStates(StatesGroup):
    """Foydalanuvchi: ⚠️ Muammo haqida xabar."""
    waiting_text = State()


class KinoAdminStates(StatesGroup):
    """Admin: kino qo'shimcha maydonlari, variantlar, to'plamlar, voronka sozlamalari."""
    waiting_field_value = State()      # rejissyor / aktyorlar / davomiylik / yosh / reyting / treyler / sifat
    waiting_version_video = State()    # variant uchun video
    waiting_version_label = State()    # variant nomi (masalan: 🇺🇿 O'zbekcha · 1080p)
    waiting_collection_name = State()  # yangi to'plam nomi
    waiting_setting_value = State()    # voronka sozlamasi (son)


class AnnounceStates(StatesGroup):
    """Admin: e'lon kanallari va rejalashtirilgan postlar."""
    waiting_chat = State()        # yangi kanal (@username, -100... yoki kanaldan forward)
    waiting_time = State()        # kanalning kunlik e'lon vaqti (HH:MM)
    waiting_sched_time = State()  # rejalashtirilgan post vaqti
    waiting_quality = State()     # variant sifati (hujjat sifatida yuborilgan video uchun)
