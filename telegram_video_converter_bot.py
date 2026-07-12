import os
import sqlite3
import subprocess
import threading
import traceback
import html
import platform
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot import TeleBot, types
from telebot import apihelper

# --- BOT SOZLAMALARI ---
BOT_TOKEN = "8357374445:AAFJfx3qYEc-fWVEZ-T-O3tILQ5UO74m6Lc"  # Bot Token
ADMIN_ID = 6377032074                                       # Admin ID

# Tarmoq ulanish xatoliklari va timeout muammolarini hal qilish uchun kutish vaqtlarini oshiramiz (120 soniya)
apihelper.CONNECT_TIMEOUT = 120
apihelper.READ_TIMEOUT = 120

# Botni yaratish
bot = TeleBot(BOT_TOKEN, threaded=True)

# --- FFmpeg YO'LINI ANIQLASH (Windows va Linux/Render uchun) ---
# Tizim Windows bo'lsa Windows papkasini, Linux bo'lsa Render-dagi yuklangan static FFmpeg-ni oladi
if platform.system() == "Windows":
    FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
else:
    # Render (Linux) uchun build.sh yuklab beradigan joy
    FFMPEG_PATH = os.path.join(os.getcwd(), "ffmpeg_bin", "ffmpeg")
    # Agar u yerda topilmasa, tizim standart ffmpeg buyrug'iga tayanadi
    if not os.path.exists(FFMPEG_PATH):
        FFMPEG_PATH = "ffmpeg"

# --- MA'LUMOTLAR BAZASI (SQLite) ---
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Foydalanuvchilar jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
    """)
    # Majburiy a'zolik kanallari jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            invite_link TEXT
        )
    """)
    conn.commit()
    conn.close()

# Ma'lumotlar bazasini yaratish/tekshirish
init_db()

def add_user(user_id, username):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

def get_channels():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, channel_name, invite_link FROM channels")
    channels = cursor.fetchall()
    conn.close()
    return channels

def add_channel(channel_id, channel_name, invite_link):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO channels (channel_id, channel_name, invite_link) VALUES (?, ?, ?)",
                   (channel_id, channel_name, invite_link))
    conn.commit()
    conn.close()

def remove_channel(channel_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()

# --- MAJBURIY A'ZOLIK TEKSHIRUVI ---
def check_subscription(user_id):
    channels = get_channels()
    if not channels:
        return True

    for channel_id, name, link in channels:
        try:
            member = bot.get_chat_member(channel_id, user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception:
            continue
    return True

def get_sub_keyboard(user_id):
    keyboard = types.InlineKeyboardMarkup()
    channels = get_channels()
    for channel_id, name, link in channels:
        keyboard.add(types.InlineKeyboardButton(text=name, url=link))
    keyboard.add(types.InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub"))
    return keyboard

# --- FFmpeg TIZIMDA BORLIGINI TEKSHIRISH ---
def is_ffmpeg_installed():
    if os.path.exists(FFMPEG_PATH):
        return True
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False

# --- FFmpeg ORQALI VIDEOLARNI QAYTA ISHLASH ---
def make_square_video(input_path, output_path):
    command = [
        FFMPEG_PATH, '-y', '-i', input_path,
        '-t', '60',  # Maksimal 60 soniya
        '-vf', "crop='min(iw,ih)':'min(iw,ih)',scale=480:480",
        '-c:v', 'libx264', '-profile:v', 'baseline', '-level', '3.0', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k', '-strict', '-2',
        output_path
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg xatosi: {result.stderr}")

def make_normal_video(input_path, output_path):
    command = [
        FFMPEG_PATH, '-y', '-i', input_path,
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        output_path
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"FFmpeg xatosi: {result.stderr}")

# --- ADMIN PANEL KLAVIATURALARI ---
def get_admin_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
        types.InlineKeyboardButton(text="📢 Ommaviy xabar", callback_data="admin_broadcast")
    )
    keyboard.add(
        types.InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin_add_channel"),
        types.InlineKeyboardButton(text="➖ Kanalni o'chirish", callback_data="admin_del_channel")
    )
    keyboard.add(types.InlineKeyboardButton(text="📋 Kanallar ro'yxati", callback_data="admin_list_channels"))
    return keyboard

def get_admin_reply_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    keyboard.add(types.KeyboardButton("⚙️ Admin Panel"))
    return keyboard

# --- HANDLERS (START & ADMIN) ---

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    username = message.from_user.username or "No Username"
    add_user(user_id, username)

    if not check_subscription(user_id):
        bot.send_message(
            message.chat.id, 
            "👋 Salom! Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:", 
            reply_markup=get_sub_keyboard(user_id)
        )
        return

    instructions = (
        "👋 <b>Salom! Video Konverter botga xush kelibsiz!</b>\n\n"
        "Men avtomatik rejimda ishlayman:\n"
        "1️⃣ Menga oddiy <b>To'rtburchak video</b> yuboring ➡️ Men uni <b>Dumaloq video (Teleskop)</b> qilib beraman.\n"
        "2️⃣ Menga <b>Dumaloq video</b> yuboring ➡️ Men uni oddiy yuklab olinadigan <b>To'rtburchak video</b> qilib beraman.\n\n"
        "⚡️ Shunchaki videoni yuboring va natijani oling!"
    )

    if user_id == ADMIN_ID:
        bot.send_message(message.chat.id, instructions, parse_mode="HTML", reply_markup=get_admin_reply_keyboard())
    else:
        bot.send_message(message.chat.id, instructions, parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "⚙️ <b>Admin Panelga xush kelibsiz!</b>\nKerakli bo'limni tanlang:",
                     reply_markup=get_admin_keyboard(), parse_mode="HTML")

@bot.message_handler(func=lambda message: message.text == "⚙️ Admin Panel")
def admin_button_click_handler(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "⚙️ <b>Admin Panelga xush kelibsiz!</b>\nKerakli bo'limni tanlang:",
                     reply_markup=get_admin_keyboard(), parse_mode="HTML")

# --- VIDEO QAYTA ISHLASH (MAIN LOGIC) ---

@bot.message_handler(content_types=['video'])
def handle_normal_video(message):
    user_id = message.from_user.id
    if not check_subscription(user_id):
        bot.send_message(message.chat.id, "❌ Botdan foydalanish uchun kanallarga a'zo bo'ling:", reply_markup=get_sub_keyboard(user_id))
        return

    if not is_ffmpeg_installed():
        bot.reply_to(message, "❌ Tizimda FFmpeg dasturi topilmadi! Iltimos, ffmpeg papkasi C:\\ diskida borligini tekshiring.")
        return

    if message.video.file_size > 20 * 1024 * 1024:
        bot.reply_to(message, "⚠️ Video hajmi juda katta! 20 MB gacha video yuboring.")
        return

    status_msg = bot.reply_to(message, "⏳ Videongiz qabul qilindi. Dumaloq shaklga keltirilmoqda, iltimos kuting...")

    def process():
        input_name = f"in_{message.chat.id}_{message.message_id}.mp4"
        output_name = f"out_{message.chat.id}_{message.message_id}.mp4"
        try:
            file_info = bot.get_file(message.video.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            with open(input_name, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            make_square_video(input_name, output_name)
            
            with open(output_name, 'rb') as video_note:
                bot.send_video_note(message.chat.id, video_note, reply_to_message_id=message.message_id, timeout=120)
            
            if os.path.exists(input_name): os.remove(input_name)
            if os.path.exists(output_name): os.remove(output_name)
            bot.delete_message(message.chat.id, status_msg.message_id)
            
        except Exception as e:
            traceback.print_exc()
            safe_error = html.escape(str(e)[:100])
            bot.edit_message_text(f"❌ Videoni qayta ishlashda xatolik yuz berdi.\nBatafsil: {safe_error}", 
                                  message.chat.id, status_msg.message_id)
            if os.path.exists(input_name): os.remove(input_name)
            if os.path.exists(output_name): os.remove(output_name)

    threading.Thread(target=process).start()

@bot.message_handler(content_types=['video_note'])
def handle_round_video(message):
    user_id = message.from_user.id
    if not check_subscription(user_id):
        bot.send_message(message.chat.id, "❌ Botdan foydalanish uchun kanallarga a'zo bo'ling:", reply_markup=get_sub_keyboard(user_id))
        return

    if not is_ffmpeg_installed():
        bot.reply_to(message, "❌ Tizimda FFmpeg dasturi topilmadi!")
        return

    status_msg = bot.reply_to(message, "⏳ Dumaloq video qabul qilindi. Oddiy video formatiga o'tkazilmoqda...")

    def process():
        input_name = f"in_{message.chat.id}_{message.message_id}.mp4"
        output_name = f"out_{message.chat.id}_{message.message_id}.mp4"
        try:
            file_info = bot.get_file(message.video_note.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            with open(input_name, 'wb') as new_file:
                new_file.write(downloaded_file)
            
            make_normal_video(input_name, output_name)
            
            with open(output_name, 'rb') as video:
                bot.send_video(message.chat.id, video, reply_to_message_id=message.message_id, caption="🎥 Videongiz tayyor!", timeout=120)
            
            if os.path.exists(input_name): os.remove(input_name)
            if os.path.exists(output_name): os.remove(output_name)
            bot.delete_message(message.chat.id, status_msg.message_id)
            
        except Exception as e:
            traceback.print_exc()
            safe_error = html.escape(str(e)[:100])
            bot.edit_message_text(f"❌ Qayta ishlashda xatolik yuz berdi.\nBatafsil: {safe_error}", 
                                  message.chat.id, status_msg.message_id)
            if os.path.exists(input_name): os.remove(input_name)
            if os.path.exists(output_name): os.remove(output_name)

    threading.Thread(target=process).start()

# --- CALLBACKS & ADMIN PANEL ---

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    user_id = call.from_user.id

    if call.data == "check_sub":
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "✅ Rahmat! Barcha kanallarga a'zo bo'ldingiz.", show_alert=True)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            instructions = (
                "👋 <b>Siz muvaffaqiyatli ro'yxatdan o'tdingiz!</b>\n\n"
                "Menga oddiy video yoki dumaloq video yuboring, men uni avtomatik o'zgartirib beraman!"
            )
            bot.send_message(call.message.chat.id, instructions, parse_mode="HTML")
        else:
            bot.answer_callback_query(call.id, "❌ Hali hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)

    elif user_id == ADMIN_ID:
        if call.data == "admin_stats":
            users_count = len(get_all_users())
            channels = get_channels()
            text = f"📊 <b>Bot Statistikasi:</b>\n\n👥 Foydalanuvchilar soni: {users_count} ta\n📢 Majburiy kanallar soni: {len(channels)} ta"
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                                  reply_markup=get_admin_keyboard(), parse_mode="HTML")
            
        elif call.data == "admin_broadcast":
            msg = bot.send_message(call.message.chat.id, "📝 Ommaviy xabar matnini yuboring:")
            bot.register_next_step_handler(msg, process_broadcast)
            
        elif call.data == "admin_add_channel":
            msg = bot.send_message(call.message.chat.id, 
                                   "📢 <b>Yangi kanal qo'shish</b>\n\n"
                                   "Ushbu usullardan biri orqali kanal qo'shishingiz mumkin:\n\n"
                                   "1️⃣ <b>Yo'naltirish (Tavsiya etiladi):</b> Kanaldagi istalgan bir postni botga <b>Forward (yo'naltirib)</b> yuboring!\n\n"
                                   "2️⃣ <b>Username yozish:</b> Kanal username yoki havolasini yuboring.\n"
                                   "   <i>Misol: @abduIIayev_09 yoki https://t.me/abduIIayev_09</i>\n\n"
                                   "⚠️ *Bot ushbu kanalda administrator bo'lishi shart!*", parse_mode="HTML")
            bot.register_next_step_handler(msg, process_add_channel)
            
        elif call.data == "admin_del_channel":
            msg = bot.send_message(call.message.chat.id, "O'chirmoqchi bo'lgan kanalingizning **Kanal ID yoki Username** ini yuboring:")
            bot.register_next_step_handler(msg, process_del_channel)
            
        elif call.data == "admin_list_channels":
            channels = get_channels()
            if not channels:
                bot.send_message(call.message.chat.id, "Sizda majburiy a'zolik kanallari sozlanmagan.")
                return
            text = "📋 <b>Majburiy a'zolik kanallari ro'yxati:</b>\n\n"
            for cid, name, link in channels:
                text += f"▪️ <b>{html.escape(name)}</b>\n   └ ID: <code>{html.escape(cid)}</code>\n   └ Havola: {html.escape(link)}\n\n"
            bot.send_message(call.message.chat.id, text, parse_mode="HTML")

# --- ADMIN FUNKSIYALARI ---

def process_broadcast(message):
    users = get_all_users()
    sent = 0
    failed = 0
    status = bot.send_message(message.chat.id, "🚀 Xabar yuborilmoqda, iltimos kuting...")
    for uid in users:
        try:
            bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except Exception:
            failed += 1
    bot.edit_message_text(f"✅ <b>Ommaviy xabar yakunlandi!</b>\n\n📥 Muvaffaqiyatli: {sent} ta\n❌ Yetib bormadi: {failed} ta", 
                          message.chat.id, status.message_id, parse_mode="HTML")

def process_add_channel(message):
    try:
        # 1. AGAR ADMIN KANALDAN POST YO'NALTIRGAN (FORWARD) BO'LSA
        if message.forward_from_chat and message.forward_from_chat.type == "channel":
            chat = message.forward_from_chat
            channel_id = str(chat.id)
            channel_name = chat.title
            invite_link = f"https://t.me/{chat.username}" if chat.username else "https://t.me/"
            
            add_channel(channel_id, channel_name, invite_link)
            
            safe_name = html.escape(channel_name)
            bot.send_message(message.chat.id, f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n📢 Nomi: {safe_name}", parse_mode="HTML")
            return

        # 2. AGAR ADMIN MATN KO'RINISHIDA USERNAME YOKI HAVOLA YUBORGAN BO'LSA
        if not message.text:
            bot.send_message(message.chat.id, "❌ Iltimos, kanal username'ini yuboring yoki biror postni yo'naltiring!")
            return

        text = message.text.strip()
        
        # ID | Nomi | Havola formatida bo'lsa
        if "|" in text:
            parts = text.split("|")
            channel_id = parts[0].strip()
            channel_name = parts[1].strip()
            invite_link = parts[2].strip()
        else:
            channel_id = text
            # t.me linklarini username'ga o'tkazish
            if "t.me/" in channel_id:
                parts = channel_id.split("t.me/")
                username_part = parts[1].split("/")[0].split("?")[0].strip()
                if not username_part.startswith("+") and not username_part.startswith("joinchat"):
                    channel_id = "@" + username_part
            
            # Formatsiz username'ga @ qo'shish
            if not channel_id.startswith("@") and not channel_id.startswith("-"):
                if not channel_id.replace("-", "").isdigit():
                    channel_id = "@" + channel_id
            
            # Telegram API orqali kanal ma'lumotlarini olish
            chat = bot.get_chat(channel_id)
            channel_name = chat.title
            invite_link = f"https://t.me/{chat.username}" if chat.username else "https://t.me/"
            channel_id = str(chat.id)

        add_channel(channel_id, channel_name, invite_link)
        
        safe_name = html.escape(channel_name)
        safe_id = html.escape(channel_id)
        safe_link = html.escape(invite_link)
        
        success_text = (
            f"✅ <b>Kanal muvaffaqiyatli qo'shildi!</b>\n\n"
            f"📢 <b>Nomi:</b> {safe_name}\n"
            f"🆔 <b>ID:</b> <code>{safe_id}</code>\n"
            f"🔗 <b>Havola:</b> {safe_link}"
        )
        bot.send_message(message.chat.id, success_text, parse_mode="HTML")

    except Exception as e:
        traceback.print_exc()
        safe_error = html.escape(str(e)[:150])
        error_msg = (
            f"❌ <b>Kanalni qo'shib bo'lmadi!</b>\n\n"
            f"<b>Sababi:</b> {safe_error}\n\n"
            f"💡 <b>Muammoni oson hal qilish chorasi:</b>\n"
            f"Kanalingizdan bitta xabarni (postni) shu botga <b>forward (yo'naltirib)</b> yuboring! Bot kanalni avtomatik qo'shib oladi.\n\n"
            f"⚠️ <b>Quyidagilarni tekshiring:</b>\n"
            f"1. Bot ushbu kanalda <b>administrator</b> (admin) qilinganmi?\n"
            f"2. Kanalning username yoki ID-si to'g'rimi?"
        )
        bot.send_message(message.chat.id, error_msg, parse_mode="HTML")

def process_del_channel(message):
    try:
        channel_id = message.text.strip()
        remove_channel(channel_id)
        bot.send_message(message.chat.id, f"✅ Kanal o'chirildi! (ID: {channel_id})")
    except Exception as e:
        bot.send_message(message.chat.id, "❌ Xatolik yuz berdi.")

# --- RENDER WEB SERVER (PING UCHUN DOIMIY UYG'OQ TUTISH) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running 24/7!")

    def log_message(self, format, *args):
        return  # Konsolni ortiqcha ping xabarlari bilan to'ldirmaymiz

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- MAIN ---
if __name__ == "__main__":
    # Render-da port scan timeout bo'lmasligi uchun alohida thread-da kichik web-server ishga tushadi
    threading.Thread(target=run_health_server, daemon=True).start()
    print("Web server portda ishga tushdi...")
    print("Bot muvaffaqiyatli ishga tushdi...")
    bot.infinity_polling()