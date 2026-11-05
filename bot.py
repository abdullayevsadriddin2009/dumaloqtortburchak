# -*- coding: utf-8 -*-
"""
Video konverter bot: dumaloq (video note) <-> to'rtburchak video.

Sozlamalar quyida, DEFAULT_... qatorlarida turadi. Render'da hech narsa
sozlash shart emas — kod o'zi ishlaydi.

MUHIM: bu faylda token ochiq yozilgan. GitHub repo **private** bo'lishi
shart, aks holda istalgan odam tokenni olib botni boshqaradi.
"""

import html
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telebot import TeleBot, apihelper, types
from telebot.apihelper import ApiTelegramException

# --- SOZLAMALAR (muhit o'zgaruvchilaridan) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file(path=None):
    """Kompyuterda ishlatish uchun: bot.py yonidagi .env faylini o'qiydi.

    Render'da .env kerak emas — u yerda Environment bo'limidagi
    o'zgaruvchilar ishlatiladi. Allaqachon mavjud o'zgaruvchilar
    o'zgartirilmaydi (muhit ustuvor).
    """
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


load_env_file()


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _env_int(name, default=0):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --- ASOSIY SOZLAMALAR (shu yerni o'zgartirasiz) ---
# BotFather bergan token:
DEFAULT_BOT_TOKEN = "8357374445:AAFJfx3qYEc-fWVEZ-T-O3tILQ5UO74m6Lc"
# Adminning Telegram ID si (bir nechta bo'lsa vergul bilan: "111,222"):
DEFAULT_ADMIN_ID = "6377032074"

# Muhit o'zgaruvchisi berilgan bo'lsa u ustun turadi (ixtiyoriy).
BOT_TOKEN = _env("BOT_TOKEN") or DEFAULT_BOT_TOKEN
ADMIN_IDS = {
    int(part)
    for part in (_env("ADMIN_ID") or DEFAULT_ADMIN_ID).replace(";", ",").split(",")
    if part.strip().lstrip("-").isdigit()
}
DB_FILE = _env("DB_FILE", os.path.join(BASE_DIR, "bot_database.db"))
MAX_INPUT_MB = _env_int("MAX_INPUT_MB", 20)
MAX_WORKERS = max(1, _env_int("MAX_WORKERS", 2))
# Telegram botga qaysi turdagi yangilanishlar kerak. callback_query BO'LMASA
# inline tugmalar (admin panel) umuman ishlamaydi — shuning uchun ro'yxat
# ochiq yozilgan va har bir poll so'rovida yuboriladi.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "chat_member",
    "inline_query",
    "chosen_inline_result",
]

PORT = _env_int("PORT", 8080)
STEP_TTL = 600          # admin amallari uchun kutish vaqti (soniya)
BROADCAST_TTL = 900     # ommaviy xabarni tasdiqlash muddati (soniya)

# Global socket timeout qo'yilmaydi: u health-check va SQLite bilan
# to'qnashib, botning "osilib qolishiga" sabab bo'lardi.
apihelper.CONNECT_TIMEOUT = 20
apihelper.READ_TIMEOUT = 120

bot = TeleBot(BOT_TOKEN or "0:invalid", threaded=True)
BOT_USERNAME = ""

# --- FFmpeg YO'LINI ANIQLASH ---
def find_ffmpeg():
    candidates = [
        _env("FFMPEG_PATH"),
        os.path.join(BASE_DIR, "ffmpeg_bin", "ffmpeg"),
        os.path.join(BASE_DIR, "ffmpeg_bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG_PATH = find_ffmpeg()


def ffmpeg_ready():
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-version"], capture_output=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


# --- MA'LUMOTLAR BAZASI (SQLite) ---
@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                blocked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                channel_name TEXT,
                invite_link TEXT
            )
            """
        )
        # Eski bazani yangilash (ustunlar yo'q bo'lsa qo'shiladi)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for column, ddl in (
            ("first_name", "TEXT"),
            ("blocked", "INTEGER DEFAULT 0"),
            ("created_at", "TEXT"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")


init_db()


def add_user(user_id, username=None, first_name=None):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, blocked)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                blocked = 0
            """,
            (user_id, username, first_name),
        )


def get_user_ids():
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE COALESCE(blocked, 0) = 0"
        ).fetchall()
    return [row[0] for row in rows]


def count_users():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def mark_user_blocked(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))


def get_channels():
    with db() as conn:
        return conn.execute(
            "SELECT channel_id, channel_name, invite_link FROM channels"
        ).fetchall()


def add_channel(channel_id, channel_name, invite_link):
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO channels (channel_id, channel_name, invite_link)
            VALUES (?, ?, ?)
            """,
            (str(channel_id), channel_name, invite_link),
        )


def remove_channel(identifier):
    """Kanalni ID, username yoki nomi bo'yicha o'chiradi. Topilsa True."""
    identifier = (identifier or "").strip()
    if not identifier:
        return False
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM channels WHERE channel_id = ?", (identifier,)
        )
        if cursor.rowcount:
            return True
        rows = conn.execute(
            "SELECT channel_id, channel_name FROM channels"
        ).fetchall()
        for channel_id, name in rows:
            if (
                channel_id.lstrip("-") == identifier.lstrip("-")
                or (name or "").lower() == identifier.lower()
            ):
                conn.execute(
                    "DELETE FROM channels WHERE channel_id = ?", (channel_id,)
                )
                return True
    return False


# --- MAJBURIY A'ZOLIK TEKSHIRUVI ---
def check_subscription(user_id):
    for channel_id, _name, _link in get_channels():
        try:
            member = bot.get_chat_member(channel_id, user_id)
        except ApiTelegramException as error:
            # Bot kanalda admin emas yoki kanal o'chirilgan: foydalanuvchini
            # bloklab qo'ymaymiz, faqat logga yozamiz.
            print(f"[OBUNA] {channel_id} tekshirilmadi: {error}")
            continue
        except Exception as error:
            print(f"[OBUNA] {channel_id} xato: {error}")
            continue

        status = getattr(member, "status", "left")
        if status in ("left", "kicked"):
            return False
        if status == "restricted" and not getattr(member, "is_member", False):
            return False
    return True


def get_sub_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for _channel_id, name, link in get_channels():
        if link and link.startswith("http"):
            keyboard.add(types.InlineKeyboardButton(text=name or "Kanal", url=link))
        else:
            keyboard.add(
                types.InlineKeyboardButton(
                    text=name or "Kanal", callback_data="noop"
                )
            )
    keyboard.add(
        types.InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")
    )
    return keyboard


# --- YORDAMCHI FUNKSIYALAR ---
def is_admin(user_id):
    return bool(user_id) and user_id in ADMIN_IDS


def safe_answer(call, text=None, show_alert=False):
    """Callback'ga har doim javob beradi -> tugma 'aylanib qolmaydi'."""
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
    except Exception:
        pass


def safe_edit(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    try:
        bot.edit_message_text(
            text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return True
    except Exception:
        return False


def retry_after(error, default=5):
    try:
        parameters = (error.result_json or {}).get("parameters") or {}
        return max(1, min(int(parameters.get("retry_after", default)), 60))
    except Exception:
        return default


def with_retry(sender, attempts=3):
    """Tarmoq / 429 / 5xx xatolarida qayta urinadi."""
    last_error = None
    for attempt in range(attempts):
        try:
            return sender()
        except ApiTelegramException as error:
            last_error = error
            if error.error_code == 429:
                time.sleep(retry_after(error))
            elif error.error_code and error.error_code >= 500:
                time.sleep(3 * (attempt + 1))
            else:
                raise
        except (ConnectionError, OSError, TimeoutError) as error:
            last_error = error
            time.sleep(3 * (attempt + 1))
    raise last_error


# --- FFmpeg FUNKSIYALARI ---
def run_ffmpeg(command, timeout=900):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg juda uzoq ishladi va to'xtatildi.")
    except FileNotFoundError:
        raise RuntimeError("FFmpeg topilmadi.")

    if result.returncode != 0:
        details = (result.stderr or "").strip().splitlines()[-6:]
        print("[FFMPEG] " + " | ".join(details))
        raise RuntimeError("FFmpeg videoni qayta ishlay olmadi.")


def make_square_video(input_path, output_path):
    """To'rtburchak videoni dumaloq (video note) uchun kvadratga keltiradi.

    sifat uchun: video kattalashtirilmaydi va 480px ga tushirilmaydi —
    o'rtasidan asl o'lchamda kvadrat qirqiladi (1080px dan katta bo'lsa
    faqat shunda kichraytiriladi), fps va ovoz asl holida qoladi.
    Telegram video note uchun: kvadrat, H.264, yuv420p, 1:1 SAR, <=60 soniya.
    """
    filter_complex = (
        "[0:v]crop='min(iw,ih)':'min(iw,ih)',"          # markazdan kvadrat, asl pikselda
        "scale='min(1080,trunc(iw/2)*2)':-2:flags=lanczos,"  # faqat kerak bo'lsa kichraytirish
        "setsar=1,format=yuv420p[v]"
    )
    command = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-t", "59",                                      # video note chegarasi
        "-map", "[v]", "-map", "0:a:0?",                 # audio bo'lmasa ham ishlaydi
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-threads", "2",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
        output_path,
    ]
    run_ffmpeg(command)


def make_rectangle_video(input_path, output_path):
    """Dumaloq (kvadrat) videoni to'liq kadrli 16:9 videoga aylantiradi.

    Yon tomonlarda xira (blur) fon QO'YILMAYDI: video o'rtasidan 16:9
    qirqiladi va kadrni to'liq egallaydi. Qirqish asl piksellarda
    bajariladi — video kattalashtirilmaydi, shuning uchun sifat
    yo'qolmaydi. Kenglik 1920px dan oshsa, mutanosib kichraytiriladi.
    """
    # Markazdan 16:9 qirqish: kengroq videoda yon, balandroq videoda tepa/past
    # qirqiladi. Qirqish asl piksellarda, hech qanday kattalashtirish yo'q.
    crop_expr = (
        "crop=w='if(gte(iw/ih,1.7777),trunc(ih*16/9/2)*2,trunc(iw/2)*2)':"
        "h='if(gte(iw/ih,1.7777),trunc(ih/2)*2,trunc(iw*9/16/2)*2)'"
    )
    filter_complex = (
        "[0:v]" + crop_expr + ","
        "scale=w='min(1920,trunc(iw/2)*2)':h=-2:flags=lanczos,"
        "setsar=1,format=yuv420p[v]"
    )
    command = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-threads", "2",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
        output_path,
    ]
    run_ffmpeg(command)


# --- KO'P VAZIFALI ISHCHI (bir vaqtda cheklangan miqdorda konvertatsiya) ---
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="convert")
busy_users = set()
busy_lock = threading.Lock()


def acquire_user(user_id):
    with busy_lock:
        if user_id in busy_users:
            return False
        busy_users.add(user_id)
        return True


def release_user(user_id):
    with busy_lock:
        busy_users.discard(user_id)


# --- ADMIN KLAVIATURALARI ---
def get_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
        types.InlineKeyboardButton(text="📢 Ommaviy xabar", callback_data="admin_broadcast"),
    )
    keyboard.add(
        types.InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin_add_channel"),
        types.InlineKeyboardButton(text="➖ Kanalni o'chirish", callback_data="admin_del_channel"),
    )
    keyboard.add(
        types.InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="admin_list_channels")
    )
    keyboard.add(
        types.InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin_panel"),
        types.InlineKeyboardButton(text="❌ Yopish", callback_data="admin_close"),
    )
    return keyboard


def admin_panel_text():
    return (
        "⚙️ <b>Admin panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{count_users()}</b> ta\n"
        f"📢 Majburiy kanallar: <b>{len(get_channels())}</b> ta\n\n"
        "Kerakli bo'limni tanlang:"
    )


INSTRUCTIONS = (
    "👋 <b>Salom! Video konverter botga xush kelibsiz!</b>\n\n"
    "Men avtomatik rejimda ishlayman:\n"
    "1️⃣ Menga oddiy <b>to'rtburchak video</b> yuboring ➡️ uni <b>dumaloq video</b> qilib beraman.\n"
    "2️⃣ Menga <b>dumaloq video</b> yuboring ➡️ uni to'liq ekranli <b>to'rtburchak videoga</b> aylantiraman.\n\n"
    "⚡️ Shunchaki videoni yuboring va natijani oling!"
)


# --- HANDLERS: START ---
@bot.message_handler(commands=["start"], chat_types=["private"])
def start_handler(message):
    user = message.from_user
    add_user(user.id, user.username, user.first_name)

    if not check_subscription(user.id):
        bot.send_message(
            message.chat.id,
            "👋 Salom! Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:",
            reply_markup=get_sub_keyboard(),
        )
        return

    if is_admin(user.id):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(text="⚙️ Admin panel", callback_data="admin_panel")
        )
        bot.send_message(
            message.chat.id, INSTRUCTIONS, parse_mode="HTML", reply_markup=keyboard
        )
    else:
        bot.send_message(message.chat.id, INSTRUCTIONS, parse_mode="HTML")


@bot.message_handler(commands=["id"])
def id_handler(message):
    bot.reply_to(
        message,
        f"🆔 Sizning Telegram ID: <code>{message.from_user.id}</code>\n"
        f"💬 Chat ID: <code>{message.chat.id}</code>",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["admin"], chat_types=["private"])
def admin_handler(message):
    if not is_admin(message.from_user.id):
        return
    clear_admin_steps(message.from_user.id, message.chat.id)
    bot.send_message(
        message.chat.id,
        admin_panel_text(),
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    if not is_admin(message.from_user.id):
        return
    clear_admin_steps(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "✅ Amal bekor qilindi.", reply_markup=get_admin_keyboard())


# --- ADMIN "KEYINGI QADAM" BOSHQARUVI ---
admin_steps = {}
pending_broadcast = {}
consumed_messages = deque(maxlen=100)   # admin amali "yeb qo'ygan" xabarlar


def set_admin_step(user_id, step):
    admin_steps[user_id] = (step, time.time())


def clear_admin_steps(user_id, chat_id=None):
    admin_steps.pop(user_id, None)
    pending_broadcast.pop(user_id, None)
    if chat_id is not None:
        try:
            bot.clear_step_handler_by_chat_id(chat_id)
        except Exception:
            pass


def take_admin_step(message, expected):
    """Faqat admin va faqat kutilgan qadam bo'lsa True qaytaradi."""
    user = getattr(message, "from_user", None)
    if user is None or not is_admin(user.id):
        return False
    current = admin_steps.pop(user.id, None)
    if not current or current[0] != expected:
        return False
    if time.time() - current[1] > STEP_TTL:
        bot.send_message(message.chat.id, "⌛️ Amal muddati tugadi, qaytadan boshlang.")
        return False
    # Telebot next-step handler bilan birga oddiy handler'lar ham ishga
    # tushadi: bu xabar konvertatsiyaga yoki boshqa amalga tushmasligi uchun
    # uni "band" deb belgilaymiz.
    consumed_messages.append((message.chat.id, message.message_id))
    return True


def register_admin_step(chat_id, user_id, step, handler):
    clear_admin_steps(user_id, chat_id)
    set_admin_step(user_id, step)
    prompt = bot.send_message(chat_id, STEP_PROMPTS[step], reply_markup=types.ForceReply())
    bot.register_next_step_handler(prompt, handler)


STEP_PROMPTS = {
    "broadcast": (
        "📝 <b>Ommaviy xabar</b>\n\n"
        "Yubormoqchi bo'lgan postni yoki matnni yuboring.\n"
        "Bekor qilish uchun /cancel"
    ),
    "add_channel": (
        "📢 <b>Yangi kanal qo'shish</b>\n\n"
        "1️⃣ Kanaldagi istalgan postni botga <b>forward</b> qiling (tavsiya etiladi), yoki\n"
        "2️⃣ Kanal username'ini yuboring: <code>@kanal</code>\n\n"
        "⚠️ Bot kanalda <b>administrator</b> bo'lishi shart.\n"
        "Bekor qilish uchun /cancel"
    ),
    "del_channel": (
        "➖ <b>Kanalni o'chirish</b>\n\n"
        "Kanalning ID, username yoki nomini yuboring.\n"
        "Bekor qilish uchun /cancel"
    ),
}


# --- VIDEO KONVERTATSIYA ---
@bot.message_handler(content_types=["video"], chat_types=["private"])
def handle_normal_video(message):
    start_conversion(message, "to_note")


@bot.message_handler(content_types=["video_note"], chat_types=["private"])
def handle_round_video(message):
    start_conversion(message, "to_video")


@bot.message_handler(
    content_types=["video", "video_note"],
    func=lambda m: m.chat.type != "private",
)
def handle_group_media(message):
    bot.send_message(
        message.chat.id,
        "🤖 Men faqat shaxsiy chatda ishlayman."
        + (f" Menga shu yerga yozing: @{BOT_USERNAME}" if BOT_USERNAME else ""),
    )


def start_conversion(message, kind):
    user = message.from_user

    # Admin panel amali uchun yuborilgan xabar (masalan, reklama posti) hech
    # qachon konvertatsiyaga tushmasligi kerak.
    if (message.chat.id, message.message_id) in consumed_messages:
        return

    add_user(user.id, user.username, user.first_name)

    if not check_subscription(user.id):
        bot.send_message(
            message.chat.id,
            "❌ Botdan foydalanish uchun kanallarga a'zo bo'ling:",
            reply_markup=get_sub_keyboard(),
        )
        return

    if not ffmpeg_ready():
        bot.send_message(
            message.chat.id, "❌ Serverda FFmpeg topilmadi. Admin bilan bog'laning."
        )
        return

    media = message.video if kind == "to_note" else message.video_note
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_INPUT_MB * 1024 * 1024:
        bot.reply_to(
            message,
            f"⚠️ Video juda katta ({size / 1024 / 1024:.1f} MB). "
            f"Telegram botlar {MAX_INPUT_MB} MB gacha videoni yuklay oladi.",
        )
        return

    if not acquire_user(user.id):
        bot.reply_to(
            message, "⏳ Oldingi videongiz hali tayyorlanmoqda. Iltimos, kuting."
        )
        return

    try:
        status = bot.reply_to(
            message,
            "⏳ Video qabul qilindi, qayta ishlanmoqda..."
            if kind == "to_note"
            else "⏳ Dumaloq video qabul qilindi, to'rtburchak videoga o'tkazilmoqda...",
        )
        executor.submit(conversion_job, message, kind, status.message_id)
    except Exception:
        release_user(user.id)
        raise


def conversion_job(message, kind, status_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    media = message.video if kind == "to_note" else message.video_note
    workdir = tempfile.mkdtemp(prefix="conv_")
    input_path = os.path.join(workdir, "input.mp4")
    output_path = os.path.join(workdir, "output.mp4")

    try:
        file_info = bot.get_file(media.file_id)
        with open(input_path, "wb") as target:
            target.write(bot.download_file(file_info.file_path))

        try:
            bot.send_chat_action(chat_id, "record_video_note" if kind == "to_note" else "upload_video")
        except Exception:
            pass

        if kind == "to_note":
            make_square_video(input_path, output_path)
        else:
            make_rectangle_video(input_path, output_path)

        # Bot API orqali yuklash chegarasi 50 MB
        output_mb = os.path.getsize(output_path) / (1024 * 1024)
        if output_mb > 49:
            raise RuntimeError("natija juda katta")

        if kind == "to_note":
            def send():
                with open(output_path, "rb") as video_file:
                    return bot.send_video_note(
                        chat_id,
                        video_file,
                        reply_to_message_id=message.message_id,
                        timeout=600,
                    )
        else:
            def send():
                with open(output_path, "rb") as video_file:
                    return bot.send_video(
                        chat_id,
                        video_file,
                        caption="🎥 Videongiz tayyor!",
                        supports_streaming=True,
                        reply_to_message_id=message.message_id,
                        timeout=600,
                    )

        with_retry(send)

        try:
            bot.delete_message(chat_id, status_message_id)
        except Exception:
            pass

    except Exception as error:
        traceback.print_exc()
        text = (
            "❌ Videoni qayta ishlashda xatolik yuz berdi.\n"
            "Iltimos, boshqa video bilan qayta urinib ko'ring."
        )
        if isinstance(error, RuntimeError) and "juda uzoq" in str(error):
            text = "❌ Video juda uzun, qayta ishlash to'xtatildi. Qisqaroq video yuboring."
        elif isinstance(error, RuntimeError) and "juda katta" in str(error):
            text = (
                "❌ Natija hajmi 50 MB dan oshib ketdi (Telegram chegarasi).\n"
                "Qisqaroq yoki kichikroq o'lchamdagi video yuboring."
            )
        safe_edit(chat_id, status_message_id, text)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        release_user(user_id)


# --- CALLBACK'LAR (ADMIN PANEL) ---
@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    try:
        handle_callback(call)
    except Exception:
        traceback.print_exc()
        safe_answer(call, "❌ Xatolik yuz berdi, qayta urinib ko'ring.", show_alert=True)


def handle_callback(call):
    data = call.data or ""
    user_id = call.from_user.id

    if data == "check_sub":
        if check_subscription(user_id):
            safe_answer(call, "✅ Rahmat! Barcha kanallarga a'zo bo'ldingiz.")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            bot.send_message(
                call.message.chat.id,
                "👋 <b>Siz muvaffaqiyatli ro'yxatdan o'tdingiz!</b>\n\n"
                "Menga oddiy video yoki dumaloq video yuboring — avtomatik o'zgartiraman!",
                parse_mode="HTML",
            )
        else:
            safe_answer(call, "❌ Hali hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)
        return

    if data == "noop":
        safe_answer(call, "Bu kanal uchun havola sozlanmagan.")
        return

    if not is_admin(user_id):
        safe_answer(call, "⛔ Bu bo'lim faqat admin uchun.")
        return

    # Admin uchun callback har doim darhol yopiladi (tugma "aylanib" qolmaydi)
    safe_answer(call)
    chat_id = call.message.chat.id

    if data == "admin_panel":
        safe_edit(chat_id, call.message.message_id, admin_panel_text(),
                  reply_markup=get_admin_keyboard(), parse_mode="HTML")

    elif data == "admin_close":
        clear_admin_steps(user_id, chat_id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass

    elif data == "admin_stats":
        blocked = count_users() - len(get_user_ids())
        safe_edit(
            chat_id,
            call.message.message_id,
            "📊 <b>Bot statistikasi</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{count_users()}</b> ta\n"
            f"🚫 Botni bloklaganlar: <b>{blocked}</b> ta\n"
            f"📢 Majburiy kanallar: <b>{len(get_channels())}</b> ta",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML",
        )

    elif data == "admin_list_channels":
        channels = get_channels()
        if not channels:
            text = "📋 Majburiy a'zolik kanallari sozlanmagan."
        else:
            text = "📋 <b>Majburiy a'zolik kanallari:</b>\n\n"
            for channel_id, name, link in channels:
                text += (
                    f"▪️ <b>{html.escape(name or '-')}</b>\n"
                    f"   └ ID: <code>{html.escape(str(channel_id))}</code>\n"
                    f"   └ Havola: {html.escape(link or '-')}\n\n"
                )
        safe_edit(chat_id, call.message.message_id, text,
                  reply_markup=get_admin_keyboard(), parse_mode="HTML")

    elif data == "admin_broadcast":
        register_admin_step(chat_id, user_id, "broadcast", on_broadcast_content)

    elif data == "admin_add_channel":
        register_admin_step(chat_id, user_id, "add_channel", on_add_channel)

    elif data == "admin_del_channel":
        register_admin_step(chat_id, user_id, "del_channel", on_del_channel)

    elif data == "bc_send":
        start_broadcast(call)
    elif data == "bc_cancel":
        pending_broadcast.pop(user_id, None)
        safe_edit(chat_id, call.message.message_id, "❌ Ommaviy xabar bekor qilindi.")
    else:
        safe_answer(call, "Noma'lum buyruq.")


# --- OMMAVIY XABAR (tasdiqlash bilan) ---
def on_broadcast_content(message):
    if not take_admin_step(message, "broadcast"):
        return

    pending_broadcast[message.from_user.id] = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "time": time.time(),
    }
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(text="✅ Yuborish", callback_data="bc_send"),
        types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bc_cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"👥 Ushbu xabar <b>{len(get_user_ids())}</b> ta foydalanuvchiga yuboriladi.\n\n"
        "Tasdiqlaysizmi?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def start_broadcast(call):
    user_id = call.from_user.id
    data = pending_broadcast.pop(user_id, None)
    if not data or time.time() - data["time"] > BROADCAST_TTL:
        safe_edit(call.message.chat.id, call.message.message_id,
                  "⌛️ Tasdiqlash muddati tugadi. Qaytadan boshlang: /admin")
        return

    status = bot.send_message(call.message.chat.id, "🚀 Xabar yuborilmoqda...")
    threading.Thread(
        target=broadcast_worker,
        args=(call.message.chat.id, status.message_id, data["chat_id"], data["message_id"]),
        daemon=True,
    ).start()


def broadcast_worker(chat_id, status_id, from_chat_id, message_id):
    users = get_user_ids()
    total = len(users)
    sent = failed = 0

    for index, user_id in enumerate(users, start=1):
        try:
            with_retry(
                lambda uid=user_id: bot.copy_message(uid, from_chat_id, message_id, timeout=30)
            )
            sent += 1
        except ApiTelegramException as error:
            failed += 1
            description = (error.description or "").lower()
            if error.error_code == 403 or "blocked" in description or "deactivated" in description:
                mark_user_blocked(user_id)
        except Exception as error:
            failed += 1
            print(f"[BROADCAST] {user_id}: {error}")

        time.sleep(0.05)  # Telegram limitiga tushib qolmaslik uchun
        if index % 25 == 0:
            safe_edit(chat_id, status_id, f"🚀 Yuborilmoqda... {index}/{total}")

    safe_edit(
        chat_id,
        status_id,
        "✅ <b>Ommaviy xabar yakunlandi</b>\n\n"
        f"📥 Yetkazildi: <b>{sent}</b> ta\n"
        f"❌ Yetib bormadi: <b>{failed}</b> ta",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


# --- KANAL QO'SHISH / O'CHIRISH ---
def channel_from_message(message):
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None and getattr(chat, "type", None) == "channel":
        return chat
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is not None and getattr(chat, "type", None) == "channel":
            return chat
    return None


def normalize_channel_reference(text):
    text = (text or "").strip()
    if "t.me/" in text:
        part = text.split("t.me/")[1].split("/")[0].split("?")[0].strip()
        if part and not part.startswith("+") and not part.startswith("joinchat"):
            return "@" + part
    if text and not text.startswith("@") and not text.lstrip("-").isdigit():
        return "@" + text
    return text


def on_add_channel(message):
    if not take_admin_step(message, "add_channel"):
        return

    try:
        chat = channel_from_message(message)
        channel_name = None
        invite_link = None

        if chat is not None:
            channel_id = str(chat.id)
            channel_name = chat.title
            if getattr(chat, "username", None):
                invite_link = f"https://t.me/{chat.username}"
        else:
            if not message.text:
                bot.send_message(
                    message.chat.id,
                    "❌ Kanal postini forward qiling yoki username yuboring.",
                )
                return

            text = message.text.strip()
            if "|" in text:
                parts = [part.strip() for part in text.split("|")]
                channel_id = parts[0]
                channel_name = parts[1] if len(parts) > 1 else None
                invite_link = parts[2] if len(parts) > 2 else None
            else:
                channel_id = normalize_channel_reference(text)

            chat = bot.get_chat(channel_id)
            channel_id = str(chat.id)
            channel_name = chat.title
            if getattr(chat, "username", None):
                invite_link = f"https://t.me/{chat.username}"

        # Bot kanalda admin bo'lishi shart (obuna tekshiruvi ishlashi uchun)
        warning = ""
        try:
            me = bot.get_me()
            member = bot.get_chat_member(channel_id, me.id)
            if getattr(member, "status", "") not in ("administrator", "creator"):
                warning = "\n\n⚠️ Bot bu kanalda admin emas — obuna tekshiruvi ishlamaydi!"
        except Exception as error:
            warning = f"\n\n⚠️ Botning kanaldagi holati tekshirilmadi: {html.escape(str(error)[:80])}"

        if not invite_link:
            try:
                invite_link = bot.export_chat_invite_link(channel_id)
            except Exception:
                invite_link = ""

        add_channel(channel_id, channel_name, invite_link)

        bot.send_message(
            message.chat.id,
            "✅ <b>Kanal qo'shildi!</b>\n\n"
            f"📢 Nomi: <b>{html.escape(channel_name or '-')}</b>\n"
            f"🆔 ID: <code>{html.escape(channel_id)}</code>\n"
            f"🔗 Havola: {html.escape(invite_link or 'yo\'q')}"
            f"{warning}",
            parse_mode="HTML",
            reply_markup=get_admin_keyboard(),
        )

    except ApiTelegramException as error:
        traceback.print_exc()
        bot.send_message(
            message.chat.id,
            "❌ <b>Kanalni qo'shib bo'lmadi</b>\n\n"
            f"Sababi: {html.escape(str(error.description or error)[:120])}\n\n"
            "💡 Kanaldan bitta postni botga <b>forward</b> qilib ko'ring.\n"
            "⚠️ Bot kanalda administrator bo'lishi shart.",
            parse_mode="HTML",
            reply_markup=get_admin_keyboard(),
        )
    except Exception as error:
        traceback.print_exc()
        bot.send_message(
            message.chat.id,
            f"❌ Xatolik: {html.escape(str(error)[:120])}",
            reply_markup=get_admin_keyboard(),
        )


def on_del_channel(message):
    if not take_admin_step(message, "del_channel"):
        return

    if not message.text:
        bot.send_message(message.chat.id, "❌ Kanal ID yoki username yuboring.")
        return

    identifier = message.text.strip()
    removed = remove_channel(identifier)

    if not removed:
        # @username bo'lsa ID ga aylantirib qayta urinamiz
        try:
            chat = bot.get_chat(normalize_channel_reference(identifier))
            removed = remove_channel(str(chat.id))
        except Exception:
            removed = False

    if removed:
        bot.send_message(
            message.chat.id,
            f"✅ Kanal o'chirildi: <code>{html.escape(identifier)}</code>",
            parse_mode="HTML",
            reply_markup=get_admin_keyboard(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ Bunday kanal ro'yxatda topilmadi. Ro'yxatni tekshiring.",
            reply_markup=get_admin_keyboard(),
        )


# --- RENDER UCHUN HEALTH-CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Bot is alive and running 24/7!"
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def run_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()


# --- MAIN ---
def main():
    global BOT_USERNAME

    if not BOT_TOKEN:
        print(
            "❌ BOT_TOKEN topilmadi.\n"
            "\nRender'da (bir marta qilinadi):\n"
            "  1) Dashboard -> botingiz (Web Service) -> chap menyuda 'Environment'\n"
            "  2) 'Add Environment Variable' -> Key: BOT_TOKEN, Value: @BotFather bergan token\n"
            "  3) Yana bittasi -> Key: ADMIN_ID, Value: sizning Telegram ID ingiz (/id)\n"
            "  4) 'Save Changes' -> Render o'zi qayta deploy qiladi\n"
            "\nKompyuterda ishlatayotgan bo'lsangiz: bot.py yonida .env fayl yarating:\n"
            "  BOT_TOKEN=...\n"
            "  ADMIN_ID=...\n"
        )
        sys.exit(1)
    if not ADMIN_IDS:
        print("⚠️ ADMIN_ID topilmadi — admin panel ishlamaydi.")

    print(f"FFmpeg: {FFMPEG_PATH} | mavjud: {ffmpeg_ready()}")

    threading.Thread(target=run_health_server, daemon=True).start()
    print(f"Health-check server {PORT}-portda ishga tushdi.")

    try:
        bot.remove_webhook()
    except Exception as error:
        print(f"[WEBHOOK] tozalanmadi: {error}")

    # Diagnostika: logda callback_query ko'rinmasa, tugmalar ishlamaydi.
    try:
        info = bot.get_webhook_info()
        print(f"[TG] allowed_updates: {info.allowed_updates}")
    except Exception as error:
        print(f"[WEBHOOK INFO] {error}")

    try:
        BOT_USERNAME = bot.get_me().username or ""
        print(f"Bot: @{BOT_USERNAME}")
    except Exception as error:
        print(f"[GETME] {error}")

    try:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Botni ishga tushirish"),
                types.BotCommand("id", "Mening ID raqamim"),
            ]
        )
        for admin_id in ADMIN_IDS:
            try:
                bot.set_my_commands(
                    [
                        types.BotCommand("start", "Botni ishga tushirish"),
                        types.BotCommand("admin", "Admin panel"),
                        types.BotCommand("id", "Mening ID raqamim"),
                        types.BotCommand("cancel", "Amalni bekor qilish"),
                    ],
                    scope=types.BotCommandScopeChat(admin_id),
                )
            except Exception:
                pass
    except Exception as error:
        print(f"[COMMANDS] {error}")

    print("Bot muvaffaqiyatli ishga tushdi...")
    # skip_pending=True: bot qayta ishga tushganda eski (masalan, tugallanmagan
    # ommaviy xabar) so'rovlar bajarilmaydi.
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=True,
        none_stop=True,
        allowed_updates=ALLOWED_UPDATES,
    )


if __name__ == "__main__":
    main()
