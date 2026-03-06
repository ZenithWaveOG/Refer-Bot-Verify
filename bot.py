import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
import requests
import json
import os
import time
import zipfile
import shutil
import threading
import itertools
import random
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# ===================== ⚙️ CONFIGURATION =====================
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN')
SUPER_ADMIN_ID = int(os.environ.get('SUPER_ADMIN_ID', 8537079657))
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://user:pass@localhost/shein_bot')

# Proxy list – comma separated, e.g., "http://ip:port,http://user:pass@ip:port"
# Leave empty to use server IP directly (NOT recommended)
PROXY_LIST = os.environ.get('PROXY_LIST', '').split(',') if os.environ.get('PROXY_LIST') else []

# Engine settings – be gentle to avoid IP bans
MAX_WORKERS = 5                # Concurrent requests per user
CYCLE_WAIT_TIME = 300          # Seconds between cycles (5 minutes)
REQUEST_DELAY = 1.0            # Base delay between requests (jitter added)
GAALI_MODE = False

# Maximum number of users running the checker simultaneously
MAX_CONCURRENT_USERS = 20

PRICES = {"SVI": 150, "SVC": 150, "SVD": 150, "SVH": 150, "OTHER": 0}

BASE_DIR = "shein_smart_data"
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN)

# ===================== 🗄️ DATABASE CONNECTION POOL =====================
class Database:
    def __init__(self, dsn):
        self.pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=dsn)

    def get_conn(self):
        return self.pool.getconn()

    def put_conn(self, conn):
        self.pool.putconn(conn)

    def execute(self, query, params=None, fetch=False, fetchone=False, commit=False):
        conn = self.get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                if commit:
                    conn.commit()
                if fetch:
                    return cur.fetchall()
                if fetchone:
                    return cur.fetchone()
        finally:
            self.put_conn(conn)

db_pool = Database(DATABASE_URL)

# Initialize tables
init_sql = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    status TEXT DEFAULT 'pending',
    joined_at TIMESTAMP DEFAULT NOW(),
    approved_at TIMESTAMP,
    declined_at TIMESTAMP,
    is_running BOOLEAN DEFAULT FALSE,
    running_thread_id INTEGER
);

CREATE TABLE IF NOT EXISTS codes (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    category TEXT,
    status TEXT DEFAULT 'unused',
    added_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cookies (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    file_name TEXT,
    cookie_string TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_codes_user ON codes(user_id);
CREATE INDEX IF NOT EXISTS idx_cookies_user ON cookies(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_code_per_user ON codes(user_id, code);
"""
db_pool.execute(init_sql, commit=True)

# ===================== 🛠️ HELPERS =====================
GAALIS = ["Bsdk", "Chutiye", "Lodu", "Gandu", "Madarchod", "Bhenchod", "Saale"]
def g(text):
    return f"{random.choice(GAALIS)}, {text}" if GAALI_MODE else text

def clean_code(text):
    match = re.search(r"(SV[ICDH][A-Z0-9]+)", text)
    return match.group(1) if match else None

def classify_code(code):
    if code.startswith("SVI"): return "SVI"
    if code.startswith("SVC"): return "SVC"
    if code.startswith("SVD"): return "SVD"
    if code.startswith("SVH"): return "SVH"
    return "OTHER"

def parse_cookie_from_file_content(content):
    """Parse cookie from JSON or Netscape format."""
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return "; ".join([f"{x['name']}={x['value']}" for x in data])
        if isinstance(data, dict):
            return "; ".join([f"{k}={v}" for k, v in data.items()])
    except:
        return content.strip()
    return None

def get_random_proxy():
    if not PROXY_LIST:
        return None
    return random.choice(PROXY_LIST).strip()

# ===================== 👤 USER MANAGEMENT =====================
def get_user_by_telegram_id(telegram_id):
    return db_pool.execute(
        "SELECT * FROM users WHERE telegram_id = %s",
        (telegram_id,), fetchone=True
    )

def create_user(telegram_id, username, first_name):
    db_pool.execute(
        "INSERT INTO users (telegram_id, username, first_name, status) VALUES (%s, %s, %s, 'pending')",
        (telegram_id, username, first_name), commit=True
    )
    return get_user_by_telegram_id(telegram_id)

def update_user_status(user_id, status):
    now = datetime.now()
    if status == 'approved':
        db_pool.execute(
            "UPDATE users SET status = 'approved', approved_at = %s WHERE id = %s",
            (now, user_id), commit=True
        )
    elif status == 'declined':
        db_pool.execute(
            "UPDATE users SET status = 'declined', declined_at = %s WHERE id = %s",
            (now, user_id), commit=True
        )

def get_all_admins():
    return [SUPER_ADMIN_ID]

def notify_admins(text, markup=None):
    for admin_id in get_all_admins():
        try:
            bot.send_message(admin_id, text, reply_markup=markup, parse_mode="Markdown")
        except:
            pass

def count_running_users():
    result = db_pool.execute("SELECT COUNT(*) FROM users WHERE is_running = TRUE", fetchone=True)
    return result['count'] if result else 0

# ===================== 📊 USER STATS =====================
def get_user_stats(user_id):
    codes = db_pool.execute(
        "SELECT category, COUNT(*) FROM codes WHERE user_id = %s GROUP BY category",
        (user_id,), fetch=True
    )
    unused_codes = db_pool.execute(
        "SELECT COUNT(*) FROM codes WHERE user_id = %s AND status = 'unused'",
        (user_id,), fetchone=True
    )['count']
    cookies_count = db_pool.execute(
        "SELECT COUNT(*) FROM cookies WHERE user_id = %s",
        (user_id,), fetchone=True
    )['count']
    stats = {cat: 0 for cat in PRICES}
    for row in codes:
        stats[row['category']] = row['count']
    total_value = sum(stats[cat] * PRICES.get(cat, 0) for cat in stats)
    return stats, unused_codes, cookies_count, total_value

# ===================== 🧠 ENGINE PER USER =====================
running_engines = {}
engine_lock = threading.Lock()

def user_engine_loop(user_id, chat_id):
    thread_id = threading.get_ident()
    with engine_lock:
        running_engines[user_id] = thread_id
        db_pool.execute("UPDATE users SET is_running = TRUE, running_thread_id = %s WHERE id = %s",
                        (thread_id, user_id), commit=True)

    cycle = 1
    try:
        while True:
            with engine_lock:
                if running_engines.get(user_id) != thread_id:
                    break

            cookies_rows = db_pool.execute(
                "SELECT id, file_name, cookie_string FROM cookies WHERE user_id = %s",
                (user_id,), fetch=True
            )
            if not cookies_rows:
                bot.send_message(chat_id, "⚠️ No cookies uploaded. Engine stopped.")
                break

            cookies = {f"cookie_{row['id']}": row['cookie_string'] for row in cookies_rows}

            codes_rows = db_pool.execute(
                "SELECT id, code, category FROM codes WHERE user_id = %s AND status = 'unused'",
                (user_id,), fetch=True
            )
            if not codes_rows:
                bot.send_message(chat_id, "⚠️ No unused codes left. Engine stopped.")
                break

            active_codes = [(row['code'], row['category']) for row in codes_rows]

            stats, unused_codes, cookies_count, total_value = get_user_stats(user_id)
            bot.send_message(
                chat_id,
                f"🔄 **Cycle {cycle}**\n🍪 Cookies: {len(cookies)}\n🛡️ Unused Codes: {len(active_codes)}\n💰 Value: ₹{total_value}",
                parse_mode="Markdown"
            )

            reports = {'valid': [], 'invalid': [], 'redeemed': [], 'logs': []}
            cookie_pool = itertools.cycle(list(cookies.keys()))

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
                for code, cat in active_codes:
                    with engine_lock:
                        if running_engines.get(user_id) != thread_id:
                            break
                    use_file = next(cookie_pool)
                    exe.submit(check_task, user_id, code, cookies[use_file], use_file, reports)

            for code in reports['valid']:
                db_pool.execute(
                    "UPDATE codes SET status = 'valid' WHERE user_id = %s AND code = %s",
                    (user_id, code), commit=True
                )
            for code in reports['redeemed']:
                db_pool.execute(
                    "UPDATE codes SET status = 'redeemed' WHERE user_id = %s AND code = %s",
                    (user_id, code), commit=True
                )
            for code in reports['invalid']:
                db_pool.execute(
                    "UPDATE codes SET status = 'invalid' WHERE user_id = %s AND code = %s",
                    (user_id, code), commit=True
                )

            files = []
            if reports['valid']:
                path = os.path.join(LOGS_DIR, f"valid_{user_id}_cycle{cycle}.txt")
                with open(path, "w") as f:
                    f.write("\n".join(reports['valid']))
                files.append((path, f"✅ **Cycle {cycle} Hits** ({len(reports['valid'])})"))

            if reports['logs']:
                path = os.path.join(LOGS_DIR, f"logs_{user_id}_cycle{cycle}.txt")
                with open(path, "w") as f:
                    f.write("\n".join(reports['logs']))
                files.append((path, "📜 **Logs**"))

            for p, cap in files:
                with open(p, "rb") as f:
                    bot.send_document(chat_id, f, caption=cap)
                os.remove(p)

            bot.send_message(chat_id, f"💤 Sleep {CYCLE_WAIT_TIME}s...")
            time.sleep(CYCLE_WAIT_TIME)
            cycle += 1
    finally:
        with engine_lock:
            if running_engines.get(user_id) == thread_id:
                del running_engines[user_id]
        db_pool.execute(
            "UPDATE users SET is_running = FALSE, running_thread_id = NULL WHERE id = %s",
            (user_id,), commit=True
        )
        bot.send_message(chat_id, "🛑 Engine stopped.")

def check_task(user_id, code, cookie, cookie_name, report_dict):
    url = "https://www.sheinindia.in/api/cart/apply-voucher"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-tenant-id": "SHEIN",
        "cookie": cookie
    }
    payload = {"voucherId": code, "device": {"client_type": "web"}}

    # Random delay with jitter
    delay = REQUEST_DELAY + random.uniform(0, 0.3)
    time.sleep(delay)

    # Proxy selection
    proxy = get_random_proxy()
    proxies = {'http': proxy, 'https': proxy} if proxy else None

    try:
        r = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=10)
        res = r.json()
    except Exception as e:
        res = {"errorMessage": f"Request failed: {str(e)}"}

    status = "UNKNOWN"
    if "errorMessage" not in res:
        status = "VALID"
        report_dict['valid'].append(code)
        print(f"\033[92m[HIT] {code} | {cookie_name} | proxy: {proxy}\033[0m")
    else:
        err = str(res.get("errorMessage", "")).lower()
        if "redeemed" in err or "limit" in err:
            status = "REDEEMED"
            report_dict['redeemed'].append(code)
            print(f"\033[93m[USED] {code} | proxy: {proxy}\033[0m")
        else:
            status = "INVALID"
            report_dict['invalid'].append(code)
            print(f"\033[91m[BAD] {code} | proxy: {proxy}\033[0m")

    report_dict['logs'].append(
        f"[{datetime.now().strftime('%H:%M:%S')}] {code} | {status} | {cookie_name} | proxy: {proxy}"
    )

# ===================== 📱 USER INTERFACE =====================
def get_user_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("▶️ Start Engine"),
        KeyboardButton("⏹️ Stop Engine"),
        KeyboardButton("➕ Add Codes"),
        KeyboardButton("🍪 Upload Cookies"),
        KeyboardButton("📦 Withdraw Codes"),
        KeyboardButton("📊 My Stats")
    )
    return markup

def get_withdraw_category_keyboard(user_id):
    stats, unused, _, _ = get_user_stats(user_id)
    markup = InlineKeyboardMarkup(row_width=2)
    for cat in ["SVI", "SVC", "SVD", "SVH"]:
        unused_cat = db_pool.execute(
            "SELECT COUNT(*) FROM codes WHERE user_id=%s AND category=%s AND status='unused'",
            (user_id, cat), fetchone=True
        )['count']
        markup.add(InlineKeyboardButton(
            f"{cat} ({unused_cat} unused)",
            callback_data=f"wdcat_{cat}"
        ))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    return markup

def get_withdraw_qty_keyboard(cat, user_id):
    unused_count = db_pool.execute(
        "SELECT COUNT(*) FROM codes WHERE user_id=%s AND category=%s AND status='unused'",
        (user_id, cat), fetchone=True
    )['count']
    markup = InlineKeyboardMarkup(row_width=3)
    for q in [1, 5, 10, 20, 50, 100]:
        if q <= unused_count:
            markup.add(InlineKeyboardButton(str(q), callback_data=f"wdqty_{cat}_{q}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="withdraw"))
    return markup

# ===================== 🤖 BOT HANDLERS =====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""

    user = get_user_by_telegram_id(user_id)
    if not user:
        user = create_user(user_id, username, first_name)
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"app_{user['id']}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"dec_{user['id']}")
        )
        notify_admins(
            f"🆕 New user request:\n"
            f"ID: `{user_id}`\n"
            f"Username: @{username}\n"
            f"Name: {first_name}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            markup=kb
        )
        bot.reply_to(message, "⏳ Your request has been sent to admin. Please wait for approval.")
    else:
        if user['status'] == 'approved':
            bot.reply_to(
                message,
                "✅ You are approved! Use the buttons below.",
                reply_markup=get_user_main_keyboard()
            )
        elif user['status'] == 'pending':
            bot.reply_to(message, "⏳ Your request is still pending. Please wait.")
        elif user['status'] == 'declined':
            bot.reply_to(message, "❌ Your request was declined. Contact admin if you think this is an error.")

@bot.message_handler(commands=['admin'])
def cmd_admin(message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    users = db_pool.execute("SELECT status, COUNT(*) FROM users GROUP BY status", fetch=True)
    total = sum(u['count'] for u in users)
    approved = next((u['count'] for u in users if u['status'] == 'approved'), 0)
    pending = next((u['count'] for u in users if u['status'] == 'pending'), 0)
    declined = next((u['count'] for u in users if u['status'] == 'declined'), 0)
    running = count_running_users()

    text = f"👥 **Users**\nTotal: {total}\n✅ Approved: {approved}\n⏳ Pending: {pending}\n❌ Declined: {declined}\n🟢 Running: {running}/{MAX_CONCURRENT_USERS}\n\n"
    pending_users = db_pool.execute(
        "SELECT id, telegram_id, username, first_name FROM users WHERE status = 'pending'",
        fetch=True
    )
    if pending_users:
        text += "**Pending approvals:**\n"
        markup = InlineKeyboardMarkup()
        for u in pending_users:
            btn_text = f"{u['first_name']} (@{u['username']})"
            markup.add(InlineKeyboardButton(btn_text, callback_data=f"admin_review_{u['id']}"))
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data

    if data.startswith('app_') or data.startswith('dec_'):
        if user_id != SUPER_ADMIN_ID:
            bot.answer_callback_query(call.id, "Unauthorized")
            return
        parts = data.split('_')
        action = parts[0]
        target_user_id = int(parts[1])

        user = db_pool.execute("SELECT * FROM users WHERE id = %s", (target_user_id,), fetchone=True)
        if not user:
            bot.answer_callback_query(call.id, "User not found")
            return

        if action == 'app':
            update_user_status(target_user_id, 'approved')
            bot.answer_callback_query(call.id, "User approved")
            try:
                bot.send_message(
                    user['telegram_id'],
                    "✅ Your request has been approved! You can now use the bot.\nUse /start to begin.",
                    reply_markup=get_user_main_keyboard()
                )
            except:
                pass
        else:
            update_user_status(target_user_id, 'declined')
            bot.answer_callback_query(call.id, "User declined")
            try:
                bot.send_message(user['telegram_id'], "❌ Your request was declined.")
            except:
                pass

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    elif data.startswith('admin_review_'):
        if user_id != SUPER_ADMIN_ID:
            return
        target_user_id = int(data.split('_')[2])
        user = db_pool.execute("SELECT * FROM users WHERE id = %s", (target_user_id,), fetchone=True)
        if not user:
            bot.answer_callback_query(call.id, "User not found")
            return
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"app_{target_user_id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"dec_{target_user_id}")
        )
        bot.send_message(
            user_id,
            f"Reviewing user {user['first_name']} (@{user['username']})",
            reply_markup=kb
        )

    user_obj = get_user_by_telegram_id(user_id)
    if not user_obj or user_obj['status'] != 'approved':
        bot.answer_callback_query(call.id, "You are not approved")
        return

    if data == "back_main":
        bot.edit_message_text(
            "Main menu:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
        bot.send_message(call.message.chat.id, "Use buttons below:", reply_markup=get_user_main_keyboard())

    elif data == "withdraw":
        bot.edit_message_text(
            "Select category to withdraw:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_withdraw_category_keyboard(user_obj['id'])
        )

    elif data.startswith("wdcat_"):
        cat = data.split("_")[1]
        bot.edit_message_text(
            f"Withdraw {cat} – select quantity:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_withdraw_qty_keyboard(cat, user_obj['id'])
        )

    elif data.startswith("wdqty_"):
        parts = data.split("_")
        cat = parts[1]
        qty = int(parts[2])
        codes = db_pool.execute(
            "SELECT code FROM codes WHERE user_id = %s AND category = %s AND status = 'unused' LIMIT %s",
            (user_obj['id'], cat, qty), fetch=True
        )
        if len(codes) < qty:
            bot.answer_callback_query(call.id, f"Not enough {cat} unused codes!", show_alert=True)
            return

        code_list = [c['code'] for c in codes]
        db_pool.execute(
            "DELETE FROM codes WHERE user_id = %s AND code = ANY(%s)",
            (user_obj['id'], code_list), commit=True
        )
        text = "\n".join(code_list)
        if len(text) > 3000:
            path = os.path.join(LOGS_DIR, f"withdraw_{user_obj['id']}_{cat}_{qty}.txt")
            with open(path, "w") as f:
                f.write(text)
            with open(path, "rb") as f:
                bot.send_document(call.message.chat.id, f, caption=f"✅ {qty} {cat} codes")
            os.remove(path)
        else:
            bot.send_message(call.message.chat.id, f"✅ {qty} {cat} codes:\n`{text}`", parse_mode="Markdown")

        bot.answer_callback_query(call.id, "Withdrawn")
        bot.edit_message_text(
            "Withdrawn. Choose another category:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_withdraw_category_keyboard(user_obj['id'])
        )

@bot.message_handler(func=lambda m: True, content_types=['text', 'document'])
def handle_user_messages(message):
    user_id = message.from_user.id
    user = get_user_by_telegram_id(user_id)
    if not user or user['status'] != 'approved':
        return

    text = message.text

    if text == "▶️ Start Engine":
        running = count_running_users()
        if running >= MAX_CONCURRENT_USERS:
            bot.reply_to(message, f"⚠️ Maximum concurrent users ({MAX_CONCURRENT_USERS}) reached. Please wait for someone to stop.")
            return
        with engine_lock:
            if user['is_running']:
                bot.reply_to(message, "Engine already running.")
                return
        thread = threading.Thread(target=user_engine_loop, args=(user['id'], message.chat.id), daemon=True)
        thread.start()
        bot.reply_to(message, "🟢 Engine starting...")

    elif text == "⏹️ Stop Engine":
        with engine_lock:
            if user['is_running']:
                running_engines.pop(user['id'], None)
                db_pool.execute("UPDATE users SET is_running = FALSE WHERE id = %s", (user['id'],), commit=True)
                bot.reply_to(message, "🔴 Engine stopping...")
            else:
                bot.reply_to(message, "Engine not running.")

    elif text == "➕ Add Codes":
        bot.reply_to(message, "Send me a `.txt` file containing codes or paste them directly. I'll extract SVI/SVC/etc automatically.")

    elif text == "🍪 Upload Cookies":
        bot.reply_to(message, "Send me `cookies.zip` (containing JSON cookie files) or a `.txt` file with cookies.")

    elif text == "📦 Withdraw Codes":
        bot.send_message(
            message.chat.id,
            "Select category:",
            reply_markup=get_withdraw_category_keyboard(user['id'])
        )

    elif text == "📊 My Stats":
        stats, unused, cookies_count, total_value = get_user_stats(user['id'])
        msg = f"**Your Stats**\n"
        for cat, cnt in stats.items():
            unused_cat = db_pool.execute(
                "SELECT COUNT(*) FROM codes WHERE user_id=%s AND category=%s AND status='unused'",
                (user['id'], cat), fetchone=True
            )['count']
            msg += f"{cat}: {cnt} total ({unused_cat} unused) – ₹{cnt * PRICES.get(cat, 0)}\n"
        msg += f"🍪 Cookies: {cookies_count}\n💰 Total Value: ₹{total_value}"
        bot.reply_to(message, msg, parse_mode="Markdown")

    if message.document:
        file_name = message.document.file_name
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)

        if file_name.endswith('.zip'):
            zip_path = os.path.join(LOGS_DIR, f"temp_{user_id}.zip")
            with open(zip_path, 'wb') as f:
                f.write(downloaded)
            extract_dir = os.path.join(LOGS_DIR, f"cookies_{user_id}")
            shutil.rmtree(extract_dir, ignore_errors=True)
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(extract_dir)
            os.remove(zip_path)

            count = 0
            for root, _, files in os.walk(extract_dir):
                for f in files:
                    if f.endswith('.json'):
                        with open(os.path.join(root, f), 'r') as cf:
                            content = cf.read()
                        cookie_str = parse_cookie_from_file_content(content)
                        if cookie_str:
                            db_pool.execute(
                                "INSERT INTO cookies (user_id, file_name, cookie_string) VALUES (%s, %s, %s)",
                                (user['id'], f, cookie_str), commit=True
                            )
                            count += 1
            shutil.rmtree(extract_dir, ignore_errors=True)
            bot.reply_to(message, g(f"✅ {count} cookies added."))

        elif file_name.endswith('.txt'):
            content = downloaded.decode('utf-8', errors='ignore')
            if message.text and message.text == "🍪 Upload Cookies":
                cookie_str = parse_cookie_from_file_content(content)
                if cookie_str:
                    db_pool.execute(
                        "INSERT INTO cookies (user_id, file_name, cookie_string) VALUES (%s, %s, %s)",
                        (user['id'], file_name, cookie_str), commit=True
                    )
                    bot.reply_to(message, g("✅ Cookie saved."))
                else:
                    bot.reply_to(message, "❌ Could not parse cookie.")
            else:
                lines = content.splitlines()
                added = 0
                for line in lines:
                    clean = clean_code(line.strip())
                    if clean:
                        cat = classify_code(clean)
                        try:
                            db_pool.execute(
                                "INSERT INTO codes (user_id, code, category) VALUES (%s, %s, %s)",
                                (user['id'], clean, cat), commit=True
                            )
                            added += 1
                        except psycopg2.errors.UniqueViolation:
                            pass
                bot.reply_to(message, g(f"✅ {added} new codes added."))

    elif message.text and message.text not in ["▶️ Start Engine", "⏹️ Stop Engine", "➕ Add Codes", "🍪 Upload Cookies", "📦 Withdraw Codes", "📊 My Stats"]:
        lines = message.text.splitlines()
        added = 0
        for line in lines:
            clean = clean_code(line.strip())
            if clean:
                cat = classify_code(clean)
                try:
                    db_pool.execute(
                        "INSERT INTO codes (user_id, code, category) VALUES (%s, %s, %s)",
                        (user['id'], clean, cat), commit=True
                    )
                    added += 1
                except psycopg2.errors.UniqueViolation:
                    pass
        bot.reply_to(message, g(f"✅ {added} new codes added."))

# ===================== 🚀 START =====================
if __name__ == "__main__":
    print("🤖 Bot started with multi-user approval system + proxy rotation.")
    bot.polling(non_stop=True)
