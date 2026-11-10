# -*- coding: utf-8 -*-
"""
Video konverter bot: dumaloq (video note) <-> to'rtburchak video.

Sozlamalar quyida, DEFAULT_... qatorlarida turadi. Render'da hech narsa
sozlash shart emas — kod o'zi ishlaydi.

MUHIM: bu faylda token ochiq yozilgan. GitHub repo **private** bo'lishi
shart, aks holda istalgan odam tokenni olib botni boshqaradi.
"""

import html
import json
import os
import random
import urllib.request
import re
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

# Render'da stdout blok bilan buferlanadi va print() qatorlari logda kechroq
# (yoki umuman) ko'rinmaydi. Qator-bufer rejimi loglarni darhol ko'rsatadi.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:  # pragma: no cover
    pass

# Firebase (Firestore) — ma'lumotlar bulutda saqlanadi. Kutubxona yo'q bo'lsa
# bot SQLite bilan ishlayveradi, shuning uchun import ixtiyoriy.
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials
    from firebase_admin import firestore as fb_firestore

    FIREBASE_LIB = True
except Exception:  # pragma: no cover
    FIREBASE_LIB = False

# yt-dlp — Instagram/YouTube/TikTok va boshqa havolalardan video yuklash.
# Kutubxona bo'lmasa botning qolgan qismi ishlayveradi.
try:
    import yt_dlp

    YTDLP_LIB = True
except Exception:  # pragma: no cover
    YTDLP_LIB = False

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
DEFAULT_BOT_TOKEN = "8357374445:AAF95N5NmF-94w6tDXyCkJNNFL5Irt29X6k"
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

# Dumaloq video (video note) o'lchami. Telegram 640px dan katta kvadratni
# video note qilib qabul qilmaydi — oddiy video qilib yuboradi.
SQUARE_SIZE = 512
# Dumaloq -> to'rtburchak natijasining o'lchami (16:9)
RECT_WIDTH = 1280
RECT_HEIGHT = 720

# Telegram bot yuklay oladigan eng katta fayl (50 MB chegara, biroz zapas bilan).
MAX_SEND_MB = 49
AUDIO_CALLBACK = "extract_audio"

# Deploy qaysi versiyada ishlayotganini logdan bilish uchun.
BUILD_VERSION = "2026-11-10.16"

# Diagnostika: /status endpoint va watchdog uchun jonli holat.
STARTED_AT = time.time()
STATE = {
    "polling": False,
    "last_update": 0.0,
    "last_poll_ok": 0.0,
    "conflicts_409": 0,
    "poll_restarts": 0,
    "last_poll_note": "",
}

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


def find_ffprobe():
    """ffprobe ffmpeg bilan bir papkada bo'ladi (Render'dagi static build)."""
    folder = os.path.dirname(FFMPEG_PATH or "")
    for candidate in (
        os.path.join(folder, "ffprobe"),
        os.path.join(folder, "ffprobe.exe"),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("ffprobe") or ""


FFPROBE_PATH = find_ffprobe()


def probe_video(path):
    """(kenglik, balandlik, davomiylik) qaytaradi; aniqlab bo'lmasa None.

    Natijani send_video'ga berish uchun kerak: shunda Telegram faylni
    albatta oddiy video deb ko'rsatadi.
    """
    if not FFPROBE_PATH:
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = int(float((data.get("format") or {}).get("duration") or 0))
        return width, height, duration
    except Exception:
        return None


# --- MA'LUMOTLAR BAZASI: Firebase Firestore (+ SQLite zaxira) ---
# Foydalanuvchilar, statistika va majburiy kanallar Firestore'da saqlanadi:
# kod yangilansa, Render qayta qurilsa yoki bot boshqa joyda ishga tushsa
# ham ma'lumotlar yo'qolmaydi. Firebase sozlanmagan bo'lsa, bot avtomatik
# SQLite bilan ishlayveradi (hech qachon ishlamay qolmaydi).

FIREBASE_KEY_FILES = (
    os.environ.get("FIREBASE_KEY_FILE", ""),
    "/etc/secrets/firebase_key.json",
    "/etc/secrets/serviceAccountKey.json",
    os.path.join(BASE_DIR, "firebase_key.json"),
    os.path.join(BASE_DIR, "serviceAccountKey.json"),
)

CHANNELS_CACHE_TTL = 20  # soniya: har xabarda Firestore'ga murojaat qilmaslik uchun

_fs_client = None
_fs_ready = False
_known_users = set()
_channels_cache = []
_channels_cache_at = 0.0
_cache_lock = threading.Lock()


def _firebase_certificate():
    """Xizmat kalitini topadi: env (JSON matn yoki fayl yo'li) -> maxfiy fayl."""
    for name in (
        "FIREBASE_CREDENTIALS",
        "FIREBASE_SERVICE_ACCOUNT",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    ):
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        if value.startswith("{"):
            return fb_credentials.Certificate(json.loads(value))
        if os.path.isfile(value):
            return fb_credentials.Certificate(value)
    for path in FIREBASE_KEY_FILES:
        if path and os.path.isfile(path):
            return fb_credentials.Certificate(path)
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return fb_credentials.ApplicationDefault()
    return None


def init_firestore():
    """Firestore'ga ulanadi. Muvaffaqiyatli bo'lsa True."""
    global _fs_client, _fs_ready
    # Testlarda (va ataylab o'chirish kerak bo'lganda) bulutga tegmaslik uchun
    if _env("DISABLE_FIRESTORE"):
        print("[DB] Firestore o'chirilgan (DISABLE_FIRESTORE) — SQLite ishlatiladi.")
        return False
    if not FIREBASE_LIB:
        print("[DB] firebase-admin topilmadi — SQLite ishlatiladi.")
        return False
    try:
        certificate = _firebase_certificate()
        if certificate is None:
            print("[DB] Firebase kaliti topilmadi — SQLite ishlatiladi.")
            return False
        try:
            firebase_admin.initialize_app(certificate)
        except ValueError:
            pass  # allaqachon ishga tushirilgan
        _fs_client = fb_firestore.client()
        _fs_client.collection("users").limit(1).get()  # ulanishni tekshirish
        _fs_ready = True
        print(f"[DB] Firestore ulandi (project: {_fs_client.project})")
    except Exception as error:
        _fs_client = None
        _fs_ready = False
        print(f"[DB] Firestore ulanmadi, SQLite ishlatiladi: {error}")
    return _fs_ready


def firestore_enabled():
    return bool(_fs_ready and _fs_client is not None)


# --- SQLite (zaxira / Firebase bo'lmasa) ---
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
init_firestore()


# --- FOYDALANUVCHILAR ---
def add_user(user_id, username=None, first_name=None):
    """Foydalanuvchini saqlaydi/yangilaydi (blokdan chiqaradi)."""
    user_id = int(user_id)
    mark_update()
    if firestore_enabled():
        try:
            data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "blocked": False,
                "last_active": time.time(),
            }
            if user_id not in _known_users:
                data["created_at"] = time.time()
            _fs_client.collection("users").document(str(user_id)).set(data, merge=True)
            _known_users.add(user_id)
        except Exception as error:
            print(f"[DB] add_user xatosi: {error}")
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
    """Bloklamagan foydalanuvchilar ID'lari."""
    if firestore_enabled():
        try:
            return [
                int(doc.id)
                for doc in _fs_client.collection("users")
                .where("blocked", "==", False)
                .stream()
            ]
        except Exception as error:
            print(f"[DB] get_user_ids xatosi: {error}")
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE COALESCE(blocked, 0) = 0"
        ).fetchall()
    return [row[0] for row in rows]


def count_users():
    if firestore_enabled():
        try:
            return int(_fs_client.collection("users").count().get()[0][0].value)
        except Exception as error:
            print(f"[DB] count_users xatosi: {error}")
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_blocked():
    if firestore_enabled():
        try:
            result = (
                _fs_client.collection("users").where("blocked", "==", True).count().get()
            )
            return int(result[0][0].value)
        except Exception as error:
            print(f"[DB] count_blocked xatosi: {error}")
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(blocked, 0) = 1"
        ).fetchone()[0]


def mark_user_blocked(user_id):
    user_id = int(user_id)
    _known_users.discard(user_id)
    if firestore_enabled():
        try:
            _fs_client.collection("users").document(str(user_id)).set(
                {"blocked": True}, merge=True
            )
        except Exception as error:
            print(f"[DB] mark_user_blocked xatosi: {error}")
    with db() as conn:
        conn.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))


# --- MAJBURIY KANALLAR ---
def _reset_channels_cache():
    global _channels_cache, _channels_cache_at
    with _cache_lock:
        _channels_cache = []
        _channels_cache_at = 0.0


def get_channels():
    """[(channel_id, channel_name, invite_link), ...]"""
    global _channels_cache, _channels_cache_at
    if firestore_enabled():
        with _cache_lock:
            fresh = (time.time() - _channels_cache_at) < CHANNELS_CACHE_TTL
            if fresh:
                return list(_channels_cache)
        try:
            rows = []
            for doc in _fs_client.collection("channels").stream():
                data = doc.to_dict() or {}
                rows.append(
                    (
                        str(data.get("channel_id") or doc.id),
                        data.get("channel_name"),
                        data.get("invite_link"),
                    )
                )
            rows.sort(key=lambda row: str(row[0]))
            with _cache_lock:
                _channels_cache = rows
                _channels_cache_at = time.time()
            return list(rows)
        except Exception as error:
            print(f"[DB] get_channels xatosi: {error}")
    with db() as conn:
        return conn.execute(
            "SELECT channel_id, channel_name, invite_link FROM channels"
        ).fetchall()


def add_channel(channel_id, channel_name, invite_link):
    channel_id = str(channel_id)
    if firestore_enabled():
        try:
            _fs_client.collection("channels").document(channel_id).set(
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "invite_link": invite_link,
                },
                merge=True,
            )
        except Exception as error:
            print(f"[DB] add_channel xatosi: {error}")
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO channels (channel_id, channel_name, invite_link)
            VALUES (?, ?, ?)
            """,
            (channel_id, channel_name, invite_link),
        )
    _reset_channels_cache()


def remove_channel(identifier):
    """Kanalni ID, username yoki nomi bo'yicha o'chiradi. Topilsa True."""
    identifier = (identifier or "").strip()
    if not identifier:
        return False
    removed = False
    if firestore_enabled():
        try:
            collection = _fs_client.collection("channels")
            if collection.document(identifier).get().exists:
                collection.document(identifier).delete()
                removed = True
            else:
                for doc in collection.stream():
                    data = doc.to_dict() or {}
                    channel_id = str(data.get("channel_id") or doc.id)
                    name = data.get("channel_name") or ""
                    if (
                        channel_id.lstrip("-") == identifier.lstrip("-")
                        or name.lower() == identifier.lower()
                        or doc.id.lstrip("-") == identifier.lstrip("-")
                    ):
                        collection.document(doc.id).delete()
                        removed = True
                        break
        except Exception as error:
            print(f"[DB] remove_channel xatosi: {error}")
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM channels WHERE channel_id = ?", (identifier,)
        )
        if cursor.rowcount:
            removed = True
        else:
            rows = conn.execute(
                "SELECT channel_id, channel_name FROM channels"
            ).fetchall()
            for channel_id, name in rows:
                if (
                    str(channel_id).lstrip("-") == identifier.lstrip("-")
                    or (name or "").lower() == identifier.lower()
                ):
                    conn.execute(
                        "DELETE FROM channels WHERE channel_id = ?", (channel_id,)
                    )
                    removed = True
                    break
    if removed:
        _reset_channels_cache()
    return removed


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


def build_caption(extra=""):
    """Har bir natija ostidagi imzo (@username orqali yaratildi)."""
    sign = (
        f"🤖 @{BOT_USERNAME} orqali yaratildi."
        if BOT_USERNAME
        else "🤖 Bot orqali yaratildi."
    )
    return (extra + "\n\n" + sign) if extra else sign


def audio_keyboard():
    """Video ostidagi \"Ovozni ajratib olish\" tugmasi."""
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            text="🎧 Ovozni ajratib olish", callback_data=AUDIO_CALLBACK
        )
    )
    return keyboard


def format_duration(seconds):
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}"


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
        # Xatoning aniq sababi adminga ko'rinishi uchun matnni saqlaymiz.
        tail = " | ".join(details)[:280] or "noma'lum xato"
        raise RuntimeError(f"FFmpeg: {tail}")


def extract_audio_track(input_path, output_path):
    """Videodan ovozni mp3 qilib ajratadi. Ovoz bo'lmasa xato beradi."""
    try:
        run_ffmpeg(
            [
                FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
                "-i", input_path,
                "-vn", "-map", "0:a:0?",
                "-c:a", "libmp3lame", "-q:a", "4",
                output_path,
            ]
        )
    except RuntimeError as error:
        # Ovoz yo'q videoda ffmpeg "Output file does not contain any stream"
        # deydi — foydalanuvchiga tushunarli sabab ko'rsatamiz.
        if "does not contain any stream" in str(error):
            raise RuntimeError("bu videoda ovoz (audio) yo'q") from error
        raise
    if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
        raise RuntimeError("bu videoda ovoz (audio) yo'q")


def _frame_pixels(input_path, target_w=160):
    """Videodan bitta kadrni kichik o'lchamda RGB baytlar sifatida oladi."""
    command = [
        FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", "1", "-i", input_path, "-frames:v", "1",
        "-vf", "scale=%d:-2" % target_w,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60)
    except Exception:
        return None
    data = result.stdout or b""
    row = target_w * 3
    height = len(data) // row
    if height < 8:
        return None
    return data[: height * row], target_w, height


def _is_flat(data, width, x0, y0, step_x, step_y, count, color, tol=22):
    for i in range(count):
        offset = ((y0 + step_y * i) * width + (x0 + step_x * i)) * 3
        if (
            abs(data[offset] - color[0]) > tol
            or abs(data[offset + 1] - color[1]) > tol
            or abs(data[offset + 2] - color[2]) > tol
        ):
            return False
    return True


def detect_content_box(input_path):
    """Videodagi bo'sh hoshiyani (qora, oq yoki bir xil rangli) topadi.

    Telegram dumaloq video kvadrat bo'lishi shart. Agar yuborilgan videoda
    atrofi bir xil rangli hoshiya bo'lsa (masalan oq fonda kichik doira),
    oddiy markazdan kesish hoshiyani ham qoldirib yuboradi va dumaloq ichida
    bo'sh joy ko'rinadi. Shuning uchun chetlardan bir xil rangli chiziqlarni
    ichkariga qarab sanaymiz va faqat haqiqiy rasm maydonini qoldiramiz.

    Qaytaradi: (w, h, x, y) yoki None (hoshiya topilmasa / ishonchsiz bo'lsa).
    """
    frame = _frame_pixels(input_path)
    info = probe_video(input_path)
    if not frame or not info:
        return None
    data, width, height = frame
    frame_w, frame_h = info[0], info[1]
    if frame_w < 32 or frame_h < 32:
        return None

    limit = int(min(width, height) * 0.45)

    def color_at(x, y):
        offset = (y * width + x) * 3
        return data[offset], data[offset + 1], data[offset + 2]

    top = 0
    top_color = color_at(0, 0)
    while top < limit and _is_flat(data, width, 0, top, 1, 0, width, top_color):
        top += 1

    bottom = 0
    bottom_color = color_at(width - 1, height - 1)
    while bottom < limit and _is_flat(
        data, width, 0, height - 1 - bottom, 1, 0, width, bottom_color
    ):
        bottom += 1

    left = 0
    inner_h = max(1, height - top - bottom)
    left_color = color_at(0, top)
    while left < limit and _is_flat(
        data, width, left, top, 0, 1, inner_h, left_color
    ):
        left += 1

    right = 0
    right_color = color_at(width - 1, top)
    while right < limit and _is_flat(
        data, width, width - 1 - right, top, 0, 1, inner_h, right_color
    ):
        right += 1

    # Kichik o'lchamda hisoblash bir-ikki piksel xato beradi; hoshiya
    # qoldig'i ko'rinmasligi uchun har tomondan biroz ichkariga olamiz.
    inset = 2
    if left > inset:
        left += inset
    if right > inset:
        right += inset
    if top > inset:
        top += inset
    if bottom > inset:
        bottom += inset

    box_w = width - left - right
    box_h = height - top - bottom
    if box_w < 8 or box_h < 8:
        return None
    if left <= 1 and right <= 1 and top <= 1 and bottom <= 1:
        return None  # hoshiya yo'q

    keeps_area = (box_w * box_h) / float(width * height)
    if keeps_area < 0.25:
        return None  # juda ko'p kesilyapti — ishonchsiz

    scale_x = frame_w / float(width)
    scale_y = frame_h / float(height)
    out_w = max(16, int(round(box_w * scale_x)) // 2 * 2)
    out_h = max(16, int(round(box_h * scale_y)) // 2 * 2)
    out_x = max(0, int(round(left * scale_x)) // 2 * 2)
    out_y = max(0, int(round(top * scale_y)) // 2 * 2)
    if out_x + out_w > frame_w:
        out_x = max(0, frame_w - out_w)
    if out_y + out_h > frame_h:
        out_y = max(0, frame_h - out_h)
    return out_w, out_h, out_x, out_y


def make_square_video(input_path, output_path):
    """To'rtburchak videoni dumaloq (video note) uchun kvadratga keltiradi.

    MUHIM: Telegram video note'ni faqat kvadrat va o'lchami 640px dan
    kichik bo'lsa yaratadi; aks holda jimgina oddiy video qilib yuboradi
    (shu sabab "dumaloq bo'lmadi" muammosi chiqqan edi). Shuning uchun
    natija doim 512x512 bo'ladi — bu dumaloq ko'rinish uchun yetarli
    sifat (chatda 240-384px ko'rsatiladi).
    O'rtasidan asl piksellarda qirqiladi, fps va ovoz asl holida qoladi.
    """
    box = detect_content_box(input_path)
    trim = ""
    if box:
        box_w, box_h, box_x, box_y = box
        info = probe_video(input_path)
        if info:
            frame_w, frame_h = info[0], info[1]
            # Faqat ishonchli holatda kesamiz: hoshiya bor, lekin rasm
            # maydoni juda kichrayib ketmagan bo'lsa.
            keeps_area = (box_w * box_h) / float(frame_w * frame_h)
            if keeps_area >= 0.4 and (box_w < frame_w or box_h < frame_h):
                trim = f"crop={box_w}:{box_h}:{box_x}:{box_y},"
                print(
                    f"[SQUARE] hoshiya kesiladi: {frame_w}x{frame_h} -> "
                    f"{box_w}x{box_h}"
                )
    filter_complex = (
        "[0:v]" + trim +
        # 1) SAR (piksel nisbati) 1:1 ga keltiriladi. Ba'zi videolar 16:15,
        #    2:1 kabi nisbat bilan keladi — ular normallashtirilmasa natija
        #    cho'zilgan/toraytirilgan ("2 xil") ko'rinishda chiqadi.
        "scale='trunc(iw*sar/2)*2':'trunc(ih/2)*2',setsar=1,"
        # 2) Markazdan kvadrat qirqish — kadr dumaloqni to'liq to'ldiradi,
        #    qora hoshiya yoki kichraytirilgan kadr bo'lmaydi. O'lcham juft
        #    qilinadi (toq piksel ba'zi o'lchamlarda xato beradi).
        "crop='trunc(min(iw,ih)/2)*2':'trunc(min(iw,ih)/2)*2',"
        f"scale={SQUARE_SIZE}:{SQUARE_SIZE}:flags=lanczos,"  # video note chegarasi
        "setsar=1,format=yuv420p[v]"
    )
    command = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-t", "59",                                      # video note chegarasi
        "-map", "[v]", "-map", "0:a:0?",                 # audio bo'lmasa ham ishlaydi
        "-filter_complex", filter_complex,
        # veryfast + crf 17: o'lchovda "fast/crf18" dan ~3.5x tez,
        # sifat deyarli bir xil (SSIM 0.9975 / 0.9980)
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-threads", "2",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
        output_path,
    ]
    run_ffmpeg(command)


def make_rectangle_video(input_path, output_path):
    """Dumaloq videoni oddiy videoga aylantiradi — manba nisbatini saqlab.

    16:9 ga majburlash yo'q: kadr kesilmaydi va yon tomonlarga bo'sh joy
    qo'shilmaydi. Kvadrat (dumaloq) video kvadrat holda, to'liq kadr bilan
    chiqadi; shu sababli Telegram uni dumaloq qilib ko'rsatmaydi.
    """
    video_filter = (
        "scale=w='min(1280,trunc(iw/2)*2)':h=-2:flags=lanczos,"
        "setsar=1,format=yuv420p"
    )
    command = [
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-threads", "2",
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
    "👋 <b>Salom! Dumaloq ⇄ To'rtburchak video botiga xush kelibsiz!</b>\n\n"
    "<b>1️⃣ Video o'girish</b>\n"
    "▫️ Oddiy <b>to'rtburchak video</b> yuboring ➡️ uni <b>dumaloq video</b> qilaman.\n"
    "▫️ <b>Dumaloq video</b> yuboring ➡️ to'liq kadrli <b>to'rtburchak video</b> qilaman.\n\n"
    "<b>2️⃣ Ovozni ajratib olish</b>\n"
    "Tayyor video ostidagi <b>🎧 Ovozni ajratib olish</b> tugmasini bosing ➡️ "
    "videoning ovozi alohida (mp3) yuboriladi.\n\n"
    "<b>3️⃣ Havoladan yuklash</b>\n"
    "Instagram, YouTube, TikTok va boshqa saytlardagi video havolasini yuboring "
    "➡️ videoni yuklab beraman.\n\n"
    "⚡️ Shunchaki videoni yoki havolani yuboring — qolganini men bajaraman!"
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
                        length=SQUARE_SIZE,  # fayl o'lchamiga mos bo'lishi shart
                        reply_to_message_id=message.message_id,
                        timeout=600,
                    )
        else:
            # O'lcham va davomiylikni aniq beramiz — Telegram faylni dumaloq
            # (video note) emas, oddiy to'rtburchak video deb ko'rsatadi.
            video_kwargs = {}
            probe = probe_video(output_path)
            if probe:
                video_kwargs = {
                    "width": probe[0],
                    "height": probe[1],
                    "duration": probe[2],
                }

            def send():
                with open(output_path, "rb") as video_file:
                    return bot.send_video(
                        chat_id,
                        video_file,
                        caption=build_caption("🎥 Videongiz tayyor!"),
                        supports_streaming=True,
                        reply_markup=audio_keyboard(),
                        reply_to_message_id=message.message_id,
                        timeout=600,
                        **video_kwargs,
                    )

        try:
            with_retry(send)
        except ApiTelegramException as error:
            # Telegram dumaloq videoni rad etsa (400), foydalanuvchini xato
            # bilan qoldirmaymiz — oddiy video sifatida yuboramiz.
            if kind != "to_note" or error.error_code != 400:
                raise
            print(f"[NOTE] Telegram rad etdi, video sifatida yuborildi: {error}")
            with open(output_path, "rb") as video_file:
                with_retry(
                    lambda: bot.send_video(
                        chat_id,
                        video_file,
                        caption=build_caption("🎥 Videongiz tayyor!"),
                        supports_streaming=True,
                        reply_markup=audio_keyboard(),
                        reply_to_message_id=message.message_id,
                        timeout=600,
                    )
                )

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
        # Admin uchun aniq sabab (foydalanuvchiga ko'rsatilmaydi).
        if user_id in ADMIN_IDS:
            reason = str(error)[:280].replace("<", "&lt;").replace(">", "&gt;")
            text += f"\n\n<i>Sabab: {reason}</i>"
        safe_edit(chat_id, status_message_id, text)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        release_user(user_id)


# --- OVOZNI AJRATIB OLISH ---
def audio_job(chat_id, user_id, video):
    workdir = tempfile.mkdtemp(prefix="audio_")
    input_path = os.path.join(workdir, "input.mp4")
    output_path = os.path.join(workdir, "audio.mp3")
    try:
        file_info = bot.get_file(video.file_id)
        with open(input_path, "wb") as target:
            target.write(bot.download_file(file_info.file_path))

        extract_audio_track(input_path, output_path)

        title = f"Ovoz {format_duration(getattr(video, 'duration', 0))}".strip()
        with open(output_path, "rb") as audio_file:
            with_retry(
                lambda: bot.send_audio(
                    chat_id,
                    audio_file,
                    caption=build_caption(),
                    title=title,
                    timeout=600,
                )
            )
    except ApiTelegramException as error:
        traceback.print_exc()
        text = "❌ Ovozni ajratib bo'lmadi. Iltimos, qayta urinib ko'ring."
        if error.error_code == 400 and "too big" in str(error).lower():
            text = (
                "❌ Bu video 20 MB dan katta — Telegram bot videoni yuklab "
                "ololmaydi, shuning uchun ovozni ajratib bo'lmadi."
            )
        bot.send_message(chat_id, text)
    except Exception as error:
        traceback.print_exc()
        text = "❌ Ovozni ajratib bo'lmadi. Iltimos, qayta urinib ko'ring."
        if isinstance(error, RuntimeError) and "ovoz" in str(error):
            text = "❌ Bu videoda ovoz (audio) yo'q — ajratadigan narsa topilmadi."
        elif isinstance(error, RuntimeError) and "juda uzoq" in str(error):
            text = "❌ Video juda uzun, ovoz ajratish to'xtatildi."
        if user_id in ADMIN_IDS:
            reason = str(error)[:280].replace("<", "&lt;").replace(">", "&gt;")
            text += f"\n\n<i>Sabab: {reason}</i>"
        bot.send_message(chat_id, text, parse_mode="HTML")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        release_user(user_id)


def handle_extract_audio(call):
    video = getattr(call.message, "video", None)
    if video is None:
        safe_answer(
            call, "❌ Ovozni faqat video ostidagi tugma orqali ajratish mumkin.",
            show_alert=True,
        )
        return

    size = getattr(video, "file_size", 0) or 0
    if size > MAX_INPUT_MB * 1024 * 1024:
        safe_answer(
            call,
            f"❌ Video {MAX_INPUT_MB} MB dan katta — Telegram bot uni yuklab "
            "ololmaydi, shuning uchun ovozni ajratib bo'lmaydi.",
            show_alert=True,
        )
        return

    user_id = call.from_user.id
    if not acquire_user(user_id):
        safe_answer(call, "⏳ Oldingi amalingiz tugamadi. Iltimos, kuting.", show_alert=True)
        return

    safe_answer(call, "🎧 Ovoz ajratilmoqda, biroz kuting...")
    executor.submit(audio_job, call.message.chat.id, user_id, video)


# --- HAVOLADAN VIDEO YUKLASH (Instagram, YouTube, TikTok...) ---
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def extract_url(text):
    match = URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(").,>\"'")


def yt_cookies_file():
    """YouTube bot tekshiruvini yengish uchun cookies fayli (bo'lsa)."""
    for path in (
        os.environ.get("YT_COOKIES_FILE", ""),
        "/etc/secrets/cookies.txt",
        os.path.join(BASE_DIR, "cookies.txt"),
        os.path.join(BASE_DIR, "youtube_cookies.txt"),
    ):
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def fit_for_telegram(input_path, workdir, limit_mb=None):
    """Har qanday o'lchamdagi videoni Telegram yubora oladigan holatga keltiradi.

    Qaytaradi: fayllar ro'yxati. Bitta fayl chegaraga sig'sa — bitta element.
    Sig'masa: avval siqiladi; sifat juda tushib ketadigan bo'lsa (uzun video),
    bir necha bo'lakka bo'linadi. Shunda 1 GB video ham yuboriladi.
    """
    limit_mb = limit_mb or MAX_SEND_MB
    size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if size_mb <= limit_mb:
        return [input_path]

    info = probe_video(input_path)
    duration = info[2] if info else 0
    if not duration or duration <= 0:
        return [input_path]

    # Bitta faylga sig'dirish uchun kerak bo'ladigan umumiy bitrate (kbps)
    total_kbps = (limit_mb - 3) * 8 * 1024 / duration
    audio_kbps = 96
    video_kbps = int(total_kbps - audio_kbps)
    print(
        f"[FIT] {size_mb:.1f} MB, {duration:.0f}s — "
        f"kerakli bitrate: {total_kbps:.0f} kbps"
    )

    if video_kbps >= 320:
        out = os.path.join(workdir, "fit.mp4")
        height = 720 if video_kbps >= 900 else (480 if video_kbps >= 500 else 360)
        run_ffmpeg([
            FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-vf", f"scale=-2:'min({height},ih)':flags=lanczos,setsar=1,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.4)}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
            out,
        ])
        if os.path.getsize(out) / (1024 * 1024) <= limit_mb:
            print(f"[FIT] siqildi: {os.path.getsize(out) / (1024 * 1024):.1f} MB")
            return [out]
        print("[FIT] bitta faylga sig'madi — bo'laklarga bo'linadi")

    # Bo'laklash: sifatni saqlab, har bir bo'lak chegaradan kichik bo'lsin
    part_kbps = 800                      # bo'laklar uchun qulay sifat
    part_sec = max(20, int((limit_mb - 3) * 8 * 1024 / part_kbps))
    pattern = os.path.join(workdir, "part_%03d.mp4")
    run_ffmpeg([
        FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", "scale=-2:'min(720,ih)':flags=lanczos,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-b:v", f"{part_kbps}k", "-maxrate", f"{int(part_kbps * 1.4)}k",
        "-bufsize", f"{part_kbps * 2}k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-f", "segment", "-segment_time", str(part_sec),
        "-reset_timestamps", "1", "-movflags", "+faststart",
        pattern,
    ])
    parts = sorted(
        os.path.join(workdir, name)
        for name in os.listdir(workdir)
        if name.startswith("part_") and name.endswith(".mp4")
    )
    print(f"[FIT] {len(parts)} bo'lakka bo'lindi")
    return parts or [input_path]


def is_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url


# YouTube datacenter IP'larni tekshiradi; bitta mijoz ishlamasa boshqasini
# sinab ko'ramiz (cookies fayli bo'lsa ko'pi bilan birinchisi yetadi).
YOUTUBE_CLIENTS = ("default", "mweb", "web_safari", "web_embedded", "tv", "android_vr")


def yt_proxy():
    """Ixtiyoriy proxy: YT_PROXY (masalan http://user:pass@host:port yoki
    socks5://host:port). YouTube datacenter IP'larni bloklaydi — proxy
    bo'lsa yuklash odatda ishlaydi."""
    return _env("YT_PROXY") or _env("YOUTUBE_PROXY") or ""


# ---------------------------------------------------------------- bepul proxy
# YouTube bu serverning IP'sini bloklaydi. Qo'lda proxy (YT_PROXY) bo'lmasa,
# ochiq (bepul) proxy ro'yxatlaridan ishlaydiganini sinab tanlaymiz.
FREE_PROXY_LISTS = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&protocol=http&proxy_format=protocolipport&format=text&timeout=2000",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
)
PROXY_TEST_URL = "https://www.youtube.com/watch?v=2-WBhcjq00g"
PROXY_TTL = 900            # topilgan proxy 15 daqiqa ishlatiladi
PROXY_RETRY_AFTER = 600    # topilmasa, 2 daqiqadan keyin qayta urinamiz
PROXY_BATCH = 300
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_proxy_state = {"value": "", "at": 0.0, "fail_at": 0.0, "bad": set()}
_proxy_lock = threading.Lock()
_proxy_searching = threading.Event()


def free_proxy_enabled():
    """YT_FREE_PROXY=0 bilan o'chirish mumkin. Qo'lda proxy bo'lsa — kerak emas."""
    raw = (_env("YT_FREE_PROXY") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return not yt_proxy()


def _fetch_free_proxies():
    found = []
    for url in FREE_PROXY_LISTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read(300000).decode("utf-8", "ignore")
        except Exception as error:
            print(f"[PROXY] ro'yxat olinmadi ({url}): {error}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or " " in line:
                continue
            if not line.startswith(("http://", "https://")):
                line = "http://" + line
            if re.fullmatch(r"https?://[0-9.]+:\d{2,5}", line):
                found.append(line)
    unique = []
    seen = set()
    for proxy in found:
        if proxy not in seen:
            seen.add(proxy)
            unique.append(proxy)
    print(f"[PROXY] {len(unique)} ta bepul proxy topildi, sinaladi...")
    return unique


def _proxy_alive(proxy):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    req = urllib.request.Request(
        PROXY_TEST_URL, headers={"User-Agent": UA_BROWSER,
                                 "Accept-Language": "en-US,en;q=0.9"}
    )
    try:
        with opener.open(req, timeout=6) as resp:
            body = resp.read(500000).decode("utf-8", "ignore")
    except Exception:
        return False
    if "ytInitialPlayerResponse" not in body:
        return False
    return "Sign in to confirm" not in body


def find_free_proxy(force=False):
    if _proxy_searching.is_set():
        # boshqa ip qidirmoqda — uning natijasini kutamiz (ko'pi bilan 90 s)
        _proxy_searching.wait(timeout=90)
        return _proxy_state["value"]
    with _proxy_lock:
        if _proxy_searching.is_set():
            _proxy_searching.wait(timeout=90)
            return _proxy_state["value"]
        _proxy_searching.set()
    try:
        return _find_free_proxy_locked(force=force)
    finally:
        _proxy_searching.clear()


def _find_free_proxy_locked(force=False):
    now = time.time()
    if not force:
        if _proxy_state["value"] and now - _proxy_state["at"] < PROXY_TTL:
            return _proxy_state["value"]
        if now - _proxy_state["fail_at"] < PROXY_RETRY_AFTER:
            return ""
    if len(_proxy_state["bad"]) > 3000:
        _proxy_state["bad"].clear()
    candidates = [p for p in _fetch_free_proxies() if p not in _proxy_state["bad"]]
    random.shuffle(candidates)
    candidates = candidates[:PROXY_BATCH]
    if not candidates:
        _proxy_state["fail_at"] = now
        return ""
    executor = ThreadPoolExecutor(max_workers=50)
    try:
        futures = {executor.submit(_proxy_alive, p): p for p in candidates}
        for future in _futures_as_completed(futures):
            proxy = futures[future]
            try:
                ok = future.result()
            except Exception:
                ok = False
            if ok:
                _proxy_state.update(value=proxy, at=time.time(), fail_at=0.0)
                print(f"[PROXY] ishlaydigan bepul proxy: {proxy}")
                return proxy
            _proxy_state["bad"].add(proxy)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    _proxy_state["fail_at"] = time.time()
    print("[PROXY] hozircha ishlaydigan bepul proxy topilmadi")
    return ""


def _futures_as_completed(futures):
    while futures:
        done = [f for f in futures if f.done()]
        if not done:
            time.sleep(0.2)
            continue
        for future in done:
            yield future
        futures = {f: p for f, p in futures.items() if f not in done}


def resolve_proxy(url, force=False):
    """Qo'lda proxy bo'lsa — o'sha; bo'lmasa YouTube uchun bepul proxy."""
    manual = yt_proxy()
    if manual:
        return manual
    if free_proxy_enabled() and is_youtube_url(url):
        return find_free_proxy(force=force)
    return ""


def _yt_options(url, workdir, client=None):
    options = {
        "outtmpl": os.path.join(workdir, "%(title).80B.%(ext)s"),
        # Hajm chegarasi yo'q: kerak bo'lsa keyin siqiladi yoki bo'laklarga
        # bo'linadi. 720p dan yuqorisini yuklash ortiqcha vaqt oladi.
        "format": (
            "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/"
            "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "concurrent_fragment_downloads": 1,
    }
    folder = os.path.dirname(FFMPEG_PATH or "")
    if folder and os.path.isdir(folder):
        options["ffmpeg_location"] = folder
    proxy = resolve_proxy(url)
    if proxy:
        options["proxy"] = proxy
    cookies = yt_cookies_file()
    if cookies:
        # Render "Secret Files" faqat o'qish uchun mount qilinadi, yt-dlp esa
        # ish oxirida cookie faylini qayta yozmoqchi bo'ladi (OSError: Read-only
        # file system). Shuning uchun ishchi papkaga nusxa olamiz.
        local_cookies = os.path.join(workdir, "cookies.txt")
        try:
            shutil.copyfile(cookies, local_cookies)
            options["cookiefile"] = local_cookies
        except OSError as error:
            print(f"[YTDLP] cookie nusxalanmadi ({error}); asl fayl ishlatiladi")
            options["cookiefile"] = cookies
    if client and client != "default":
        options["extractor_args"] = {"youtube": {"player_client": [client]}}
    return options


def _is_bot_check(error):
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "sign in to confirm",
            "failed to extract",
            "confirm you’re not a bot",
            "confirm you're not a bot",
            "player response",
            "http error 429",
        )
    )


def download_by_url(url, workdir):
    """Havoladan videoni yuklaydi va (fayl yo'li, sarlavha) qaytaradi."""
    clients = YOUTUBE_CLIENTS if is_youtube_url(url) else ("default",)
    info = None
    last_error = None
    for client in clients:
        try:
            with yt_dlp.YoutubeDL(_yt_options(url, workdir, client)) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception as error:  # noqa: BLE001 - mijozni almashtirib ko'ramiz
            last_error = error
            if not _is_bot_check(error):
                raise
            print(f"[YTDLP] {client} mijozida bloklandi, keyingisiga o'tamiz")
    if info is None:
        raise last_error or RuntimeError("havoladan video topilmadi")

    # Merge bo'lsa kengaytma o'zgaradi — papkadagi eng katta faylni olamiz.
    files = [
        os.path.join(workdir, name)
        for name in os.listdir(workdir)
        if not name.endswith((".part", ".ytdl", ".jpg", ".webp", ".png"))
    ]
    if not files:
        raise RuntimeError("havoladan video topilmadi")
    path = max(files, key=os.path.getsize)
    title = (info or {}).get("title") or "Video"
    return path, str(title)[:120]


def ensure_mp4(input_path, workdir):
    """Telegram video sifatida qabul qilishi uchun mp4 konteynerga keltiradi."""
    if input_path.lower().endswith((".mp4", ".m4v")):
        return input_path
    output_path = os.path.join(workdir, "converted.mp4")
    run_ffmpeg(
        [
            FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
            "-i", input_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-movflags", "+faststart",
            output_path,
        ]
    )
    return output_path


def start_link_download(message, url):
    user = message.from_user
    add_user(user.id, user.username, user.first_name)

    if not check_subscription(user.id):
        bot.send_message(
            message.chat.id,
            "❌ Botdan foydalanish uchun kanallarga a'zo bo'ling:",
            reply_markup=get_sub_keyboard(),
        )
        return

    if not YTDLP_LIB:
        bot.reply_to(
            message, "❌ Serverda yt-dlp o'rnatilmagan. Admin bilan bog'laning."
        )
        return

    if not ffmpeg_ready():
        bot.reply_to(
            message, "❌ Serverda FFmpeg topilmadi. Admin bilan bog'laning."
        )
        return

    if not acquire_user(user.id):
        bot.reply_to(
            message, "⏳ Oldingi videongiz hali tayyorlanmoqda. Iltimos, kuting."
        )
        return

    try:
        status = bot.reply_to(message, "⬇️ Havoladan video yuklanmoqda, biroz kuting...")
        executor.submit(link_job, message, url, status.message_id)
    except Exception:
        release_user(user.id)
        raise


def link_job(message, url, status_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    workdir = tempfile.mkdtemp(prefix="link_")

    try:
        path, title = download_by_url(url, workdir)
        path = ensure_mp4(path, workdir)

        # Hajmi qanday bo'lishidan qat'i nazar yuboriladi: sig'masa siqiladi,
        # juda uzun bo'lsa bo'laklarga bo'linadi.
        parts = fit_for_telegram(path, workdir)
        total = len(parts)
        if total > 1:
            safe_edit(
                chat_id,
                status_message_id,
                f"⬇️ Yuklandi, {total} bo'lakka bo'lindi — yuborilmoqda...",
            )

        for index, part in enumerate(parts, 1):
            video_kwargs = {}
            probe = probe_video(part)
            if probe:
                video_kwargs = {
                    "width": probe[0],
                    "height": probe[1],
                    "duration": probe[2],
                }
            caption = f"⬇️ {title}" if total == 1 else f"⬇️ {title} ({index}/{total})"
            try:
                with open(part, "rb") as video_file:
                    with_retry(
                        lambda video_file=video_file, caption=caption,
                        video_kwargs=video_kwargs: bot.send_video(
                            chat_id,
                            video_file,
                            caption=build_caption(caption),
                            supports_streaming=True,
                            reply_markup=audio_keyboard(),
                            reply_to_message_id=message.message_id,
                            timeout=1800,
                            **video_kwargs,
                        )
                    )
            except Exception:
                traceback.print_exc()
                if total == 1:
                    raise

        try:
            bot.delete_message(chat_id, status_message_id)
        except Exception:
            pass

    except Exception as error:
        traceback.print_exc()
        reason = str(error)
        text = (
            "❌ Havoladan videoni yuklab bo'lmadi.\n"
            "Havola ochiq (private emas) ekanini tekshirib, qayta urinib ko'ring."
        )
        lowered = reason.lower()
        if "login" in lowered or "cookies" in lowered or "private" in lowered:
            text = (
                "❌ Bu video yopiq (private) yoki login talab qiladi — "
                "uni yuklab bo'lmadi."
            )
        elif "read-only file system" in lowered and "cookies" in lowered:
            text = (
                "❌ Cookie faylini o'qishda xatolik. Admin: Render'dagi "
                "cookies.txt ni tekshiring."
            )
        elif is_youtube_url(url) and _is_bot_check(reason):
            text = (
                "❌ YouTube bu videoni serverdan yuklashga ruxsat bermadi "
                "(bot tekshiruvi).\n"
                "Video faylini to'g'ridan-to'g'ri yuborsangiz, konvertatsiya "
                "ishlaydi."
            )
        elif "juda katta" in lowered:
            text = (
                f"❌ Video {MAX_SEND_MB} MB dan katta — Telegram bot bunday "
                "faylni yubora olmaydi. Qisqaroq video tanlang."
            )
        elif "unsupported url" in lowered:
            text = "❌ Bu havola qo'llab-quvvatlanmaydi. Video sahifasining havolasini yuboring."

        if user_id in ADMIN_IDS:
            safe = reason[:280].replace("<", "&lt;").replace(">", "&gt;")
            text += f"\n\n<i>Sabab: {safe}</i>"
        safe_edit(chat_id, status_message_id, text, parse_mode="HTML")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        release_user(user_id)


@bot.message_handler(
    content_types=["text"],
    chat_types=["private"],
    func=lambda m: bool(extract_url(m.text)) and not (m.text or "").lstrip().startswith("/"),
)
def handle_link(message):
    # Admin amali (kanal havolasi, reklama posti) bilan aralashmasin.
    if (message.chat.id, message.message_id) in consumed_messages:
        return
    if admin_steps.get(message.from_user.id):
        return
    start_link_download(message, extract_url(message.text))


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
    mark_update()

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
                "Menga video yoki Instagram/YouTube havolasini yuboring — "
                "qolganini o'zim bajaraman!",
                parse_mode="HTML",
            )
        else:
            safe_answer(call, "❌ Hali hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)
        return

    if data == "noop":
        safe_answer(call, "Bu kanal uchun havola sozlanmagan.")
        return

    if data == AUDIO_CALLBACK:
        handle_extract_audio(call)
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
        blocked = count_blocked()
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


# --- DIAGNOSTIKA VA WATCHDOG ---
_pending_cache = {"time": 0.0, "value": None}


def mark_update():
    """Oxirgi yangilanish qachon kelganini yozib qo'yadi (watchdog uchun)."""
    STATE["last_update"] = time.time()


def telegram_pending_count():
    """Telegram navbatida kutayotgan yangilanishlar soni (10 s kesh)."""
    now = time.time()
    if now - _pending_cache["time"] < 10:
        return _pending_cache["value"]
    try:
        info = bot.get_webhook_info()
        value = getattr(info, "pending_update_count", None)
    except Exception:
        value = None
    _pending_cache.update(time=now, value=value)
    return value


def status_snapshot():
    """Bot holati — /status endpoint va /status buyrug'i uchun."""
    now = time.time()
    last = STATE["last_update"]
    data = {
        "build": BUILD_VERSION,
        "bot": BOT_USERNAME or None,
        "uptime_sec": int(now - STARTED_AT),
        "polling": STATE["polling"],
        "last_poll_note": STATE["last_poll_note"],
        "conflicts_409": STATE["conflicts_409"],
        "poll_restarts": STATE["poll_restarts"],
        "seconds_since_update": int(now - last) if last else None,
        "seconds_since_poll_ok": int(now - (STATE["last_poll_ok"] or STARTED_AT)),
        "pending_updates": telegram_pending_count(),
        "ffmpeg": ffmpeg_ready(),
        "ffprobe": bool(FFPROBE_PATH),
        "yt_dlp": YTDLP_LIB,
        "cookies": bool(yt_cookies_file()),
        "proxy": bool(yt_proxy()) or bool(_proxy_state["value"]),
        "proxy_mode": "manual" if yt_proxy() else (
            "auto" if free_proxy_enabled() else "off"
        ),
        "free_proxy": _proxy_state["value"] or "",
    }
    for key, getter in (
        ("users", count_users),
        ("channels", lambda: len(get_channels())),
        ("firestore", firestore_enabled),
    ):
        try:
            data[key] = getter()
        except Exception as error:
            data[key] = f"xato: {error}"
    return data


def _poll_once(offset):
    """get_updates ni chaqiradi, telebot versiyalari imzosi turlicha bo'lsa ham.

    Ba'zi versiyalarda `long_polling_timeout` bor, ba'zilarida yo'q; noto'g'ri
    kalit so'z TypeError beradi. Bunday holda eng oddiy imzoga o'tamiz —
    polling hech qachon shu sababdan to'xtab qolmasin.
    """
    try:
        return bot.get_updates(
            offset=offset,
            timeout=15,
            long_polling_timeout=30,
            allowed_updates=ALLOWED_UPDATES,
        )
    except TypeError as error:
        print(f"[POLL] get_updates imzosi mos kelmadi ({error}); oddiy variant...")
        return bot.get_updates(
            offset=offset,
            timeout=30,
            allowed_updates=ALLOWED_UPDATES,
        )


def watchdog_loop():
    """Polling umuman javob bermay qolsa adminni ogohlantiradi va logga yozadi.

    Polling so'rovlarining o'zida qat'iy timeout bor, shuning uchun qotib
    qolgan socket o'zi uziladi; bu nazorat esa tizimli muammoni erta
    ko'rsatadi (masalan, Telegram bloklangan yoki token boshqa joyda).
    """
    last_alert = 0.0
    while True:
        time.sleep(60)
        try:
            last = STATE["last_poll_ok"] or STARTED_AT
            idle = time.time() - last
            if idle < 180:
                continue
            pending = telegram_pending_count()
            print(
                f"[WATCHDOG] {int(idle)}s davomida Telegramdan javob yo'q "
                f"(navbatda {pending} ta) — sabab: {STATE['last_poll_note']}"
            )
            if time.time() - last_alert > 900:
                last_alert = time.time()
                for admin_id in ADMIN_IDS:
                    try:
                        bot.send_message(
                            admin_id,
                            "⚠️ Bot Telegramdan yangilanish olmayapti "
                            f"({int(idle // 60)} daqiqa, navbatda {pending} ta).\n"
                            f"Sabab: {STATE['last_poll_note']}\n"
                            f"Holat: /status [BUILD {BUILD_VERSION}]",
                        )
                    except Exception:
                        pass
        except Exception as error:  # watchdog hech qachon o'chmasin
            print(f"[WATCHDOG] xato: {error}")


# --- RENDER UCHUN HEALTH-CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status"):
            try:
                payload = json.dumps(status_snapshot(), ensure_ascii=False, indent=1)
            except Exception as error:
                payload = json.dumps({"error": str(error)})
            body = payload.encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = b"Bot is alive and running 24/7!"
            ctype = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def run_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()


@bot.message_handler(commands=["status"], chat_types=["private"])
def status_handler(message):
    if not is_admin(message.from_user.id):
        return
    data = status_snapshot()
    lines = [
        "🩺 <b>Bot holati</b>",
        f"▪️ Build: <code>{data['build']}</code>",
        f"▪️ Bot: @{data['bot'] or '-'}",
        f"▪️ Ishlagan vaqt: {data['uptime_sec'] // 60} daq",
        f"▪️ Polling: {'✅' if data['polling'] else '❌'} "
        f"({data['last_poll_note'] or '-'})",
        f"▪️ 409 konflikt: {data['conflicts_409']}, qayta ishga tushirish: {data['poll_restarts']}",
        f"▪️ Navbatda: {data['pending_updates']} ta yangilanish",
        f"▪️ Oxirgi xabar: {data['seconds_since_update']} s oldin",
        f"▪️ Foydalanuvchilar: {data['users']} | Kanallar: {data['channels']}",
        f"▪️ Firestore: {data['firestore']} | yt-dlp: {data['yt_dlp']}",
        f"▪️ Cookies: {'✅' if data['cookies'] else '❌'} | "
        f"Proxy: {'✅' if data['proxy'] else '❌'} ({data['proxy_mode']})",
    ]
    try:
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    except Exception:
        bot.send_message(message.chat.id, "\n".join(lines).replace("<", ""))


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
    threading.Thread(target=watchdog_loop, daemon=True).start()
    print("Watchdog ishga tushdi (qotib qolgan pollingni o'zi tiklaydi).")

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
                        types.BotCommand("status", "Bot holati"),
                    ],
                    scope=types.BotCommandScopeChat(admin_id),
                )
            except Exception:
                pass
    except Exception as error:
        print(f"[COMMANDS] {error}")

    # Bot tavsifi (BotFather'dagi "What can this bot do?" va profil matni)
    try:
        bot.set_my_description(
            "🎥 Videoni dumaloq ⇄ to'rtburchak qilib beraman.\n"
            "🎧 Video ovozini alohida mp3 qilib ajrataman.\n"
            "⬇️ Instagram, YouTube, TikTok havolalaridan video yuklayman.\n\n"
            "Videoni yoki havolani shunchaki yuboring!"
        )
        bot.set_my_short_description(
            "Dumaloq ⇄ to'rtburchak video, ovozni ajratish va havoladan yuklash."
        )
    except Exception as error:
        print(f"[DESCRIPTION] {error}")

    # Admin deploydan keyin darhol bilishi uchun (sendMessage getUpdates'ga
    # bog'liq emas — polling qotib qolsa ham yetib boradi).
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                "✅ <b>Bot ishga tushdi</b>\n"
                f"▪️ Build: <code>{BUILD_VERSION}</code>\n"
                f"▪️ Bot: @{BOT_USERNAME or '-'}\n"
                f"▪️ Holatni ko'rish: /status",
                parse_mode="HTML",
            )
            break
        except Exception as error:
            print(f"[BOOT] xabar yuborilmadi: {error}")

    print(f"Bot muvaffaqiyatli ishga tushdi... [BUILD {BUILD_VERSION}]")
    if free_proxy_enabled():
        threading.Thread(target=find_free_proxy, daemon=True).start()

    # Polling ATAYIN qo'lda yozilgan (telebot'ning infinity_polling'i o'rniga):
    # 1) har bir so'rovda qat'iy timeout bor — socket jim qotib qolmaydi;
    # 2) nima bo'layotgani logda va /status da ko'rinadi;
    # 3) 409 / 401 / tarmoq xatolari aniq hal qilinadi.
    # Telebot'ning ichki polling'ida timeout yo'qligi sabab jarayon "polling
    # ishlayapti" deb turib, Telegramdan birorta yangilanish olmay qolgan edi.
    offset = None
    while True:
        STATE["polling"] = True
        try:
            # telebot 4.37 imzosi: get_updates(offset, limit, timeout,
            # allowed_updates, long_polling_timeout).
            # timeout -> tarmoq (ulanish/o'qish) chegarasi, long_polling_timeout
            # -> Telegram serverida kutish vaqti. Ikkalasi ham chegaralangan,
            # shuning uchun socket jim osilib qolmaydi.
            updates = _poll_once(offset)
        except ApiTelegramException as error:
            code = getattr(error, "error_code", None)
            STATE["last_poll_note"] = f"{code}: {str(error)[:100]}"
            if code == 409:
                STATE["conflicts_409"] += 1
                print(
                    "[POLL] 409 Conflict: bu token bilan boshqa nusxa bot ishlayapti.\n"
                    "       Render'da eski deploy hali o'chmagan bo'lishi mumkin, yoki\n"
                    "       kompyuterda/boshqa serverda bot.py ochiq qolgan."
                )
                time.sleep(20)
                if STATE["conflicts_409"] in (1, 5, 20):
                    for admin_id in ADMIN_IDS:
                        try:
                            bot.send_message(
                                admin_id,
                                "⚠️ 409 Conflict: bot tokeni bilan boshqa nusxa "
                                "ishlayapti. Eski deployni to'xtating.",
                            )
                        except Exception:
                            pass
                continue
            if code in (401, 404):
                print(f"[POLL] Token qabul qilinmadi ({code}). To'xtatildi.")
                sys.exit(1)
            print(f"[POLL] Telegram xatosi {code}: {error}. 5 s dan keyin qayta...")
            time.sleep(5)
            continue
        except Exception as error:  # noqa: BLE001 - bot hech qachon o'chmasin
            STATE["last_poll_note"] = f"tarmoq: {str(error)[:100]}"
            print(f"[POLL] Tarmoq xatosi: {error}. 5 s dan keyin qayta...")
            time.sleep(5)
            continue

        STATE["last_poll_ok"] = time.time()
        STATE["last_poll_note"] = f"ok ({len(updates)} ta)"
        if not updates:
            continue

        offset = updates[-1].update_id + 1
        print(f"[POLL] {len(updates)} ta yangilanish olindi (offset={offset})")
        try:
            bot.process_new_updates(updates)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
