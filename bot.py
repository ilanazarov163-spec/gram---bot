import os
import time
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Set, Optional, Tuple

import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

DB_PATH = os.getenv("DB_PATH", "gram.db").strip() or "gram.db"

BONUS_AMOUNT = 2500
BONUS_COOLDOWN_SEC = 24 * 60 * 60
GO_DELAY_SEC = 10
MAX_CHOICES = 16

# ====== Helpers ======

def now() -> int:
    return int(time.time())

def mention(user) -> str:
    name = (user.first_name or "User").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

def fmt_int(n: int) -> str:
    # 2500000 -> "2 500 000"
    s = str(int(n))
    parts = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    return " ".join(reversed(parts))

def color_emoji(num: int) -> str:
    if num == 0:
        return "🟢"
    # roulette red numbers (European)
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    return "🔴" if num in red else "⚫"

def is_private(message) -> bool:
    return message.chat.type == "private"

def is_group(message) -> bool:
    return message.chat.type in ("group", "supergroup")

# ====== DB ======

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init_and_migrate():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        last_bonus_ts INTEGER NOT NULL DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        covered TEXT NOT NULL,
        created_ts INTEGER NOT NULL,
        original TEXT NOT NULL,
        PRIMARY KEY (user_id, chat_id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        num INTEGER NOT NULL,
        ts INTEGER NOT NULL
    );
    """)

    conn.commit()
    conn.close()

def get_user(user_id: int) -> sqlite3.Row:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row

def set_balance(user_id: int, bal: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (int(bal), user_id))
    conn.commit()
    conn.close()

def add_balance(user_id: int, delta: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (int(delta), user_id))
    conn.commit()
    conn.close()

def get_balance(user_id: int) -> int:
    row = get_user(user_id)
    return int(row["balance"])

def get_last_bonus(user_id: int) -> int:
    row = get_user(user_id)
    return int(row["last_bonus_ts"])

def set_last_bonus(user_id: int, ts: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_bonus_ts=? WHERE user_id=?", (int(ts), user_id))
    conn.commit()
    conn.close()

def upsert_bet(user_id: int, chat_id: int, amount: int, covered: Set[int], created_ts: int, original: str):
    covered_str = ",".join(str(x) for x in sorted(covered))
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO bets(user_id, chat_id, amount, covered, created_ts, original)
    VALUES(?,?,?,?,?,?)
    ON CONFLICT(user_id, chat_id) DO UPDATE SET
        amount=excluded.amount,
        covered=excluded.covered,
        created_ts=excluded.created_ts,
        original=excluded.original
    """, (user_id, chat_id, int(amount), covered_str, int(created_ts), original))
    conn.commit()
    conn.close()

def get_bet(user_id: int, chat_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    row = cur.fetchone()
    conn.close()
    return row

def delete_bet(user_id: int, chat_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bets WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()
    conn.close()

def add_result(chat_id: int, num: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO results(chat_id, num, ts) VALUES(?,?,?)", (chat_id, int(num), now()))
    conn.commit()
    conn.close()

def get_last_results(chat_id: int, limit: int = 10) -> List[int]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT num FROM results WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, int(limit)))
    rows = cur.fetchall()
    conn.close()
    return [int(r["num"]) for r in rows]

# ====== Betting parse ======

def parse_bet_text(text: str) -> Optional[Tuple[int, Set[int], str]]:
    """
    Formats:
      2500 0
      2500 0 1 4 9 2 ... (up to 16 choices)
      2600 0-9 9-11
    """
    t = text.strip().lower()
    t = re.sub(r"\s+", " ", t)

    parts = t.split(" ")
    if len(parts) < 2:
        return None

    if not parts[0].isdigit():
        return None

    amount = int(parts[0])
    if amount <= 0:
        return None

    choices = parts[1:]
    if len(choices) > MAX_CHOICES:
        choices = choices[:MAX_CHOICES]

    covered: Set[int] = set()

    for c in choices:
        c = c.strip()
        if not c:
            continue

        # range a-b
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", c)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            if a < 0 or b < 0 or a > 36 or b > 36:
                return None
            if a > b:
                a, b = b, a
            for x in range(a, b + 1):
                covered.add(x)
            continue

        # single number
        if c.isdigit():
            n = int(c)
            if n < 0 or n > 36:
                return None
            covered.add(n)
            continue

        # (цвета / к / ч) — если захочешь включить, скажешь. Сейчас по твоим условиям не обяз.
        return None

    if not covered:
        return None

    return amount, covered, t

def calc_payout(amount: int, covered: Set[int], spun: int) -> int:
    """Return total payout (not profit). If lose -> 0. Uses 36/coverage_size."""
    if spun not in covered:
        return 0
    k = len(covered)
    if k <= 0:
        return 0
    # multiplier = 36 / k (e.g. single number -> 36x, 0-9 (10 nums) -> 3.6x)
    # Keep integer payout
    return int(amount * (36.0 / k))

# ====== UI (private) ======

def kb_private_main(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("👥 Профиль", callback_data="p_profile"),
        types.InlineKeyboardButton("🛒 Донат", callback_data="p_donate"),
    )
    kb.add(types.InlineKeyboardButton("❓ Помощь", callback_data="p_help"))

    # Bonus only if available
    lastb = get_last_bonus(user_id)
    if now() - lastb >= BONUS_COOLDOWN_SEC:
        kb.add(types.InlineKeyboardButton("🎁 Бонус", callback_data="p_bonus"))
    return kb

def text_profile(user_id: int) -> str:
    bal = get_balance(user_id)
    return f"🆔 ID: <b>{user_id}</b>\n💰 Баланс: <b>{fmt_int(bal)}</b> GRAM"

def text_help() -> str:
    return (
        "Команды в чате:\n"
        "• <b>Б</b> — баланс\n"
        "• <b>ставка</b>: <code>2500 0</code> или <code>2600 0-9 9-11</code> или <code>2500 0 1 4 9</code> (до 16)\n"
        "• <b>го</b> — запуск игры (ставка крутится через 10 сек)\n"
        "• <b>лог</b> — последние числа\n\n"
        "В боте (ЛС): Профиль / Донат / Бонус"
    )

def text_donate() -> str:
    # Только текст как у тебя, без лишнего.
    return (
        "Выберите пакет:\n\n"
        "50 ⭐ - 100 000\n"
        "100 ⭐ - 204 000 (+2%)\n"
        "250 ⭐ - 525 000 (+5%)\n"
        "500 ⭐ - 1 150 000 (+10%)\n"
        "1000 ⭐ - 2 300 000 (+15%)\n"
        "2500 ⭐ - 6 250 000 (+25%)\n"
        "100 ⭐ - VIP\n\n"
        "Если возникли проблемы с пополнением — пишите @youcoid"
    )

# ====== Group logic ======

def send_balance_group(message):
    bal = get_balance(message.from_user.id)
    txt = f"{mention(message.from_user)}\n💰 Баланс: <b>{fmt_int(bal)}</b> GRAM"
    bot.send_message(message.chat.id, txt, reply_to_message_id=message.message_id)

def send_no_bets(message):
    bot.send_message(message.chat.id, "Невозможно начать игру без ставок.", reply_to_message_id=message.message_id)

def build_result_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Повторить", callback_data="bet_repeat"),
        types.InlineKeyboardButton("Удвоить", callback_data="bet_double"),
    )
    return kb

def schedule_spin(chat_id: int, user_id: int, reply_to_mid: int):
    def _run():
        # check bet exists
        bet = get_bet(user_id, chat_id)
        if not bet:
            return

        # spin
        spun = int(time.time()) % 37  # simple RNG replacement; you can change to random.SystemRandom later
        # better RNG:
        try:
            import secrets
            spun = secrets.randbelow(37)
        except Exception:
            pass

        add_result(chat_id, spun)

        amount = int(bet["amount"])
        covered = set(int(x) for x in bet["covered"].split(",") if x.strip().isdigit())
        original = bet["original"]

        payout = calc_payout(amount, covered, spun)

        # close bet
        delete_bet(user_id, chat_id)

        # if win, add profit = payout (we already removed stake when placing)
        if payout > 0:
            add_balance(user_id, payout)

        # Compose message (NO "Проигрыш", NO "икс")
        head = f"Рулетка: <b>{spun}</b> {color_emoji(spun)}"
        line = f"{mention(types.User.de_json({'id': user_id, 'first_name': 'User'}, bot))}"  # fallback
        # get real name via cached? we don't have user object here reliably; use stored mention by resolving via reply (ok)
        # We'll mention by id only (still clickable)
        line = f'<a href="tg://user?id={user_id}">Игрок</a>'

        # Try to mention by id with no name issues; Telegram will show name in preview
        bet_line = f"• <b>{fmt_int(amount)}</b> GRAM на {original.split(' ', 1)[1]}"
        msg = head + "\n" + line + "\n" + bet_line

        if payout > 0:
            msg += f"\n\n✅ Выигрыш: <b>{fmt_int(payout)}</b> GRAM"

        bot.send_message(chat_id, msg, reply_to_message_id=reply_to_mid, reply_markup=build_result_kb())

    threading.Timer(GO_DELAY_SEC, _run).start()

def place_bet(message, amount: int, covered: Set[int], original: str):
    uid = message.from_user.id
    cid = message.chat.id
    bal = get_balance(uid)
    if bal < amount:
        bot.send_message(cid, "Недостаточно GRAM.", reply_to_message_id=message.message_id)
        return

    # deduct immediately
    add_balance(uid, -amount)
    upsert_bet(uid, cid, amount, covered, now(), original)

    # confirm
    bet_target = original.split(" ", 1)[1]
    txt = f"Ставка принята: <b>{fmt_int(amount)}</b> GRAM на <b>{bet_target}</b>"
    bot.send_message(cid, txt, reply_to_message_id=message.message_id)

# ====== Handlers ======

@bot.message_handler(commands=["start"])
def on_start(message):
    if is_private(message):
        bot.send_message(
            message.chat.id,
            "👋 Добро пожаловать!",
            reply_markup=kb_private_main(message.from_user.id)
        )
    else:
        # in groups just show balance shortcut
        send_balance_group(message)

@bot.message_handler(func=lambda m: is_group(m) and (m.text or "").strip().lower() in ["б", "b"])
def on_balance_group(m):
    send_balance_group(m)

@bot.message_handler(func=lambda m: is_group(m) and (m.text or "").strip().lower() == "лог")
def on_log_group(m):
    nums = get_last_results(m.chat.id, 10)
    if not nums:
        bot.send_message(m.chat.id, "Лог пуст.", reply_to_message_id=m.message_id)
        return
    # show only numbers with colors
    lines = [f"{n} {color_emoji(n)}" for n in reversed(nums)]
    bot.send_message(m.chat.id, "\n".join(lines), reply_to_message_id=m.message_id)

@bot.message_handler(func=lambda m: is_group(m) and (m.text or "").strip().lower() == "го")
def on_go_group(m):
    bet = get_bet(m.from_user.id, m.chat.id)
    if not bet:
        send_no_bets(m)
        return
    # schedule spin and block further "го" after bet finishes (bet is removed in schedule)
    schedule_spin(m.chat.id, m.from_user.id, m.message_id)

@bot.message_handler(func=lambda m: is_group(m) and m.text is not None)
def on_bet_text(m):
    t = (m.text or "").strip()
    # ignore short commands we already handled
    low = t.lower()
    if low in ["б", "b", "го", "лог"] or low.startswith("/"):
        return

    parsed = parse_bet_text(t)
    if not parsed:
        return

    amount, covered, original = parsed
    place_bet(m, amount, covered, original)

# ====== Admin commands ======

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

@bot.message_handler(commands=["give"])
def cmd_give(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Формат: /give n")
        return
    n = int(parts[1])
    add_balance(m.from_user.id, n)
    bot.reply_to(m, f"Ок. Баланс: {fmt_int(get_balance(m.from_user.id))} GRAM")

@bot.message_handler(commands=["giveid"])
def cmd_giveid(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 3:
        bot.reply_to(m, "Формат: /giveid user_id n")
        return
    if not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(m, "Формат: /giveid user_id n")
        return
    uid = int(parts[1])
    n = int(parts[2])
    add_balance(uid, n)
    bot.reply_to(m, f"Ок. Игрок {uid}: {fmt_int(get_balance(uid))} GRAM")

@bot.message_handler(commands=["resetid"])
def cmd_resetid(m):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Формат: /resetid user_id")
        return
    uid = int(parts[1])
    set_balance(uid, 0)
    bot.reply_to(m, f"Ок. Игрок {uid}: 0 GRAM")

# ====== Private callbacks ======

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("p_"))
def cb_private(c):
    uid = c.from_user.id
    data = c.data

    if data == "p_profile":
        bot.answer_callback_query(c.id)
        bot.edit_message_text(text_profile(uid), c.message.chat.id, c.message.message_id, reply_markup=kb_private_main(uid))
        return

    if data == "p_donate":
        bot.answer_callback_query(c.id)
        bot.edit_message_text(text_donate(), c.message.chat.id, c.message.message_id, reply_markup=kb_private_main(uid))
        return

    if data == "p_help":
        bot.answer_callback_query(c.id)
        bot.edit_message_text(text_help(), c.message.chat.id, c.message.message_id, reply_markup=kb_private_main(uid))
        return

    if data == "p_bonus":
        lastb = get_last_bonus(uid)
        if now() - lastb < BONUS_COOLDOWN_SEC:
            bot.answer_callback_query(c.id, "Бонус ещё недоступен.", show_alert=False)
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb_private_main(uid))
            return
        set_last_bonus(uid, now())
        add_balance(uid, BONUS_AMOUNT)
        bot.answer_callback_query(c.id, f"+{BONUS_AMOUNT} GRAM")
        bot.edit_message_text(text_profile(uid), c.message.chat.id, c.message.message_id, reply_markup=kb_private_main(uid))
        return

# ====== Repeat/Double buttons ======

@bot.callback_query_handler(func=lambda c: c.data in ["bet_repeat", "bet_double"])
def cb_repeat_double(c):
    if not is_group(c.message) and c.message.chat.type not in ("group", "supergroup"):
        bot.answer_callback_query(c.id)
        return

    uid = c.from_user.id
    cid = c.message.chat.id

    # Can't create a new bet if an active bet exists
    if get_bet(uid, cid):
        bot.answer_callback_query(c.id, "У вас уже есть ставка.", show_alert=False)
        return

    # Extract last bet line from message (we stored original as text after "на ")
    text = c.message.text or ""
    m = re.search(r"•\s*([\d\s]+)\s*GRAM на (.+)$", text, flags=re.MULTILINE)
    if not m:
        bot.answer_callback_query(c.id, "Не могу повторить.", show_alert=False)
        return

    amount_str = m.group(1).replace(" ", "")
    tail = m.group(2).strip().lower()

    try:
        amount = int(amount_str)
    except Exception:
        bot.answer_callback_query(c.id, "Не могу повторить.", show_alert=False)
        return

    if c.data == "bet_double":
        amount *= 2

    # Reconstruct bet text: "amount " + tail
    bet_text = f"{amount} {tail}"
    parsed = parse_bet_text(bet_text)
    if not parsed:
        bot.answer_callback_query(c.id, "Ставка невалидна.", show_alert=False)
        return

    amount2, covered, original = parsed
    bal = get_balance(uid)
    if bal < amount2:
        bot.answer_callback_query(c.id, "Недостаточно GRAM.", show_alert=False)
        return

    add_balance(uid, -amount2)
    upsert_bet(uid, cid, amount2, covered, now(), original)

    bot.answer_callback_query(c.id)
    # start spin automatically after 10 sec (как удобно по кнопкам)
    schedule_spin(cid, uid, c.message.message_id)

# ====== Run ======

if __name__ == "__main__":
    db_init_and_migrate()
    print("Bot started.")
    bot.infinity_polling(skip_pending=True)

    
