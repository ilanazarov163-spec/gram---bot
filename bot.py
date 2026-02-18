# bot.py
# Python 3.11+ | pyTelegramBotAPI
import os
import re
import time
import random
import sqlite3
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import telebot
from telebot import types

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing or invalid. Set it in environment variables.")

DB_PATH = os.getenv("DB_PATH", "gram.db")

BONUS_AMOUNT = 2500
BONUS_COOLDOWN_SEC = 24 * 60 * 60

PLAY_COOLDOWN_SEC = 10  # после установки ставки нужно подождать 10 сек, потом можно "го"
MAX_LOG = 10            # сколько чисел показывать в логе

# Европейская рулетка (0-36)
RED_NUMS = {
    1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36
}

# ===================== BOT =====================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ===================== DB =====================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

def _col_exists(table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cur.fetchall())

def db_init_and_migrate():
    # users
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        last_bonus_ts INTEGER NOT NULL DEFAULT 0
    )
    """)
    # bets: одна активная ставка на (chat_id, user_id)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS bets(
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL DEFAULT 0,
        bet_type TEXT NOT NULL DEFAULT '',
        bet_value TEXT NOT NULL DEFAULT '',
        placed_ts INTEGER NOT NULL DEFAULT 0,
        last_play_ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat_id, user_id)
    )
    """)
    # logs per chat
    conn.execute("""
    CREATE TABLE IF NOT EXISTS chat_logs(
        chat_id INTEGER PRIMARY KEY,
        log TEXT NOT NULL DEFAULT ''
    )
    """)
    conn.commit()

def get_user(user_id: int):
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def set_balance(user_id: int, balance: int):
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    conn.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, user_id))
    conn.commit()

def add_balance(user_id: int, delta: int):
    u = get_user(user_id)
    new_bal = int(u["balance"]) + int(delta)
    if new_bal < 0:
        new_bal = 0
    set_balance(user_id, new_bal)

def get_bet(chat_id: int, user_id: int):
    return conn.execute(
        "SELECT * FROM bets WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()

def set_bet(chat_id: int, user_id: int, amount: int, bet_type: str, bet_value: str):
    now = int(time.time())
    conn.execute("""
    INSERT INTO bets(chat_id, user_id, amount, bet_type, bet_value, placed_ts, last_play_ts)
    VALUES(?,?,?,?,?,?,0)
    ON CONFLICT(chat_id, user_id) DO UPDATE SET
        amount=excluded.amount,
        bet_type=excluded.bet_type,
        bet_value=excluded.bet_value,
        placed_ts=excluded.placed_ts
    """, (chat_id, user_id, amount, bet_type, bet_value, now))
    conn.commit()

def set_last_play(chat_id: int, user_id: int, ts: int):
    conn.execute("UPDATE bets SET last_play_ts=? WHERE chat_id=? AND user_id=?",
                 (ts, chat_id, user_id))
    conn.commit()

def update_chat_log(chat_id: int, new_num: int, new_color: str):
    row = conn.execute("SELECT log FROM chat_logs WHERE chat_id=?", (chat_id,)).fetchone()
    items = []
    if row and row["log"]:
        # формат: "15b,8b,36r,..."
        items = row["log"].split(",")
    tag = f"{new_num}{new_color}"
    items = [tag] + [x for x in items if x]  # newest first
    items = items[:MAX_LOG]
    log_str = ",".join(items)
    conn.execute("""
    INSERT INTO chat_logs(chat_id, log) VALUES(?,?)
    ON CONFLICT(chat_id) DO UPDATE SET log=excluded.log
    """, (chat_id, log_str))
    conn.commit()

def get_chat_log(chat_id: int):
    row = conn.execute("SELECT log FROM chat_logs WHERE chat_id=?", (chat_id,)).fetchone()
    if not row or not row["log"]:
        return []
    return [x for x in row["log"].split(",") if x]

# ===================== HELPERS =====================
def fmt_int(n: int) -> str:
    # 1000000 -> 1 000 000
    return f"{int(n):,}".replace(",", " ")

def user_title(u: types.User) -> str:
    # как на скринах: отображаем имя пользователя
    if u.username:
        return u.username
    # fallback
    return (u.first_name or "User").strip()

def bet_title(bet_type: str, bet_value: str) -> str:
    if bet_type == "number":
        return bet_value
    if bet_type == "color":
        if bet_value == "red":
            return "красное"
        if bet_value == "black":
            return "черное"
        if bet_value == "green":
            return "зелёное"
    if bet_type == "range":
        return bet_value
    return bet_value

def num_color(n: int) -> str:
    if n == 0:
        return "g"
    return "r" if n in RED_NUMS else "b"

def color_emoji(c: str) -> str:
    return {"r": "🔴", "b": "⚫️", "g": "🟢"}.get(c, "⚫️")

def parse_bet_message(text: str) -> Optional[Tuple[int, str, str]]:
    """
    Форматы:
      2500 0            -> number
      2500 к / 2500 красное -> color red
      2500 ч / 2500 черное  -> color black
      2500 0-5          -> range
    """
    t = text.strip().lower()
    # amount first
    m = re.match(r"^(\d{1,9})\s+(.+)$", t)
    if not m:
        return None
    amount = int(m.group(1))
    target = m.group(2).strip()

    if amount <= 0:
        return None

    # color
    if target in ("к", "красное", "red", "r"):
        return amount, "color", "red"
    if target in ("ч", "черное", "чёрное", "black", "b"):
        return amount, "color", "black"
    if target in ("з", "зелёное", "зеленое", "green", "g", "0green"):
        return amount, "color", "green"

    # range a-b
    rm = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})$", target)
    if rm:
        a = int(rm.group(1))
        b = int(rm.group(2))
        if 0 <= a <= 36 and 0 <= b <= 36 and a <= b:
            return amount, "range", f"{a}-{b}"
        return None

    # number 0-36
    if re.fullmatch(r"\d{1,2}", target):
        n = int(target)
        if 0 <= n <= 36:
            return amount, "number", str(n)

    return None

def payout_multiplier(bet_type: str, bet_value: str, rolled: int, rolled_color: str) -> float:
    """
    Возвращает X (множитель). Если 0 -> проигрыш.
    - number: 36x
    - color red/black: 2x (0 не подходит)
    - green: только на 0, 36x
    - range: X = 36 / count(range)
    """
    if bet_type == "number":
        return 36.0 if str(rolled) == bet_value else 0.0

    if bet_type == "color":
        if bet_value == "green":
            return 36.0 if rolled == 0 else 0.0
        if rolled == 0:
            return 0.0
        if bet_value == "red" and rolled_color == "r":
            return 2.0
        if bet_value == "black" and rolled_color == "b":
            return 2.0
        return 0.0

    if bet_type == "range":
        a, b = map(int, bet_value.split("-"))
        if a <= rolled <= b:
            count = (b - a + 1)
            # "икс получается на разделение" -> 36 / count
            return 36.0 / float(count)
        return 0.0

    return 0.0

def profile_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    now = int(time.time())
    u = get_user(user_id)
    last_bonus = int(u["last_bonus_ts"])
    can_bonus = (now - last_bonus) >= BONUS_COOLDOWN_SEC

    # Кнопки меню как ты просил
    kb.add(
        types.InlineKeyboardButton("👥 Профиль", callback_data="menu_profile"),
        types.InlineKeyboardButton("🛒 Донат", callback_data="menu_donate"),
        types.InlineKeyboardButton("❓ Помощь", callback_data="menu_help"),
    )
    # бонус показываем ТОЛЬКО если доступен
    if can_bonus:
        kb.add(types.InlineKeyboardButton("🎁 Бонус", callback_data="bonus_claim"))
    return kb

def bet_action_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Повторить", callback_data="bet_repeat"),
        types.InlineKeyboardButton("Удвоить", callback_data="bet_double"),
    )
    return kb

# ===================== MENU TEXTS =====================
DONATE_USER = "@youcoid"

DONATE_PACKS = [
    ("50 ⭐ - 100 000",  "don_50"),
    ("100 ⭐ - 204 000 (+2%)", "don_100"),
    ("250 ⭐ - 525 000 (+5%)", "don_250"),
    ("500 ⭐ - 1 150 000 (+10%)", "don_500"),
    ("1000 ⭐ - 2 300 000 (+15%)", "don_1000"),
    ("2500 ⭐ - 6 250 000 (+25%)", "don_2500"),
    ("100 ⭐ - VIP", "don_vip"),
]

def donate_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for label, cb in DONATE_PACKS:
        kb.add(types.InlineKeyboardButton(label, callback_data=cb))
    return kb

HELP_TEXT = (
    "Команды:\n"
    "• <b>б</b> или <b>баланс</b> — показать баланс\n"
    "• <b>сумма ставка</b> — поставить ставку (пример: <b>2500 0</b>, <b>2500 к</b>, <b>2500 ч</b>, <b>2500 0-5</b>)\n"
    "• <b>го</b> — крутить рулетку по вашей активной ставке\n\n"
    "Ставки:\n"
    "• На число: 36x\n"
    "• На цвет (к/ч): 2x\n"
    "• На диапазон a-b: X = 36 / (b-a+1)\n"
)

# ===================== HANDLERS =====================
@bot.message_handler(commands=["start"])
def cmd_start(m: types.Message):
    u = m.from_user
    get_user(u.id)

    text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "GRAM — игровой бот для вашего чата.\n"
        "Нажми кнопки ниже 👇"
    )
    bot.send_message(m.chat.id, text, reply_markup=profile_keyboard(u.id))

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() in ("б", "баланс"))
def msg_balance(m: types.Message):
    u = m.from_user
    row = get_user(u.id)
    name = user_title(u)
    bal = int(row["balance"])

    text = f"<b>{name}</b>\n💰 Баланс: <b>{fmt_int(bal)}</b> GRAM"
    bot.send_message(m.chat.id, text, reply_markup=profile_keyboard(u.id))

@bot.callback_query_handler(func=lambda c: c.data in ("menu_profile","menu_donate","menu_help"))
def cb_menu(c: types.CallbackQuery):
    u = c.from_user
    if c.data == "menu_profile":
        row = get_user(u.id)
        name = user_title(u)
        bal = int(row["balance"])
        text = f"<b>{name}</b>\n💰 Баланс: <b>{fmt_int(bal)}</b> GRAM"
        bot.edit_message_text(
            text, c.message.chat.id, c.message.message_id,
            reply_markup=profile_keyboard(u.id)
        )
    elif c.data == "menu_donate":
        text = f"Если возникли проблемы с пополнением — обратитесь к {DONATE_USER}\n\nВыберите пакет:"
        bot.edit_message_text(
            text, c.message.chat.id, c.message.message_id,
            reply_markup=donate_keyboard()
        )
    else:
        bot.edit_message_text(
            HELP_TEXT, c.message.chat.id, c.message.message_id,
            reply_markup=profile_keyboard(u.id)
        )
    bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("don_"))
def cb_donate(c: types.CallbackQuery):
    # Здесь настоящие Stars payments — отдельная интеграция.
    # Пока показываем выбранный пакет (как ты просил — без лишних сообщений)
    label = next((x[0] for x in DONATE_PACKS if x[1] == c.data), "Пакет")
    bot.answer_callback_query(c.id, "Выбрано.")
    bot.send_message(c.message.chat.id, f"Вы выбрали: <b>{label}</b>\nПисать: {DONATE_USER}")

@bot.callback_query_handler(func=lambda c: c.data == "bonus_claim")
def cb_bonus(c: types.CallbackQuery):
    u = c.from_user
    row = get_user(u.id)
    now = int(time.time())
    last_bonus = int(row["last_bonus_ts"])

    if (now - last_bonus) < BONUS_COOLDOWN_SEC:
        left = BONUS_COOLDOWN_SEC - (now - last_bonus)
        mins = left // 60
        bot.answer_callback_query(c.id, f"Рано. Осталось ~{mins} мин.")
        return

    add_balance(u.id, BONUS_AMOUNT)
    conn.execute("UPDATE users SET last_bonus_ts=? WHERE user_id=?", (now, u.id))
    conn.commit()

    bot.answer_callback_query(c.id, "Бонус выдан ✅")
    # обновим профиль
    row2 = get_user(u.id)
    name = user_title(u)
    bal = int(row2["balance"])
    text = f"<b>{name}</b>\n💰 Баланс: <b>{fmt_int(bal)}</b> GRAM"
    bot.edit_message_text(
        text, c.message.chat.id, c.message.message_id,
        reply_markup=profile_keyboard(u.id)
    )

@bot.message_handler(func=lambda m: m.text and parse_bet_message(m.text) is not None)
def msg_place_bet(m: types.Message):
    u = m.from_user
    chat_id = m.chat.id
    user_id = u.id

    parsed = parse_bet_message(m.text)
    if not parsed:
        return
    amount, btype, bval = parsed

    row = get_user(user_id)
    bal = int(row["balance"])
    if amount > bal:
        bot.reply_to(m, "Недостаточно средств.")
        return

    # списываем сумму ставки один раз
    add_balance(user_id, -amount)
    set_bet(chat_id, user_id, amount, btype, bval)

    text = f"Ставка принята: <b>{fmt_int(amount)}</b> GRAM на <b>{bet_title(btype,bval)}</b>"
    bot.reply_to(m, text)

@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "го")
def msg_go(m: types.Message):
    u = m.from_user
    chat_id = m.chat.id
    user_id = u.id

    bet = get_bet(chat_id, user_id)
    if not bet or int(bet["amount"]) <= 0:
        bot.reply_to(m, "Невозможно начать игру без ставок.")
        return

    now = int(time.time())
    placed_ts = int(bet["placed_ts"])
    last_play = int(bet["last_play_ts"])

    # правило: после установки ставки ждём 10 сек, потом можно крутить бесконечно
    if now - placed_ts < PLAY_COOLDOWN_SEC:
        left = PLAY_COOLDOWN_SEC - (now - placed_ts)
        bot.reply_to(m, f"Подожди {left} сек и снова пиши «го».")
        return

    # анти-спам: минимальная пауза 1 сек между "го"
    if last_play and (now - last_play) < 1:
        return

    handle_spin(chat_id, user_id, u, m)

def handle_spin(chat_id: int, user_id: int, u: types.User, m: types.Message):
    bet = get_bet(chat_id, user_id)
    if not bet:
        bot.reply_to(m, "Ставка не найдена.")
        return

    amount = int(bet["amount"])
    btype = bet["bet_type"]
    bval = bet["bet_value"]

    rolled = random.randint(0, 36)
    c = num_color(rolled)
    update_chat_log(chat_id, rolled, c)

    mult = payout_multiplier(btype, bval, rolled, c)
    win = 0
    if mult > 0:
        # выплата (как ты писал: 2500 * 36 = 90000)
        win = int(amount * mult)
        add_balance(user_id, win)

    # фиксируем последнее время крутки
    set_last_play(chat_id, user_id, int(time.time()))

    # Лог (как на скрине — список последних)
    log_items = get_chat_log(chat_id)
    # выводим красиво: число + кружок
    pretty_log = []
    for tag in log_items:
        # tag like "16r"
        mm = re.match(r"^(\d{1,2})([rbg])$", tag)
        if not mm:
            continue
        n = int(mm.group(1))
        cc = mm.group(2)
        pretty_log.append(f"{n} {color_emoji(cc)}")
    log_text = "\n".join(pretty_log)

    name = user_title(u)
    head = f"<b>{name}</b>\n"
    head += f"Рулетка: <b>{rolled}</b> {color_emoji(c)}\n"
    head += f"• <b>{fmt_int(amount)}</b> GRAM на <b>{bet_title(btype,bval)}</b>\n"

    if win > 0:
        head += f"\n✅ Выигрыш: <b>{fmt_int(win)}</b> GRAM (x{mult:.2f})"
    else:
        head += "\n❌ Проигрыш."

    # отправляем одно сообщение + кнопки повтор/удвоить
    bot.send_message(chat_id, (head + "\n\n" + log_text).strip(), reply_markup=bet_action_keyboard())

@bot.callback_query_handler(func=lambda c: c.data in ("bet_repeat","bet_double"))
def cb_bet_actions(c: types.CallbackQuery):
    u = c.from_user
    chat_id = c.message.chat.id
    user_id = u.id
    bet = get_bet(chat_id, user_id)

    if not bet or int(bet["amount"]) <= 0:
        bot.answer_callback_query(c.id, "Ставки нет.")
        return

    if c.data == "bet_repeat":
        bot.answer_callback_query(c.id, "Повторяю…")
        fake = types.Message.de_json(c.message.json)
        fake.from_user = u
        fake.chat = c.message.chat
        handle_spin(chat_id, user_id, u, fake)
        return

    # bet_double
    amount = int(bet["amount"])
    row = get_user(user_id)
    bal = int(row["balance"])

    if bal < amount:
        bot.answer_callback_query(c.id, "Не хватает на удвоение.")
        return

    add_balance(user_id, -amount)
    new_amount = amount * 2
    # обновим ставку, timestamp НЕ трогаем (чтоб не сбрасывать ожидание)
    conn.execute("""
        UPDATE bets SET amount=? WHERE chat_id=? AND user_id=?
    """, (new_amount, chat_id, user_id))
    conn.commit()

    bot.answer_callback_query(c.id, "Удвоено ✅")

# ===================== ADMIN =====================
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

@bot.message_handler(commands=["give"])
def cmd_give(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    # /give 1000
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Формат: /give N")
        return
    n = int(parts[1])
    add_balance(m.from_user.id, n)
    bot.reply_to(m, f"Выдал себе {fmt_int(n)} GRAM.")

@bot.message_handler(commands=["giveid"])
def cmd_giveid(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    # /giveid 123456789 1000
    parts = m.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(m, "Формат: /giveid ID N")
        return
    uid = int(parts[1])
    n = int(parts[2])
    add_balance(uid, n)
    bot.reply_to(m, f"Выдал игроку {uid} {fmt_int(n)} GRAM.")

@bot.message_handler(commands=["resetid"])
def cmd_resetid(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    # /resetid 123456789
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Формат: /resetid ID")
        return
    uid = int(parts[1])
    set_balance(uid, 0)
    bot.reply_to(m, f"Баланс игрока {uid} сброшен.")

# ===================== ENTRYPOINT FOR web.py =====================
def start_polling():
    db_init_and_migrate()
    logging.info("Bot polling started.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
