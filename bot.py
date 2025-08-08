import logging
import os
import sqlite3
import time
from contextlib import closing

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

# --- ENV ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")  # from @CryptoBot
CRYPTO_CREATE_URL = "https://pay.crypt.bot/api/createInvoice"

# Admins by username (without @)
ADMIN_USERNAMES = {"mkru27"}  # <-- добавлен как просили

# Mining economy
DEFAULT_RATE_USDT_PER_GH_PER_DAY = 0.01   # доход в USDT на каждый GH/s в день (пример)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("miningbot")

# --- DB ---
DB_PATH = "db.sqlite"
con = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = con.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0,
    hashrate REAL DEFAULT 0,
    ref_id INTEGER,
    is_admin INTEGER DEFAULT 0
)""")
cur.execute("""
CREATE TABLE IF NOT EXISTS accruals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    created_at INTEGER
)""")
cur.execute("""
CREATE TABLE IF NOT EXISTS settings(
    k TEXT PRIMARY KEY,
    v TEXT
)""")
# init default rate if not exists
cur.execute("INSERT OR IGNORE INTO settings(k,v) VALUES('rate_usdt_per_gh_per_day', ?)", (str(DEFAULT_RATE_USDT_PER_GH_PER_DAY),))
con.commit()

def db_get_rate() -> float:
    with closing(sqlite3.connect(DB_PATH)) as c:
        k = c.cursor()
        k.execute("SELECT v FROM settings WHERE k='rate_usdt_per_gh_per_day'")
        row = k.fetchone()
        return float(row[0]) if row else DEFAULT_RATE_USDT_PER_GH_PER_DAY

def db_set_rate(val: float):
    with closing(sqlite3.connect(DB_PATH)) as c:
        k = c.cursor()
        k.execute("INSERT INTO settings(k,v) VALUES('rate_usdt_per_gh_per_day', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(val),))
        c.commit()

def ensure_user(user_id: int, username: str | None, ref_id: int | None = None):
    cur.execute("SELECT id FROM users WHERE id=?", (user_id,))
    if not cur.fetchone():
        is_admin = 1 if (username or "").lower() in ADMIN_USERNAMES else 0
        cur.execute("INSERT INTO users(id, username, ref_id, is_admin) VALUES(?,?,?,?)", (user_id, username or "", ref_id, is_admin))
        con.commit()
    else:
        # обновим username на всякий случай
        cur.execute("UPDATE users SET username=? WHERE id=?", (username or "", user_id))
        con.commit()

def get_user(user_id: int):
    cur.execute("SELECT id, username, balance, hashrate, ref_id, is_admin FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "balance": row[2], "hashrate": row[3], "ref_id": row[4], "is_admin": row[5]}

# --- Core Handlers ---
def main_menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Баланс", callback_data="balance"),
        InlineKeyboardButton("⚡ Купить хешрейт", callback_data="buy_hashrate")
    ],[
        InlineKeyboardButton("👥 Пригласить друга", callback_data="invite"),
        InlineKeyboardButton("📈 Доход", callback_data="income_info")
    ]])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    # parse ref
    ref_id = None
    if context.args and context.args[0].isdigit():
        ref_id = int(context.args[0])
        if ref_id == u.id:
            ref_id = None
    ensure_user(u.id, u.username, ref_id)
    await update.message.reply_text(
        "👋 Добро пожаловать в облачный майнинг!\nВыбирай действие ниже.",
        reply_markup=main_menu_kb()
    )

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user = get_user(uid)
    if q.data == "balance":
        await q.edit_message_text(f"💰 Баланс: {user['balance']:.2f} USDT\n⚡ Хешрейт: {user['hashrate']:.2f} GH/s",
                                  reply_markup=main_menu_kb())
    elif q.data == "buy_hashrate":
        # создаём инвойс в CryptoBot
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        payload = {
            "asset": "USDT",
            "amount": 1,  # цена за пакет, пример
            "description": "Покупка 10 GH/s",
            "payload": str(uid)
        }
        try:
            r = requests.post(CRYPTO_CREATE_URL, headers=headers, json=payload, timeout=15)
            j = r.json()
            if j.get("ok") and j["result"].get("pay_url"):
                pay_url = j["result"]["pay_url"]
                invoice_id = j["result"]["invoice_id"]
                context.user_data["last_invoice_id"] = invoice_id
                await q.edit_message_text(
                    f"🧾 Счёт создан.\nОплати по ссылке: {pay_url}\n\n"
                    f"После оплаты пока используйте команду:\n/confirm {invoice_id}\n\n"
                    f"(Вебхуки подключим позже — будет автоматически)",
                    reply_markup=main_menu_kb()
                )
            else:
                await q.edit_message_text("❌ Не удалось создать счёт. Попробуй позже.", reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"Ошибка соединения с CryptoBot: {e}", reply_markup=main_menu_kb())
    elif q.data == "invite":
        bot_name = (await context.bot.get_me()).username
        await q.edit_message_text(f"🔗 Твоя реферальная ссылка:\nhttps://t.me/{bot_name}?start={uid}", reply_markup=main_menu_kb())
    elif q.data == "income_info":
        rate = db_get_rate()
        await q.edit_message_text(f"📈 Текущая доходность: {rate:.6f} USDT на 1 GH/s в день.\n"
                                  f"При твоём хешрейте {user['hashrate']:.2f} GH/s — это {(user['hashrate']*rate):.4f} USDT/день.",
                                  reply_markup=main_menu_kb())

# --- Manual confirm while no webhooks ---
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /confirm <invoice_id>")
        return
    invoice_id = context.args[0]
    uid = update.effective_user.id
    # Эмулируем успешную покупку: +10 GH/s
    cur.execute("UPDATE users SET hashrate = hashrate + 10 WHERE id=?", (uid,))
    con.commit()
    await update.message.reply_text(f"✅ Оплата {invoice_id} подтверждена. Хешрейт +10 GH/s.")

# --- Daily accrual ---
def do_daily_accrual():
    """Начисление дохода всем пользователям по формуле:
       accr = hashrate(GH/s) * rate(USDT/GH/день)
       + реферальный бонус 1% рефералу от дохода приглашённого.
    """
    rate = db_get_rate()
    now = int(time.time())
    with closing(sqlite3.connect(DB_PATH)) as c:
        k = c.cursor()
        k.execute("SELECT id, hashrate, ref_id FROM users")
        rows = k.fetchall()
        for uid, hr, ref_id in rows:
            if hr <= 0: 
                continue
            accr = hr * rate
            if accr <= 0:
                continue
            # начислим пользователю
            k.execute("UPDATE users SET balance = balance + ? WHERE id=?", (accr, uid))
            k.execute("INSERT INTO accruals(user_id, amount, created_at) VALUES(?,?,?)", (uid, accr, now))
            # рефералка 1%
            if ref_id:
                ref_bonus = accr * 0.01
                k.execute("UPDATE users SET balance = balance + ? WHERE id=?", (ref_bonus, ref_id))
                k.execute("INSERT INTO accruals(user_id, amount, created_at) VALUES(?,?,?)", (ref_id, ref_bonus, now))
        c.commit()

async def cmd_run_accrual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ручной запуск (на всякий случай)
    if not is_admin(update.effective_user.username):
        return
    do_daily_accrual()
    await update.message.reply_text("✅ Начисление выполнено.")

# --- Admin ---
def is_admin(username: str | None) -> bool:
    return (username or "").lower() in ADMIN_USERNAMES or (get_user_by_username(username) or {}).get("is_admin") == 1

def get_user_by_username(username: str | None):
    if not username:
        return None
    cur.execute("SELECT id, username, balance, hashrate, ref_id, is_admin FROM users WHERE LOWER(username)=LOWER(?)", (username,))
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "balance": row[2], "hashrate": row[3], "ref_id": row[4], "is_admin": row[5]}

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Кол-во пользователей", callback_data="adm_users_count"),
         InlineKeyboardButton("🏆 ТОП баланса", callback_data="adm_top")],
        [InlineKeyboardButton("⚙️ Задать доходность", callback_data="adm_set_rate"),
         InlineKeyboardButton("➕ Выдать баланс", callback_data="adm_give")],
        [InlineKeyboardButton("🚀 Начислить сейчас", callback_data="adm_accrual_now")]
    ])

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.username):
        await update.message.reply_text("⛔ Недостаточно прав.")
        return
    await update.message.reply_text("Админ-панель:", reply_markup=admin_kb())

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.username):
        await q.edit_message_text("⛔ Нет прав.")
        return
    data = q.data
    if data == "adm_users_count":
        cur.execute("SELECT COUNT(*) FROM users")
        n = cur.fetchone()[0]
        await q.edit_message_text(f"👥 Пользователей: {n}", reply_markup=admin_kb())
    elif data == "adm_top":
        cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
        rows = cur.fetchall()
        lines = ["🏆 Топ по балансу:"]
        for i, (uname, bal) in enumerate(rows, start=1):
            shown = ("@" + uname) if uname else "(без ника)"
            lines.append(f"{i}. {shown} — {bal:.2f} USDT")
        await q.edit_message_text("\n".join(lines), reply_markup=admin_kb())
    elif data == "adm_set_rate":
        await q.edit_message_text(f"Текущая ставка: {db_get_rate():.6f}\nПришли сообщением новую ставку (USDT за 1 GH/s/день).",
                                  reply_markup=None)
        context.user_data["await_rate"] = True
    elif data == "adm_give":
        await q.edit_message_text("Пришли в формате: +баланс @username 10  (или user_id 10)",
                                  reply_markup=None)
        context.user_data["await_give"] = True
    elif data == "adm_accrual_now":
        do_daily_accrual()
        await q.edit_message_text("✅ Начисление выполнено.", reply_markup=admin_kb())

async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.username):
        return
    text = (update.message.text or "").strip()
    if context.user_data.get("await_rate"):
        try:
            val = float(text.replace(",", "."))
            db_set_rate(val)
            context.user_data["await_rate"] = False
            await update.message.reply_text(f"✅ Ставка обновлена: {val:.6f}", reply_markup=admin_kb())
        except:
            await update.message.reply_text("Не удалось разобрать число. Пришли ещё раз.")
        return
    if context.user_data.get("await_give"):
        # formats: "@username 10" or "123456 10"
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: @username 10  или  123456 10")
            return
        target, amount_s = parts
        try:
            amount = float(amount_s.replace(",", "."))
        except:
            await update.message.reply_text("Сумма должна быть числом.")
            return
        uid = None
        if target.startswith("@"):
            u = get_user_by_username(target[1:])
            uid = u["id"] if u else None
        else:
            if target.isdigit():
                uid = int(target)
        if not uid:
            await update.message.reply_text("Пользователь не найден (должен написать боту хотя бы раз).")
            return
        cur.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
        con.commit()
        context.user_data["await_give"] = False
        await update.message.reply_text(f"✅ Выдал {amount} USDT пользователю {target}.", reply_markup=admin_kb())

# --- App bootstrap ---
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("accrual", cmd_run_accrual))

    # callbacks
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^(balance|buy_hashrate|invite|income_info)$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^adm_"))

    # admin text inputs
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), admin_text_input))

    # schedule daily accrual every 24h
    async def periodic_accrual(ctx: ContextTypes.DEFAULT_TYPE):
        do_daily_accrual()
    app.job_queue.run_repeating(periodic_accrual, interval=24*3600, first=30)  # первая через 30 сек

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
