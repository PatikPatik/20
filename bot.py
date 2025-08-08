
import logging
import os
import sqlite3
import time
from contextlib import closing

import re
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
ADMIN_USERNAMES = {"mkru27"}  # <-- admin

# Mining economy
DEFAULT_RATE_USDT_PER_GH_PER_DAY = 0.01   # доход в USDT на каждый GH/s в день

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
    is_admin INTEGER DEFAULT 0,
    wallet TEXT
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
cur.execute("""
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    address TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
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
        cur.execute("UPDATE users SET username=? WHERE id=?", (username or "", user_id))
        con.commit()

def get_user(user_id: int):
    cur.execute("SELECT id, username, balance, hashrate, ref_id, is_admin, wallet FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "balance": row[2], "hashrate": row[3], "ref_id": row[4], "is_admin": row[5], "wallet": row[6]}

# --- Address validation (basic regex for common chains) ---
def detect_chain(addr: str) -> str | None:
    a = addr.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", a):
        return "EVM (ETH/BSC/Polygon/Arbitrum/etc.)"
    if re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", a):
        return "TRON (TRC20)"
    if re.fullmatch(r"[13][a-km-zA-HJ-NP-Z1-9]{25,34}", a) or a.startswith("bc1"):
        return "Bitcoin"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a) and not a.startswith("T"):
        return "Solana"
    if re.fullmatch(r"[EU][A-Z0-9_-]{46}", a):
        return "TON (base64)"
    return None

# --- UI ---
def main_menu_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💰 Баланс", callback_data="balance"),
        InlineKeyboardButton("⚡ Купить хешрейт", callback_data="buy_hashrate")
    ],[
        InlineKeyboardButton("👥 Пригласить друга", callback_data="invite"),
        InlineKeyboardButton("📈 Доход", callback_data="income_info")
    ],[
        InlineKeyboardButton("💼 Кошелёк", callback_data="wallet"),
        InlineKeyboardButton("💸 Вывод", callback_data="withdraw")
    ]])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
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
        await q.edit_message_text(
            f"💰 Баланс: {user['balance']:.2f} USDT\n⚡ Хешрейт: {user['hashrate']:.2f} GH/s\n💼 Кошелёк: {user['wallet'] or 'не привязан'}",
            reply_markup=main_menu_kb())
    elif q.data == "buy_hashrate":
        headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        payload = {
            "asset": "USDT",
            "amount": 1,
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
                    f"После оплаты пока используйте команду:\n/confirm {invoice_id}",
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
    elif q.data == "wallet":
        await q.edit_message_text("Пришли адрес для вывода (поддерживаются ETH/BSC/Polygon: `0x...`, TRC20: `T...`, TON: `EQ...`, Solana: base58).",
                                  reply_markup=None, parse_mode="Markdown")
        context.user_data["await_wallet"] = True
    elif q.data == "withdraw":
        if not user["wallet"]:
            await q.edit_message_text("Сначала привяжи кошелёк: нажми «💼 Кошелёк».", reply_markup=main_menu_kb())
        else:
            await q.edit_message_text(f"Отправь сумму для вывода в USDT (числом). Кошелёк: `{user['wallet']}`", reply_markup=None, parse_mode="Markdown")
            context.user_data["await_withdraw"] = True

async def text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    msg = (update.message.text or "").strip()
    # wallet binding
    if context.user_data.get("await_wallet"):
        chain = detect_chain(msg)
        if not chain:
            await update.message.reply_text("❌ Адрес не похож на поддерживаемый. Пример: 0x.. (EVM), T.. (TRC20), EQ.. (TON), base58 (Solana). Пришли ещё раз.")
            return
        cur.execute("UPDATE users SET wallet=? WHERE id=?", (msg, uid))
        con.commit()
        context.user_data["await_wallet"] = False
        await update.message.reply_text(f"✅ Кошелёк сохранён ({chain}):\n{msg}", reply_markup=main_menu_kb())
        return
    # withdraw request
    if context.user_data.get("await_withdraw"):
        try:
            amount = float(msg.replace(",", "."))
        except:
            await update.message.reply_text("Сумма должна быть числом. Пришли ещё раз.")
            return
        if amount <= 0 or amount > user["balance"]:
            await update.message.reply_text(f"Недостаточно средств или некорректная сумма. Баланс: {user['balance']:.2f} USDT")
            return
        now = int(time.time())
        cur.execute("INSERT INTO withdrawals(user_id, amount, address, created_at) VALUES(?,?,?,?)", (uid, amount, user["wallet"], now))
        con.commit()
        context.user_data["await_withdraw"] = False
        await update.message.reply_text("✅ Заявка на вывод создана. Админ обработает её вручную.", reply_markup=main_menu_kb())
        return

# --- Manual confirm while no webhooks ---
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /confirm <invoice_id>")
        return
    invoice_id = context.args[0]
    uid = update.effective_user.id
    # успешная покупка: +10 GH/s
    cur.execute("UPDATE users SET hashrate = hashrate + 10 WHERE id=?", (uid,))
    con.commit()
    await update.message.reply_text(f"✅ Оплата {invoice_id} подтверждена. Хешрейт +10 GH/s.")

# --- Daily accrual (1% ref on profit already) ---
def do_daily_accrual():
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
            # рефералка 1% от прибыли
            if ref_id:
                ref_bonus = accr * 0.01
                k.execute("UPDATE users SET balance = balance + ? WHERE id=?", (ref_bonus, ref_id))
                k.execute("INSERT INTO accruals(user_id, amount, created_at) VALUES(?,?,?)", (ref_id, ref_bonus, now))
        c.commit()

async def cmd_run_accrual(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    cur.execute("SELECT id, username, balance, hashrate, ref_id, is_admin, wallet FROM users WHERE LOWER(username)=LOWER(?)", (username,))
    row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "balance": row[2], "hashrate": row[3], "ref_id": row[4], "is_admin": row[5], "wallet": row[6]}

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Пользователи", callback_data="adm_users_count"),
         InlineKeyboardButton("🏆 ТОП баланса", callback_data="adm_top")],
        [InlineKeyboardButton("⚙️ Ставка дохода", callback_data="adm_set_rate"),
         InlineKeyboardButton("➕ Выдать баланс", callback_data="adm_give")],
        [InlineKeyboardButton("💸 Выводы (pending)", callback_data="adm_withdrawals"),
         InlineKeyboardButton("🚀 Начислить сейчас", callback_data="adm_accrual_now")]
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
        await q.edit_message_text("Пришли в формате: @username 10  (или user_id 10)",
                                  reply_markup=None)
        context.user_data["await_give"] = True
    elif data == "adm_accrual_now":
        do_daily_accrual()
        await q.edit_message_text("✅ Начисление выполнено.", reply_markup=admin_kb())
    elif data == "adm_withdrawals":
        cur.execute("SELECT id, user_id, amount, address, status FROM withdrawals WHERE status='pending' ORDER BY id ASC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет ожидающих заявок.", reply_markup=admin_kb())
            return
        lines = ["💸 Ожидающие выводы:"]
        kb = []
        for wid, uid, amt, addr, st in rows:
            lines.append(f"#{wid}: uid {uid}, {amt:.2f} USDT, {addr}")
            kb.append([InlineKeyboardButton(f"✅ #{wid}", callback_data=f"adm_w_ok_{wid}"),
                       InlineKeyboardButton(f"❌ #{wid}", callback_data=f"adm_w_rej_{wid}")])
        kb.append([InlineKeyboardButton("⟵ Назад", callback_data="adm_back")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

    elif data == "adm_back":
        await q.edit_message_text("Админ-панель:", reply_markup=admin_kb())

    elif data.startswith("adm_w_ok_"):
        wid = int(data.split("_")[-1])
        # одобрение: списываем баланс и помечаем approved
        cur.execute("SELECT user_id, amount FROM withdrawals WHERE id=? AND status='pending'", (wid,))
        row = cur.fetchone()
        if not row:
            await q.edit_message_text("Заявка не найдена или уже обработана.", reply_markup=admin_kb()); return
        uid, amt = row
        cur.execute("UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (amt, uid, amt))
        cur.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wid,))
        con.commit()
        await q.edit_message_text(f"✅ Заявка #{wid} одобрена. Списано {amt:.2f} USDT.", reply_markup=admin_kb())

    elif data.startswith("adm_w_rej_"):
        wid = int(data.split("_")[-1])
        cur.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
        con.commit()
        await q.edit_message_text(f"❌ Заявка #{wid} отклонена.", reply_markup=admin_kb())

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
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: @username 10  или  123456 10")
            return
        target, amount_s = parts
        try:
            amount = float(amount_s.replace(",", "."))
        except:
            await update.message.reply_text("Сумма должна быть числом."); return
        uid = None
        if target.startswith("@"):
            u = get_user_by_username(target[1:])
            uid = u["id"] if u else None
        else:
            if target.isdigit():
                uid = int(target)
        if not uid:
            await update.message.reply_text("Пользователь не найден (должен написать боту хотя бы раз)."); return
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
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^(balance|buy_hashrate|invite|income_info|wallet|withdraw)$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^adm_"))

    # text flows (wallet / withdraw / admin inputs)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_flow))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), admin_text_input))

    # schedule daily accrual every 24h
    async def periodic_accrual(ctx: ContextTypes.DEFAULT_TYPE):
        do_daily_accrual()
    app.job_queue.run_repeating(periodic_accrual, interval=24*3600, first=30)

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
