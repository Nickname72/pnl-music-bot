import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Flask, request
import telebot
from telebot import types


# ==========================================================
# НАЛАШТУВАННЯ
# ==========================================================

TOKEN = os.getenv("8984472073:AAEQGwL0C2N4GfMEQaNnfiC2KZxi1-xPlGA", "").strip()

# ID головного адміністратора
ADMIN_ID = 8423060500

# Пароль адмін-панелі
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# Блокування за мат
BAD_WORD_BAN_MINUTES = 5

# Мінімальний час між заявками
REQUEST_COOLDOWN_MINUTES = 2

# База даних
DB_NAME = "bot.db"


if not TOKEN:
    raise RuntimeError(
        "Не знайдено BOT_TOKEN. Додай BOT_TOKEN у Environment Variables Render."
    )

if not ADMIN_PASSWORD:
    raise RuntimeError(
        "Не знайдено ADMIN_PASSWORD. Додай ADMIN_PASSWORD у Environment Variables Render."
    )


bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
app = Flask(__name__)

db_lock = threading.RLock()


# ==========================================================
# ТИМЧАСОВІ ДАНІ
# ==========================================================

# user_id -> час закінчення блокування
temp_blocks = {}

# user_id -> час останньої заявки
last_requests = {}

# хто зараз увійшов в адмінку
admin_logged_in = set()

# поточна дія адміна
admin_action = {}


# ==========================================================
# БАЗА ДАНИХ
# ==========================================================

def get_db():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                song TEXT,
                date TEXT,
                time TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS manual_blocks (
                user_id INTEGER PRIMARY KEY,
                blocked_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS co_admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT
            )
        """)

        conn.commit()
        conn.close()


def register_user(user):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                registered_at
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            now
        ))

        conn.commit()
        conn.close()


def add_request(user, song):
    now = datetime.now()

    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT INTO requests (
                user_id,
                username,
                first_name,
                song,
                date,
                time
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            song,
            now.strftime("%d.%m.%Y"),
            now.strftime("%H:%M:%S")
        ))

        conn.commit()
        conn.close()


# ==========================================================
# БЛОКУВАННЯ
# ==========================================================

def is_manually_blocked(user_id):
    with db_lock:
        conn = get_db()

        row = conn.execute(
            "SELECT 1 FROM manual_blocks WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        conn.close()

        return row is not None


def add_manual_block(user_id):
    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT OR IGNORE INTO manual_blocks (
                user_id,
                blocked_at
            )
            VALUES (?, ?)
        """, (
            user_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()


def remove_manual_block(user_id):
    with db_lock:
        conn = get_db()

        conn.execute(
            "DELETE FROM manual_blocks WHERE user_id = ?",
            (user_id,)
        )

        conn.commit()
        conn.close()


# ==========================================================
# СПІВ-АДМІНИ
# ==========================================================

def is_co_admin(user_id):

    if user_id == ADMIN_ID:
        return True

    with db_lock:
        conn = get_db()

        row = conn.execute(
            "SELECT 1 FROM co_admins WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        conn.close()

        return row is not None


def add_co_admin(user_id):
    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT OR IGNORE INTO co_admins (
                user_id,
                added_at
            )
            VALUES (?, ?)
        """, (
            user_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()


def remove_co_admin(user_id):

    if user_id == ADMIN_ID:
        return

    with db_lock:
        conn = get_db()

        conn.execute(
            "DELETE FROM co_admins WHERE user_id = ?",
            (user_id,)
        )

        conn.commit()
        conn.close()


def get_co_admins():

    with db_lock:
        conn = get_db()

        rows = conn.execute("""
            SELECT user_id, added_at
            FROM co_admins
            ORDER BY added_at ASC
        """).fetchall()

        conn.close()

        return rows


# ==========================================================
# НЕЦЕНЗУРНА ЛЕКСИКА
# ==========================================================

BAD_WORDS = [
    "бля",
    "бляд",
    "блд",
    "сука",
    "сук",
    "хуй",
    "хуйн",
    "хуя",
    "хуе",
    "пизд",
    "пізд",
    "еб",
    "їб",
    "єб",
    "нахуй",
    "нах",
    "мудак",
    "мудил",
    "курва",
    "шлюх",
    "гандон",
    "долбо",
    "уеб",
    "уїб",
    "еблан",
    "єблан"
]


def normalize_text(text):
    text = (text or "").lower()

    return re.sub(
        r"[^а-яіїєґa-z0-9]",
        "",
        text
    )


def contains_bad_words(text):

    normalized = normalize_text(text)

    return any(
        word in normalized
        for word in BAD_WORDS
    )


# ==========================================================
# АДМІН-МЕНЮ
# ==========================================================

def admin_menu():

    keyboard = types.InlineKeyboardMarkup(row_width=1)

    keyboard.add(
        types.InlineKeyboardButton(
            "🎵 Перегляд заявок",
            callback_data="admin_requests"
        ),

        types.InlineKeyboardButton(
            "🚫 Заблокувати користувача",
            callback_data="admin_block"
        ),

        types.InlineKeyboardButton(
            "📊 Статистика",
            callback_data="admin_stats"
        ),

        types.InlineKeyboardButton(
            "📢 Надіслати повідомлення всім",
            callback_data="admin_broadcast"
        ),

        types.InlineKeyboardButton(
            "⚙️ Налаштування",
            callback_data="admin_settings"
        ),

        types.InlineKeyboardButton(
            "🔒 Вийти",
            callback_data="admin_logout"
        )
    )

    return keyboard


def settings_menu():

    keyboard = types.InlineKeyboardMarkup(row_width=1)

    keyboard.add(
        types.InlineKeyboardButton(
            "👑 Додати спів-адміна",
            callback_data="coadmin_add"
        ),

        types.InlineKeyboardButton(
            "🗑️ Видалити спів-адміна",
            callback_data="coadmin_remove"
        ),

        types.InlineKeyboardButton(
            "👥 Список спів-адмінів",
            callback_data="coadmin_list"
        ),

        types.InlineKeyboardButton(
            "⬅️ Назад",
            callback_data="admin_back"
        )
    )

    return keyboard


def is_admin_logged(user_id):

    return (
        user_id in admin_logged_in
        and is_co_admin(user_id)
    )


# ==========================================================
# /START
# ==========================================================

@bot.message_handler(commands=["start"])
def start_handler(message):

    register_user(message.from_user)

    text = """
🎵 <b>Вітаю в PNL MUSIC BOT!</b>

Надсилай назву пісні або посилання на неї — ми додамо її до списку заявок.

⚠️ <b>Правила:</b>

• 🇷🇺 Російськомовні пісні не приймаються
• 🤬 Пісні з нецензурною лексикою не приймаються
• 🎧 Phonk не приймається
• 🤘 Metal / важка музика не приймається
• 🚫 За нецензурну лексику — блокування на 5 хвилин
• ⏱️ Між заявками має пройти 2 хвилини

📩 <b>Надішли назву треку або посилання:</b>
"""

    bot.send_message(
        message.chat.id,
        text
    )


# ==========================================================
# /ADMIN
# ==========================================================

@bot.message_handler(commands=["admin"])
def admin_command(message):

    user_id = message.from_user.id

    register_user(message.from_user)

    if not is_co_admin(user_id):

        bot.send_message(
            message.chat.id,
            "⛔ У тебе немає доступу до адмін-панелі."
        )

        return

    admin_action[user_id] = "password"

    bot.send_message(
        message.chat.id,
        "🔐 Введи пароль адміністратора:"
    )


# ==========================================================
# CALLBACK-КНОПКИ
# ==========================================================

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):

    user_id = call.from_user.id

    if not is_co_admin(user_id):

        bot.answer_callback_query(
            call.id,
            "⛔ Немає доступу",
            show_alert=True
        )

        return

    data = call.data

    # ------------------------------------------------------
    # ЗАЯВКИ
    # ------------------------------------------------------

    if data == "admin_requests":

        bot.answer_callback_query(call.id)

        with db_lock:
            conn = get_db()

            rows = conn.execute("""
                SELECT
                    id,
                    first_name,
                    username,
                    song,
                    date,
                    time
                FROM requests
                ORDER BY id DESC
                LIMIT 20
            """).fetchall()

            conn.close()

        if not rows:

            bot.send_message(
                call.message.chat.id,
                "🎵 Заявок поки немає."
            )

            return

        text = "🎵 <b>ОСТАННІ 20 ЗАЯВОК</b>\n\n"

        for row in rows:

            username = (
                f"@{row['username']}"
                if row["username"]
                else "без username"
            )

            text += (
                f"<b>#{row['id']}</b> — "
                f"{row['song']}\n"

                f"👤 {row['first_name'] or 'Без імені'} "
                f"({username})\n"

                f"🕒 {row['date']} "
                f"{row['time']}\n\n"
            )

        bot.send_message(
            call.message.chat.id,
            text
        )

        return

    # ------------------------------------------------------
    # БЛОКУВАННЯ
    # ------------------------------------------------------

    if data == "admin_block":

        bot.answer_callback_query(call.id)

        admin_action[user_id] = "block"

        bot.send_message(
            call.message.chat.id,
            "🚫 Надішли Telegram ID користувача:"
        )

        return

    # ------------------------------------------------------
    # СТАТИСТИКА
    # ------------------------------------------------------

    if data == "admin_stats":

        bot.answer_callback_query(call.id)

        today = datetime.now().strftime("%d.%m.%Y")

        with db_lock:

            conn = get_db()

            users = conn.execute(
                "SELECT COUNT(*) c FROM users"
            ).fetchone()["c"]

            all_requests = conn.execute(
                "SELECT COUNT(*) c FROM requests"
            ).fetchone()["c"]

            today_requests = conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE date = ?",
                (today,)
            ).fetchone()["c"]

            manual_blocks = conn.execute(
                "SELECT COUNT(*) c FROM manual_blocks"
            ).fetchone()["c"]

            coadmins = conn.execute(
                "SELECT COUNT(*) c FROM co_admins"
            ).fetchone()["c"]

            conn.close()

        active_temp_blocks = sum(
            1
            for until in temp_blocks.values()
            if until > datetime.now()
        )

        text = (
            "📊 <b>СТАТИСТИКА</b>\n\n"

            f"👥 Користувачів: "
            f"<b>{users}</b>\n"

            f"🎵 Усього заявок: "
            f"<b>{all_requests}</b>\n"

            f"📅 Заявок сьогодні: "
            f"<b>{today_requests}</b>\n"

            f"🚫 Постійно заблоковано: "
            f"<b>{manual_blocks}</b>\n"

            f"⏳ Тимчасово заблоковано: "
            f"<b>{active_temp_blocks}</b>\n"

            f"👑 Спів-адмінів: "
            f"<b>{coadmins}</b>"
        )

        bot.send_message(
            call.message.chat.id,
            text
        )

        return

    # ------------------------------------------------------
    # РОЗСИЛКА
    # ------------------------------------------------------

    if data == "admin_broadcast":

        bot.answer_callback_query(call.id)

        admin_action[user_id] = "broadcast"

        bot.send_message(
            call.message.chat.id,
            "📢 Надішли текст для розсилки:"
        )

        return

    # ------------------------------------------------------
    # НАЛАШТУВАННЯ
    # ------------------------------------------------------

    if data == "admin_settings":

        bot.answer_callback_query(call.id)

        if user_id != ADMIN_ID:

            bot.send_message(
                call.message.chat.id,
                "⛔ Налаштування доступні тільки головному адміну."
            )

            return

        bot.send_message(
            call.message.chat.id,
            "⚙️ <b>Налаштування</b>",
            reply_markup=settings_menu()
        )

        return

    # ------------------------------------------------------
    # НАЗАД
    # ------------------------------------------------------

    if data == "admin_back":

        bot.answer_callback_query(call.id)

        bot.send_message(
            call.message.chat.id,
            "🔐 <b>Адмін-панель</b>",
            reply_markup=admin_menu()
        )

        return

    # ------------------------------------------------------
    # ВИХІД
    # ------------------------------------------------------

    if data == "admin_logout":

        admin_logged_in.discard(user_id)
        admin_action.pop(user_id, None)

        bot.answer_callback_query(
            call.id,
            "Вихід виконано"
        )

        bot.send_message(
            call.message.chat.id,
            "🔒 Ти вийшов з адмін-панелі."
        )

        return

    # ------------------------------------------------------
    # РОЗБЛОКУВАННЯ
    # ------------------------------------------------------

    if data.startswith("unblock_yes_"):

        target_id = int(
            data.split("_")[-1]
        )

        remove_manual_block(target_id)

        bot.answer_callback_query(
            call.id,
            "Розблоковано"
        )

        bot.send_message(
            call.message.chat.id,
            f"✅ Користувача "
            f"<code>{target_id}</code> розблоковано."
        )

        return

    if data.startswith("unblock_no_"):

        bot.answer_callback_query(call.id)

        bot.send_message(
            call.message.chat.id,
            "🚫 Користувач залишається заблокованим."
        )

        return

    # ------------------------------------------------------
    # ДОДАТИ СПІВ-АДМІНА
    # ------------------------------------------------------

    if data == "coadmin_add":

        bot.answer_callback_query(call.id)

        if user_id != ADMIN_ID:

            bot.send_message(
                call.message.chat.id,
                "⛔ Тільки головний адмін."
            )

            return

        admin_action[user_id] = "coadmin_add"

        bot.send_message(
            call.message.chat.id,
            "👑 Надішли Telegram ID нового спів-адміна:"
        )

        return

    # ------------------------------------------------------
    # ВИДАЛИТИ СПІВ-АДМІНА
    # ------------------------------------------------------

    if data == "coadmin_remove":

        bot.answer_callback_query(call.id)

        if user_id != ADMIN_ID:

            bot.send_message(
                call.message.chat.id,
                "⛔ Тільки головний адмін."
            )

            return

        admin_action[user_id] = "coadmin_remove"

        bot.send_message(
            call.message.chat.id,
            "🗑️ Надішли Telegram ID спів-адміна:"
        )

        return

    # ------------------------------------------------------
    # СПИСОК СПІВ-АДМІНІВ
    # ------------------------------------------------------

    if data == "coadmin_list":

        bot.answer_callback_query(call.id)

        rows = get_co_admins()

        if not rows:

            bot.send_message(
                call.message.chat.id,
                "👥 Спів-адмінів поки немає."
            )

            return

        text = "👥 <b>СПІВ-АДМІНИ</b>\n\n"

        for i, row in enumerate(rows, 1):

            text += (
                f"{i}. <code>{row['user_id']}</code>\n"
                f"   Додано: {row['added_at']}\n\n"
            )

        bot.send_message(
            call.message.chat.id,
            text
        )


# ==========================================================
# ОБРОБКА ТЕКСТУ
# ==========================================================

@bot.message_handler(content_types=["text"])
def text_handler(message):

    user = message.from_user
    user_id = user.id
    text = message.text.strip()

    register_user(user)

    # ======================================================
    # АДМІН
    # ======================================================

    if is_co_admin(user_id) and user_id in admin_action:

        action = admin_action[user_id]

        # --------------------------------------------------
        # ПАРОЛЬ
        # --------------------------------------------------

        if action == "password":

            if text == ADMIN_PASSWORD:

                admin_logged_in.add(user_id)
                admin_action.pop(user_id, None)

                bot.send_message(
                    message.chat.id,
                    "✅ <b>Пароль правильний!</b>\n\n"
                    "🔐 Адмін-панель:",
                    reply_markup=admin_menu()
                )

            else:

                bot.send_message(
                    message.chat.id,
                    "❌ Неправильний пароль."
                )

            return

        # --------------------------------------------------
        # ПЕРЕВІРКА СЕСІЇ
        # --------------------------------------------------

        if not is_admin_logged(user_id):

            admin_action.pop(user_id, None)

            bot.send_message(
                message.chat.id,
                "🔒 Сесія закінчилась. Напиши /admin."
            )

            return

        # --------------------------------------------------
        # БЛОКУВАННЯ
        # --------------------------------------------------

        if action == "block":

            if not text.isdigit():

                bot.send_message(
                    message.chat.id,
                    "❌ ID повинен складатися тільки з цифр."
                )

                return

            target_id = int(text)

            if target_id == ADMIN_ID:

                bot.send_message(
                    message.chat.id,
                    "⛔ Головного адміна блокувати не можна."
                )

                admin_action.pop(user_id, None)

                return

            if is_co_admin(target_id):

                bot.send_message(
                    message.chat.id,
                    "⛔ Спів-адміна блокувати не можна."
                )

                admin_action.pop(user_id, None)

                return

            if is_manually_blocked(target_id):

                keyboard = types.InlineKeyboardMarkup(
                    row_width=2
                )

                keyboard.add(
                    types.InlineKeyboardButton(
                        "✅ Так",
                        callback_data=f"unblock_yes_{target_id}"
                    ),

                    types.InlineKeyboardButton(
                        "❌ Ні",
                        callback_data=f"unblock_no_{target_id}"
                    )
                )

                bot.send_message(
                    message.chat.id,
                    "🚫 Користувач уже заблокований.\n"
                    "Хочете його розблокувати?",
                    reply_markup=keyboard
                )

            else:

                add_manual_block(target_id)

                bot.send_message(
                    message.chat.id,
                    f"🚫 Користувача "
                    f"<code>{target_id}</code> заблоковано."
                )

            admin_action.pop(user_id, None)

            return

        # --------------------------------------------------
        # РОЗСИЛКА
        # --------------------------------------------------

        if action == "broadcast":

            admin_action.pop(user_id, None)

            with db_lock:

                conn = get_db()

                users = conn.execute(
                    "SELECT user_id FROM users"
                ).fetchall()

                conn.close()

            success = 0
            failed = 0

            for row in users:

                try:

                    bot.send_message(
                        row["user_id"],
                        text
                    )

                    success += 1

                except Exception:

                    failed += 1

            bot.send_message(
                message.chat.id,

                "📢 <b>Розсилку завершено!</b>\n\n"

                f"✅ Успішно: {success}\n"
                f"❌ Не доставлено: {failed}"
            )

            return

        # --------------------------------------------------
        # ДОДАТИ СПІВ-АДМІНА
        # --------------------------------------------------

        if action == "coadmin_add":

            if user_id != ADMIN_ID:

                bot.send_message(
                    message.chat.id,
                    "⛔ Тільки головний адмін."
                )

                admin_action.pop(user_id, None)

                return

            if not text.isdigit():

                bot.send_message(
                    message.chat.id,
                    "❌ ID повинен бути числом."
                )

                return

            target_id = int(text)

            if target_id == ADMIN_ID:

                bot.send_message(
                    message.chat.id,
                    "ℹ️ Це вже головний адмін."
                )

            else:

                add_co_admin(target_id)

                bot.send_message(
                    message.chat.id,
                    f"👑 <code>{target_id}</code> "
                    "доданий як спів-адмін."
                )

            admin_action.pop(user_id, None)

            return

        # --------------------------------------------------
        # ВИДАЛИТИ СПІВ-АДМІНА
        # --------------------------------------------------

        if action == "coadmin_remove":

            if user_id != ADMIN_ID:

                bot.send_message(
                    message.chat.id,
                    "⛔ Тільки головний адмін."
                )

                admin_action.pop(user_id, None)

                return

            if not text.isdigit():

                bot.send_message(
                    message.chat.id,
                    "❌ ID повинен бути числом."
                )

                return

            target_id = int(text)

            if target_id == ADMIN_ID:

                bot.send_message(
                    message.chat.id,
                    "⛔ Головного адміна видалити не можна."
                )

            else:

                remove_co_admin(target_id)

                bot.send_message(
                    message.chat.id,
                    f"🗑️ <code>{target_id}</code> "
                    "видалений зі спів-адмінів."
                )

            admin_action.pop(user_id, None)

            return

    # ======================================================
    # РУЧНЕ БЛОКУВАННЯ
    # ======================================================

    if user_id != ADMIN_ID:

        if is_manually_blocked(user_id):

            bot.send_message(
                message.chat.id,
                "🚫 Ти заблокований адміністратором."
            )

            return

    # ======================================================
    # ТИМЧАСОВЕ БЛОКУВАННЯ
    # ======================================================

    now = datetime.now()

    temp_until = temp_blocks.get(user_id)

    if temp_until:

        if now < temp_until:

            remaining = int(
                (temp_until - now).total_seconds() / 60
            ) + 1

            bot.send_message(
                message.chat.id,

                "🚫 <b>Ти тимчасово заблокований.</b>\n"
                f"Залишилось приблизно {remaining} хв."
            )

            return

        else:

            temp_blocks.pop(
                user_id,
                None
            )

    # ======================================================
    # МАТ
    # ======================================================

    if contains_bad_words(text):

        try:

            bot.delete_message(
                message.chat.id,
                message.message_id
            )

        except Exception:

            pass

        temp_blocks[user_id] = (
            now +
            timedelta(
                minutes=BAD_WORD_BAN_MINUTES
            )
        )

        bot.send_message(
            message.chat.id,

            "⚠️ <b>Перестань використовувати "
            "нецензурну лексику! 🚫</b>\n\n"

            "Тобі заборонено писати протягом "
            "5 хвилин."
        )

        return

    # ======================================================
    # ЛІМІТ 2 ХВ
    # ======================================================

    last_time = last_requests.get(user_id)

    if last_time:

        cooldown = timedelta(
            minutes=REQUEST_COOLDOWN_MINUTES
        )

        elapsed = now - last_time

        if elapsed < cooldown:

            remaining = cooldown - elapsed

            minutes = int(
                remaining.total_seconds() // 60
            )

            seconds = int(
                remaining.total_seconds() % 60
            )

            bot.send_message(
                message.chat.id,

                "⏱️ <b>Ще рано надсилати "
                "наступну заявку.</b>\n\n"

                f"Зачекай {minutes} хв "
                f"{seconds} сек."
            )

            return

    # ======================================================
    # ЗБЕРІГАЄМО ЗАЯВКУ
    # ======================================================

    last_requests[user_id] = now

    add_request(
        user,
        text
    )

    username = (
        f"@{user.username}"
        if user.username
        else "немає"
    )

    admin_text = (
        "🎵 <b>НОВА ЗАЯВКА</b>\n\n"

        f"🎶 <b>Пісня:</b> {text}\n\n"

        f"👤 <b>Ім'я:</b> "
        f"{user.first_name or 'Без імені'}\n"

        f"🔗 <b>Username:</b> "
        f"{username}\n"

        f"🆔 <b>ID:</b> "
        f"<code>{user.id}</code>\n"

        f"📅 <b>Дата:</b> "
        f"{now.strftime('%d.%m.%Y')}\n"

        f"🕒 <b>Час:</b> "
        f"{now.strftime('%H:%M:%S')}"
    )

    try:

        bot.send_message(
            ADMIN_ID,
            admin_text
        )

    except Exception as e:

        print(
            "Помилка відправки адміну:",
            repr(e)
        )

    bot.send_message(
        message.chat.id,

        "✅ <b>Заявку прийнято!</b>\n\n"
        "🎵 Пісня передана адміністратору."
    )


# ==========================================================
# WEBHOOK
# ==========================================================

@app.get("/")
def home():

    return "PNL MUSIC BOT is running."


@app.get("/health")
def health():

    return {
        "status": "ok"
    }


@app.post("/telegram")
def telegram_webhook():

    try:

        json_string = (
            request
            .get_data()
            .decode("utf-8")
        )

        update = types.Update.de_json(
            json_string
        )

        bot.process_new_updates(
            [update]
        )

        return "OK", 200

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return "ERROR", 500


# ==========================================================
# ВСТАНОВЛЕННЯ WEBHOOK
# ==========================================================

def configure_webhook():

    webhook_url = os.getenv(
        "WEBHOOK_URL",
        ""
    ).strip().rstrip("/")

    if not webhook_url:

        print(
            "WEBHOOK_URL не встановлено."
        )

        return

    full_url = (
        webhook_url +
        "/telegram"
    )

    try:

        bot.remove_webhook()

        bot.set_webhook(
            url=full_url
        )

        print(
            "Webhook встановлено:",
            full_url
        )

    except Exception as e:

        print(
            "Помилка webhook:",
            repr(e)
        )


# ==========================================================
# ЗАПУСК
# ==========================================================

init_db()
configure_webhook()


if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
