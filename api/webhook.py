import os
import sys
import json
import logging

# ─────────────────────────────────────────────────────────────
# Logging FIRST
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("shop-bot")

# ─────────────────────────────────────────────────────────────
# Boot status tracking
# ─────────────────────────────────────────────────────────────
_boot_errors = {}

try:
    import asyncio
    import sqlite3
    import base64
    from io import BytesIO
    from datetime import datetime
    _STDLIB_OK = True
except Exception as e:
    _boot_errors["stdlib"] = repr(e)
    _STDLIB_OK = False

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]
ADMIN_PANEL_USER = os.environ.get("ADMIN_PANEL_USER", "admin").strip()
ADMIN_PANEL_PASS = os.environ.get("ADMIN_PANEL_PASS", "").strip()

# ⚠️ FIX: /tmp is ephemeral on Vercel. For production use an external DB.
#          Kept here so nothing breaks, but data resets on cold start.
DB_PATH = os.environ.get("DB_PATH", "/tmp/shop.db")

if not TOKEN:
    logger.error("BOT_TOKEN environment variable is not set")
if not ADMIN_PANEL_PASS:
    logger.error("ADMIN_PANEL_PASS environment variable is not set")

try:
    from starlette.applications import Starlette
    from starlette.responses import (
        HTMLResponse, JSONResponse, PlainTextResponse, Response,
    )
    from starlette.routing import Route
    _HAS_STARLETTE = True
except Exception as e:
    _boot_errors["starlette"] = repr(e)
    _HAS_STARLETTE = False

try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup,
        BotCommand, InputFile,
    )
    from telegram.ext import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, filters, ContextTypes,
    )
    from telegram.constants import ParseMode
    _HAS_TG = True
except Exception as e:
    _boot_errors["telegram"] = repr(e)
    _HAS_TG = False

BOOT_OK = _STDLIB_OK and _HAS_STARLETTE and _HAS_TG

if BOOT_OK:
    logger.info("BOOT OK. TOKEN set=%s, ADMIN_IDS=%s", bool(TOKEN), ADMIN_IDS)
else:
    logger.error("BOOT FAILED: %s", _boot_errors)


async def _fallback_asgi(scope, receive, send):
    if scope["type"] != "http":
        return
    body = json.dumps({
        "status": "boot_failed",
        "errors": _boot_errors,
        "hint": (
            "pyproject.toml [project].dependencies must include: "
            "python-telegram-bot>=21.6,<22 and starlette>=0.37,<1.0"
        ),
    }, indent=2).encode()
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def build_app():
    # ═════════════════════════════════════════════
    # Database
    # ═════════════════════════════════════════════
    def _get_conn():
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            balance REAL DEFAULT 0, is_banned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT DEFAULT '', price REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS stock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL, content TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0, sold_to INTEGER, sold_at TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, stock_item_id INTEGER,
            amount REAL NOT NULL, status TEXT DEFAULT 'completed',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS topup_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount REAL NOT NULL, status TEXT DEFAULT 'awaiting_payment',
            reference TEXT, receipt_file_id TEXT, receipt_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            subject TEXT, status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL, is_admin INTEGER DEFAULT 0,
            message TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        conn.commit()
        conn.close()

    def db_execute(sql, params=()):
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        lid = cur.lastrowid
        conn.close()
        return lid

    def db_fetchone(sql, params=()):
        conn = _get_conn()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None

    def db_fetchall(sql, params=()):
        conn = _get_conn()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ═════════════════════════════════════════════
    # Settings
    # ═════════════════════════════════════════════
    async def get_setting(key, default=None):
        row = await asyncio.to_thread(
            db_fetchone, "SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(key, value):
        await asyncio.to_thread(
            db_execute,
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    async def get_qr():
        raw = await get_setting("payment_qr")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set_qr(qtype, value):
        await set_setting("payment_qr",
                          json.dumps({"type": qtype, "value": value}))

    async def send_qr(bot, chat_id, caption):
        qr = await get_qr()
        if not qr:
            return None
        try:
            if qr["type"] in ("file_id", "url"):
                return await bot.send_photo(
                    chat_id=chat_id, photo=qr["value"], caption=caption,
                    parse_mode=ParseMode.HTML)
            if qr["type"] == "b64":
                data = base64.b64decode(qr["value"])
                photo = InputFile(BytesIO(data), filename="qr.png")
                return await bot.send_photo(
                    chat_id=chat_id, photo=photo, caption=caption,
                    parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.exception("send_qr failed: %s", e)
        return None

    # ═════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════
    def is_admin(uid):
        return uid in ADMIN_IDS

    async def ensure_user(update):
        u = update.effective_user
        if not u:
            return
        await asyncio.to_thread(
            db_execute,
            """INSERT INTO users (user_id, username, first_name)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name""",
            (u.id, u.username, u.first_name))

    async def get_user(uid):
        return await asyncio.to_thread(
            db_fetchone, "SELECT * FROM users WHERE user_id = ?", (uid,))

    def main_menu_kb(uid):
        kb = [
            [InlineKeyboardButton("🛍 Shop", callback_data="menu_shop")],
            [InlineKeyboardButton("💰 Balance", callback_data="menu_balance")],
            [InlineKeyboardButton("📦 My Orders", callback_data="menu_orders")],
            [InlineKeyboardButton("🆘 Support", callback_data="menu_support")],
        ]
        if is_admin(uid):
            kb.append([InlineKeyboardButton("⚙️ Admin",
                                            callback_data="admin_main")])
        return InlineKeyboardMarkup(kb)

    def admin_menu_kb():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Products",
                                  callback_data="admin_products")],
            [InlineKeyboardButton("📊 Stock", callback_data="admin_stock")],
            [InlineKeyboardButton("🧾 Orders", callback_data="admin_orders")],
            [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
            [InlineKeyboardButton("💳 Top‑ups",
                                  callback_data="admin_topups")],
            [InlineKeyboardButton("⚙️ Payment",
                                  callback_data="admin_payment")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
        ])

    def products_list_kb(products):
        kb = [
            [InlineKeyboardButton(
                f"{p['name']} – ${p['price']:.2f}",
                callback_data=f"buy_{p['id']}")] for p in products
        ]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])
        return InlineKeyboardMarkup(kb)

    # ═════════════════════════════════════════════
    # Bot commands
    # ═════════════════════════════════════════════
    async def cmd_start(update, context):
        await ensure_user(update)
        await update.message.reply_text(
            f"👋 Welcome, <b>{update.effective_user.first_name}</b>!\n\n"
            "Use the menu to shop, check balance, view orders, or get support.",
            reply_markup=main_menu_kb(update.effective_user.id),
            parse_mode=ParseMode.HTML)

    async def cmd_shop(update, context):
        await ensure_user(update)
        products = await asyncio.to_thread(
            db_fetchall,
            "SELECT * FROM products WHERE is_active = 1 ORDER BY id")
        if not products:
            await update.message.reply_text("🛒 Shop is empty right now.")
            return
        await update.message.reply_text(
            "🛍 <b>Available products:</b>",
            reply_markup=products_list_kb(products),
            parse_mode=ParseMode.HTML)

    async def cmd_balance(update, context):
        await ensure_user(update)
        user = await get_user(update.effective_user.id)
        await update.message.reply_text(
            f"💰 <b>Balance:</b> <code>${user['balance']:.2f}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Top Up",
                                      callback_data="topup_start")],
                [InlineKeyboardButton("🔙 Back",
                                      callback_data="menu_main")]]),
            parse_mode=ParseMode.HTML)

    async def cmd_orders(update, context):
        await ensure_user(update)
        orders = await asyncio.to_thread(
            db_fetchall,
            """SELECT o.*, p.name AS product_name FROM orders o
               JOIN products p ON p.id = o.product_id
               WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20""",
            (update.effective_user.id,))
        if not orders:
            await update.message.reply_text("📭 No orders yet.")
            return
        lines = ["🧾 <b>Your orders:</b>\n"]
        for o in orders:
            lines.append(
                f"• <b>{o['product_name']}</b> – ${o['amount']:.2f} "
                f"({o['status']}) – {o['created_at'][:10]}")
        await update.message.reply_text("\n".join(lines),
                                        parse_mode=ParseMode.HTML)

    async def cmd_support(update, context):
        await ensure_user(update)
        context.user_data["support_state"] = "awaiting_subject"
        await update.message.reply_text(
            "🆘 <b>Support</b>\n\nDescribe your issue in one message.",
            parse_mode=ParseMode.HTML)

    # ═════════════════════════════════════════════
    # Top-up helpers
    # ═════════════════════════════════════════════
    async def _approve_topup(rid, bot):
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (rid,))
        if not req or req["status"] != "pending":
            return False
        await asyncio.to_thread(
            db_execute,
            "UPDATE topup_requests SET status = 'approved' WHERE id = ?",
            (rid,))
        await asyncio.to_thread(
            db_execute,
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (req["amount"], req["user_id"]))
        try:
            await bot.send_message(
                chat_id=req["user_id"],
                text=f"✅ <b>Top‑up approved!</b>\n\n"
                     f"<b>${req['amount']:.2f}</b> added to your balance.\n"
                     f"Request: <code>#{rid}</code>",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return True

    async def _reject_topup(rid, bot):
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (rid,))
        if not req or req["status"] != "pending":
            return False
        await asyncio.to_thread(
            db_execute,
            "UPDATE topup_requests SET status = 'rejected' WHERE id = ?",
            (rid,))
        try:
            await bot.send_message(
                chat_id=req["user_id"],
                text=f"❌ <b>Top‑up rejected</b>\n\n"
                     f"Request <code>#{rid}</code> was rejected.",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return True

    # ═════════════════════════════════════════════
    # Callback handler
    # ═════════════════════════════════════════════
    async def callback_handler(update, context):
        q = update.callback_query
        await q.answer()
        data = q.data
        uid = q.from_user.id
        await ensure_user(update)
        user = await get_user(uid)

        if data == "menu_main":
            for k in ("admin_state", "support_state",
                      "topup_state", "topup_request_id"):
                context.user_data.pop(k, None)
            await q.edit_message_text(
                "🏠 <b>Main menu</b>",
                reply_markup=main_menu_kb(uid), parse_mode=ParseMode.HTML)
            return

        if data == "menu_shop":
            products = await asyncio.to_thread(
                db_fetchall,
                "SELECT * FROM products WHERE is_active = 1 ORDER BY id")
            if not products:
                await q.edit_message_text(
                    "🛒 Shop is empty.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back",
                                               callback_data="menu_main")]]))
                return
            await q.edit_message_text(
                "🛍 <b>Available products:</b>",
                reply_markup=products_list_kb(products),
                parse_mode=ParseMode.HTML)
            return

        if data.startswith("buy_"):
            pid = int(data.split("_")[1])
            product = await asyncio.to_thread(
                db_fetchone, "SELECT * FROM products WHERE id = ?", (pid,))
            if not product:
                await q.edit_message_text("❌ Product not found.")
                return
            stock = await asyncio.to_thread(
                db_fetchone,
                "SELECT COUNT(*) AS cnt FROM stock_items "
                "WHERE product_id = ? AND is_sold = 0", (pid,))
            if stock["cnt"] == 0:
                await q.edit_message_text(
                    "❌ Out of stock.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back",
                                               callback_data="menu_shop")]]))
                return
            if user["balance"] < product["price"]:
                await q.edit_message_text(
                    f"❌ Insufficient balance.\n\n"
                    f"Price: <b>${product['price']:.2f}</b>\n"
                    f"Balance: <b>${user['balance']:.2f}</b>",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ Top Up",
                                              callback_data="topup_start")],
                        [InlineKeyboardButton("🔙 Back",
                                              callback_data="menu_shop")]]),
                    parse_mode=ParseMode.HTML)
                return
            item = await asyncio.to_thread(
                db_fetchone,
                "SELECT * FROM stock_items "
                "WHERE product_id = ? AND is_sold = 0 LIMIT 1", (pid,))
            if not item:
                await q.edit_message_text("❌ Stock gone. Try again.")
                return
            await asyncio.to_thread(
                db_execute,
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (product["price"], uid))
            await asyncio.to_thread(
                db_execute,
                "UPDATE stock_items SET is_sold = 1, sold_to = ?, "
                "sold_at = ? WHERE id = ?",
                (uid, datetime.utcnow().isoformat(), item["id"]))
            await asyncio.to_thread(
                db_execute,
                "INSERT INTO orders "
                "(user_id, product_id, stock_item_id, amount, status) "
                "VALUES (?, ?, ?, ?, 'completed')",
                (uid, pid, item["id"], product["price"]))
            await q.edit_message_text(
                f"✅ <b>Purchase successful!</b>\n\n"
                f"Product: <b>{product['name']}</b>\n"
                f"Price: <b>${product['price']:.2f}</b>\n\n"
                f"<b>Your item:</b>\n<code>{item['content']}</code>",
                parse_mode=ParseMode.HTML)
            return

        if data == "menu_balance":
            await q.edit_message_text(
                f"💰 <b>Balance:</b> <code>${user['balance']:.2f}</code>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Top Up",
                                          callback_data="topup_start")],
                    [InlineKeyboardButton("🔙 Back",
                                          callback_data="menu_main")]]),
                parse_mode=ParseMode.HTML)
            return

        if data == "topup_start":
            qr = await get_qr()
            if not qr:
                await q.edit_message_text(
                    "⚠️ Top‑up unavailable. Contact support.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back",
                                               callback_data="menu_main")]]))
                return
            context.user_data["topup_state"] = "awaiting_amount"
            await q.edit_message_text(
                "💳 <b>Top Up</b>\n\nSend the amount (e.g. <code>10</code>).\n"
                "Minimum: <b>$1</b>", parse_mode=ParseMode.HTML)
            return

        if data == "menu_orders":
            orders = await asyncio.to_thread(
                db_fetchall,
                """SELECT o.*, p.name AS product_name FROM orders o
                   JOIN products p ON p.id = o.product_id
                   WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20""",
                (uid,))
            if not orders:
                await q.edit_message_text(
                    "📭 No orders.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back",
                                               callback_data="menu_main")]]))
                return
            lines = ["🧾 <b>Your orders:</b>\n"]
            for o in orders:
                lines.append(
                    f"• <b>{o['product_name']}</b> – ${o['amount']:.2f} "
                    f"({o['status']}) – {o['created_at'][:10]}")
            await q.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back",
                                           callback_data="menu_main")]]))
            return

        if data == "menu_support":
            context.user_data["support_state"] = "awaiting_subject"
            await q.edit_message_text(
                "🆘 Describe your issue in one message.",
                parse_mode=ParseMode.HTML)
            return

        if not is_admin(uid):
            return

        if data == "admin_main":
            await q.edit_message_text(
                "⚙️ <b>Admin panel</b>",
                reply_markup=admin_menu_kb(), parse_mode=ParseMode.HTML)
            return

        if data == "admin_products":
            products = await asyncio.to_thread(
                db_fetchall, "SELECT * FROM products ORDER BY id")
            lines = ["📦 <b>Products</b>\n"]
            for p in products:
                lines.append(
                    f"• #{p['id']} <b>{p['name']}</b> – ${p['price']:.2f} "
                    f"({'✅' if p['is_active'] else '❌'})")
            if not products:
                lines.append("(none)")
            await q.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Product",
                                          callback_data="admin_addproduct")],
                    [InlineKeyboardButton("🔙 Back",
                                          callback_data="admin_main")]]),
                parse_mode=ParseMode.HTML)
            return

        if data == "admin_addproduct":
            context.user_data["admin_state"] = "add_product_name"
            await q.edit_message_text("📝 Send product name.")
            return

        if data == "admin_stock":
            products = await asyncio.to_thread(
                db_fetchall, "SELECT * FROM products ORDER BY id")
            lines = ["📊 <b>Stock</b>\n"]
            for p in products:
                cnt = await asyncio.to_thread(
                    db_fetchone,
                    "SELECT COUNT(*) AS c FROM stock_items "
                    "WHERE product_id = ? AND is_sold = 0", (p["id"],))
                lines.append(f"• <b>{p['name']}</b> – {cnt['c']}")
            if not products:
                lines.append("(none)")
            await q.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Stock",
                                          callback_data="admin_addstock")],
                    [InlineKeyboardButton("🔙 Back",
                                          callback_data="admin_main")]]),
                parse_mode=ParseMode.HTML)
            return

        if data == "admin_addstock":
            products = await asyncio.to_thread(
                db_fetchall, "SELECT * FROM products ORDER BY id")
            if not products:
                await q.edit_message_text("Add a product first.")
                return
            context.user_data["admin_state"] = "add_stock_product"
            lines = ["Send the <b>product ID</b>:\n"]
            for p in products:
                lines.append(f"#{p['id']} – {p['name']}")
            await q.edit_message_text("\n".join(lines),
                                      parse_mode=ParseMode.HTML)
            return

        if data == "admin_orders":
            orders = await asyncio.to_thread(
                db_fetchall,
                """SELECT o.*, p.name AS product_name, u.username
                   FROM orders o JOIN products p ON p.id = o.product_id
                   LEFT JOIN users u ON u.user_id = o.user_id
                   ORDER BY o.id DESC LIMIT 30""")
            lines = ["🧾 <b>Recent orders</b>\n"]
            for o in orders:
                lines.append(
                    f"• #{o['id']} {o['product_name']} – "
                    f"${o['amount']:.2f} "
                    f"@{o['username'] or o['user_id']}")
            await q.edit_message_text(
                "\n".join(lines) or "No orders.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back",
                                           callback_data="admin_main")]]))
            return

        if data == "admin_users":
            users = await asyncio.to_thread(
                db_fetchall,
                "SELECT * FROM users ORDER BY created_at DESC LIMIT 30")
            lines = ["👥 <b>Users</b>\n"]
            for u in users:
                lines.append(
                    f"• <code>{u['user_id']}</code> "
                    f"@{u['username'] or '—'} – ${u['balance']:.2f}")
            await q.edit_message_text(
                "\n".join(lines) or "No users.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back",
                                           callback_data="admin_main")]]))
            return

        if data == "admin_topups":
            reqs = await asyncio.to_thread(
                db_fetchall,
                """SELECT r.*, u.username FROM topup_requests r
                   LEFT JOIN users u ON u.user_id = r.user_id
                   WHERE r.status = 'pending'
                   ORDER BY r.id DESC LIMIT 20""")
            if not reqs:
                await q.edit_message_text(
                    "✅ No pending top‑ups.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔙 Back",
                                               callback_data="admin_main")]]))
                return
            kb = []
            for r in reqs:
                kb.append([InlineKeyboardButton(
                    f"👁 #{r['id']} @{r['username'] or r['user_id']} – "
                    f"${r['amount']:.2f}",
                    callback_data=f"tu_view_{r['id']}")])
            kb.append([InlineKeyboardButton("🔙 Back",
                                            callback_data="admin_main")])
            await q.edit_message_text(
                "💳 <b>Pending top‑ups</b>",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.HTML)
            return

        if data.startswith("tu_view_"):
            rid = int(data.split("_")[2])
            req = await asyncio.to_thread(
                db_fetchone,
                "SELECT * FROM topup_requests WHERE id = ?", (rid,))
            if not req:
                await q.edit_message_text("Not found.")
                return
            u = await get_user(req["user_id"])
            caption = (
                f"💳 <b>Top‑up #{rid}</b>\n\n"
                f"<b>User:</b> @{u['username'] or 'N/A'} "
                f"(<code>{req['user_id']}</code>)\n"
                f"<b>Amount:</b> <code>${req['amount']:.2f}</code>\n"
                f"<b>Reference:</b> "
                f"<code>{req['reference'] or '—'}</code>")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve",
                                      callback_data=f"tu_ok_{rid}"),
                 InlineKeyboardButton("❌ Reject",
                                      callback_data=f"tu_no_{rid}")],
                [InlineKeyboardButton("🔙 Back",
                                      callback_data="admin_topups")],
            ])
            if req["receipt_file_id"] and req["receipt_type"] == "photo":
                await q.message.reply_photo(
                    photo=req["receipt_file_id"], caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            elif req["receipt_file_id"]:
                await q.message.reply_document(
                    document=req["receipt_file_id"], caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await q.message.reply_text(
                    caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if data.startswith("tu_ok_"):
            rid = int(data.split("_")[2])
            await _approve_topup(rid, context.bot)
            try:
                await q.edit_message_caption(caption=f"✅ Approved #{rid}")
            except Exception:
                await q.edit_message_text(f"✅ Approved #{rid}")
            return

        if data.startswith("tu_no_"):
            rid = int(data.split("_")[2])
            await _reject_topup(rid, context.bot)
            try:
                await q.edit_message_caption(caption=f"❌ Rejected #{rid}")
            except Exception:
                await q.edit_message_text(f"❌ Rejected #{rid}")
            return

        if data == "admin_payment":
            qr = await get_qr()
            note = await get_setting("payment_note") or "(not set)"
            method = await get_setting("payment_method") or "(not set)"
            text = (
                f"⚙️ <b>Payment</b>\n\n"
                f"<b>Method:</b> {method}\n<b>Note:</b> {note}\n"
                f"<b>QR:</b> {'✅' if qr else '❌'}")
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🖼 Set QR",
                                          callback_data="admin_setqr")],
                    [InlineKeyboardButton("📝 Set Note",
                                          callback_data="admin_setnote")],
                    [InlineKeyboardButton("🏦 Set Method",
                                          callback_data="admin_setmethod")],
                    [InlineKeyboardButton("🔙 Back",
                                          callback_data="admin_main")]]))
            return

        if data == "admin_setqr":
            context.user_data["admin_state"] = "set_payment_qr"
            await q.edit_message_text("📸 Send QR image.")
            return
        if data == "admin_setnote":
            context.user_data["admin_state"] = "set_payment_note"
            await q.edit_message_text("📝 Send payment note.")
            return
        if data == "admin_setmethod":
            context.user_data["admin_state"] = "set_payment_method"
            await q.edit_message_text("🏦 Send method name.")
            return

    # ═════════════════════════════════════════════
    # Photo + document handlers
    # ═════════════════════════════════════════════
    async def photo_handler(update, context):
        await ensure_user(update)
        uid = update.effective_user.id
        msg = update.message

        if (is_admin(uid)
                and context.user_data.get("admin_state") == "set_payment_qr"):
            await set_qr("file_id", msg.photo[-1].file_id)
            context.user_data.pop("admin_state", None)
            await msg.reply_text("✅ QR saved.")
            return

        if context.user_data.get("topup_state") == "awaiting_receipt":
            rid = context.user_data.get("topup_request_id")
            if not rid:
                return
            req = await asyncio.to_thread(
                db_fetchone,
                "SELECT * FROM topup_requests WHERE id = ?", (rid,))
            if not req or req["status"] != "awaiting_payment":
                await msg.reply_text("⚠️ Request no longer active.")
                context.user_data.pop("topup_state", None)
                context.user_data.pop("topup_request_id", None)
                return
            fid = msg.photo[-1].file_id
            await asyncio.to_thread(
                db_execute,
                "UPDATE topup_requests SET receipt_file_id = ?, "
                "receipt_type = 'photo', status = 'pending' WHERE id = ?",
                (fid, rid))
            context.user_data.pop("topup_state", None)
            context.user_data.pop("topup_request_id", None)
            await msg.reply_text(
                f"✅ Receipt received for <b>#{rid}</b>. "
                f"Admin will verify shortly.", parse_mode=ParseMode.HTML)
            user = await get_user(uid)
            caption = (
                f"💳 <b>New Top‑up</b>\n\n<b>Request:</b> #{rid}\n"
                f"<b>User:</b> @{user['username'] or 'N/A'} "
                f"(<code>{uid}</code>)\n"
                f"<b>Amount:</b> <code>${req['amount']:.2f}</code>\n"
                f"<b>Reference:</b> "
                f"<code>{req['reference'] or '—'}</code>")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve",
                                     callback_data=f"tu_ok_{rid}"),
                InlineKeyboardButton("❌ Reject",
                                     callback_data=f"tu_no_{rid}")]])
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_photo(
                        chat_id=admin_id, photo=fid, caption=caption,
                        parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception:
                    pass

    async def document_handler(update, context):
        await ensure_user(update)
        uid = update.effective_user.id
        msg = update.message
        if context.user_data.get("topup_state") != "awaiting_receipt":
            return
        rid = context.user_data.get("topup_request_id")
        if not rid:
            return
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (rid,))
        if not req or req["status"] != "awaiting_payment":
            await msg.reply_text("⚠️ Request no longer active.")
            return
        fid = msg.document.file_id
        await asyncio.to_thread(
            db_execute,
            "UPDATE topup_requests SET receipt_file_id = ?, "
            "receipt_type = 'document', status = 'pending' WHERE id = ?",
            (fid, rid))
        context.user_data.pop("topup_state", None)
        context.user_data.pop("topup_request_id", None)
        await msg.reply_text(
            f"✅ Receipt received for <b>#{rid}</b>.",
            parse_mode=ParseMode.HTML)
        user = await get_user(uid)
        caption = (
            f"💳 <b>New Top‑up</b>\n\n<b>Request:</b> #{rid}\n"
            f"<b>User:</b> @{user['username'] or 'N/A'} "
            f"(<code>{uid}</code>)\n"
            f"<b>Amount:</b> <code>${req['amount']:.2f}</code>")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve",
                                 callback_data=f"tu_ok_{rid}"),
            InlineKeyboardButton("❌ Reject",
                                 callback_data=f"tu_no_{rid}")]])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_document(
                    chat_id=admin_id, document=fid, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    # ═════════════════════════════════════════════
    # Text handler
    # ═════════════════════════════════════════════
    async def message_handler(update, context):
        await ensure_user(update)
        uid = update.effective_user.id
        user = await get_user(uid)
        text = update.message.text.strip()

        if context.user_data.get("topup_state") == "awaiting_amount":
            context.user_data.pop("topup_state", None)
            try:
                amount = float(text)
                if amount < 1:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Invalid amount. Send ≥ 1.")
                return
            qr = await get_qr()
            if not qr:
                await update.message.reply_text(
                    "⚠️ Payment not configured.")
                return
            note = (await get_setting("payment_note")
                    or "Send receipt after payment.")
            method = await get_setting("payment_method") or "Payment"
            ref = f"TP{int(datetime.utcnow().timestamp())}{uid % 10000}"
            rid = await asyncio.to_thread(
                db_execute,
                "INSERT INTO topup_requests "
                "(user_id, amount, reference, status) "
                "VALUES (?, ?, ?, 'awaiting_payment')",
                (uid, amount, ref))
            context.user_data["topup_state"] = "awaiting_receipt"
            context.user_data["topup_request_id"] = rid
            caption = (
                f"💳 <b>Complete your payment</b>\n\n"
                f"<b>Amount:</b> <code>${amount:.2f}</code>\n"
                f"<b>Method:</b> {method}\n"
                f"<b>Reference:</b> <code>{ref}</code>\n\n"
                f"📝 <b>Note:</b>\n{note}\n\n"
                f"📸 After paying, <b>send your receipt here</b>.")
            sent = await send_qr(context.bot,
                                 update.effective_chat.id, caption)
            if not sent:
                await update.message.reply_text(
                    caption, parse_mode=ParseMode.HTML)
            return

        if context.user_data.get("support_state") == "awaiting_subject":
            context.user_data.pop("support_state", None)
            tid = await asyncio.to_thread(
                db_execute,
                "INSERT INTO tickets (user_id, subject) VALUES (?, ?)",
                (uid, text[:200]))
            await update.message.reply_text(
                f"✅ Ticket <b>#{tid}</b> created.",
                parse_mode=ParseMode.HTML)
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🎫 Ticket #{tid} from "
                             f"@{user['username'] or uid}:\n{text}")
                except Exception:
                    pass
            return

        if not is_admin(uid):
            return
        state = context.user_data.get("admin_state")

        if state == "set_payment_note":
            await set_setting("payment_note", text)
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("✅ Note saved.")
            return
        if state == "set_payment_method":
            await set_setting("payment_method", text)
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("✅ Method saved.")
            return
        if state == "add_product_name":
            context.user_data["new_product_name"] = text
            context.user_data["admin_state"] = "add_product_price"
            await update.message.reply_text("Send price (e.g. 9.99).")
            return
        if state == "add_product_price":
            try:
                price = float(text)
            except ValueError:
                await update.message.reply_text("❌ Invalid price.")
                return
            context.user_data["new_product_price"] = price
            context.user_data["admin_state"] = "add_product_desc"
            await update.message.reply_text(
                "Send description (or type: skip).")
            return
        if state == "add_product_desc":
            name = context.user_data.pop("new_product_name")
            price = context.user_data.pop("new_product_price")
            desc = "" if text.lower() == "skip" else text
            pid = await asyncio.to_thread(
                db_execute,
                "INSERT INTO products (name, description, price) "
                "VALUES (?, ?, ?)", (name, desc, price))
            context.user_data.pop("admin_state", None)
            await update.message.reply_text(
                f"✅ Product <b>#{pid}</b> added.",
                parse_mode=ParseMode.HTML)
            return
        if state == "add_stock_product":
            try:
                pid = int(text)
            except ValueError:
                await update.message.reply_text(
                    "❌ Send numeric product ID.")
                return
            p = await asyncio.to_thread(
                db_fetchone,
                "SELECT * FROM products WHERE id = ?", (pid,))
            if not p:
                await update.message.reply_text("❌ Product not found.")
                return
            context.user_data["stock_product_id"] = pid
            context.user_data["admin_state"] = "add_stock_content"
            await update.message.reply_text(
                f"Send stock content for <b>{p['name']}</b> "
                f"(one per line).", parse_mode=ParseMode.HTML)
            return
        if state == "add_stock_content":
            pid = context.user_data.pop("stock_product_id")
            context.user_data.pop("admin_state", None)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for line in lines:
                await asyncio.to_thread(
                    db_execute,
                    "INSERT INTO stock_items (product_id, content) "
                    "VALUES (?, ?)", (pid, line))
            await update.message.reply_text(
                f"✅ Added {len(lines)} item(s) to product #{pid}.")
            return

    # ═════════════════════════════════════════════
    # ⚠️ FIXED: Telegram Application with correct
    #           lock-per-loop handling + started flag
    # ═════════════════════════════════════════════
    _tg_app = None
    _tg_loop = None
    _tg_started = False
    _lock = None
    _lock_loop = None  # ⚠️ NEW: track loop the lock belongs to

    def _get_lock():
        nonlocal _lock, _lock_loop
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        # ⚠️ If loop changed, the old lock is bound to a dead loop — recreate
        if _lock is None or _lock_loop is not cur:
            _lock = asyncio.Lock()
            _lock_loop = cur
        return _lock

    async def get_tg_app():
        nonlocal _tg_app, _tg_loop, _tg_started
        if not TOKEN:
            raise RuntimeError("BOT_TOKEN environment variable is not set")

        cur_loop = asyncio.get_running_loop()
        if _tg_app is not None and _tg_loop is cur_loop:
            return _tg_app

        lock = _get_lock()
        async with lock:
            if _tg_app is not None and _tg_loop is cur_loop:
                return _tg_app

            app = Application.builder().token(TOKEN).build()
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("shop", cmd_shop))
            app.add_handler(CommandHandler("balance", cmd_balance))
            app.add_handler(CommandHandler("orders", cmd_orders))
            app.add_handler(CommandHandler("support", cmd_support))
            app.add_handler(CallbackQueryHandler(callback_handler))
            app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
            app.add_handler(MessageHandler(filters.Document.ALL,
                                           document_handler))
            app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND, message_handler))

            await app.initialize()
            # ⚠️ NEW: start the update processor (required for process_update)
            await app.start()
            try:
                await app.bot.set_my_commands([
                    BotCommand("start", "Main menu"),
                    BotCommand("shop", "Browse products"),
                    BotCommand("balance", "Balance / top up"),
                    BotCommand("orders", "My orders"),
                    BotCommand("support", "Contact support"),
                ])
            except Exception as e:
                logger.warning("set_my_commands failed: %s", e)

            _tg_app = app
            _tg_loop = cur_loop
            _tg_started = True
            logger.info("Telegram Application initialized on loop %s", cur_loop)
            return app

    # ═════════════════════════════════════════════
    # Lazy DB init
    # ═════════════════════════════════════════════
    _db_ready = False

    async def ensure_db():
        nonlocal _db_ready
        if _db_ready:
            return
        await asyncio.to_thread(init_db)
        _db_ready = True

    # ═════════════════════════════════════════════
    # Admin API dispatch
    # ═════════════════════════════════════════════
    async def dispatch_admin(action, body):
        if action == "stats":
            u = await asyncio.to_thread(
                db_fetchone, "SELECT COUNT(*) AS c FROM users")
            p = await asyncio.to_thread(
                db_fetchone, "SELECT COUNT(*) AS c FROM products")
            o = await asyncio.to_thread(
                db_fetchone, "SELECT COUNT(*) AS c FROM orders")
            pt = await asyncio.to_thread(
                db_fetchone,
                "SELECT COUNT(*) AS c FROM topup_requests "
                "WHERE status = 'pending'")
            rev = await asyncio.to_thread(
                db_fetchone,
                "SELECT COALESCE(SUM(amount), 0) AS s FROM orders "
                "WHERE status = 'completed'")
            return JSONResponse({
                "users": u["c"], "products": p["c"],
                "orders": o["c"], "pending_topups": pt["c"],
                "revenue": rev["s"]})

        if action == "products_list":
            rows = await asyncio.to_thread(
                db_fetchall, "SELECT * FROM products ORDER BY id")
            return JSONResponse(rows)

        if action == "product_add":
            name = (body.get("name") or "").strip()
            desc = (body.get("description") or "").strip()
            try:
                price = float(body.get("price", 0))
            except Exception:
                return JSONResponse({"error": "bad price"},
                                    status_code=400)
            if not name or price <= 0:
                return JSONResponse({"error": "invalid"},
                                    status_code=400)
            pid = await asyncio.to_thread(
                db_execute,
                "INSERT INTO products (name, description, price) "
                "VALUES (?, ?, ?)", (name, desc, price))
            return JSONResponse({"id": pid})

        if action == "product_delete":
            pid = int(body.get("id", 0))
            await asyncio.to_thread(
                db_execute, "DELETE FROM products WHERE id = ?", (pid,))
            return JSONResponse({"ok": True})

        if action == "product_toggle":
            pid = int(body.get("id", 0))
            active = 1 if body.get("is_active") else 0
            await asyncio.to_thread(
                db_execute,
                "UPDATE products SET is_active = ? WHERE id = ?",
                (active, pid))
            return JSONResponse({"ok": True})

        if action == "stock_list":
            products = await asyncio.to_thread(
                db_fetchall, "SELECT * FROM products ORDER BY id")
            out = []
            for p in products:
                a = await asyncio.to_thread(
                    db_fetchone,
                    "SELECT COUNT(*) AS c FROM stock_items "
                    "WHERE product_id = ? AND is_sold = 0", (p["id"],))
                s = await asyncio.to_thread(
                    db_fetchone,
                    "SELECT COUNT(*) AS c FROM stock_items "
                    "WHERE product_id = ? AND is_sold = 1", (p["id"],))
                out.append({
                    "product_id": p["id"],
                    "product_name": p["name"],
                    "available": a["c"], "sold": s["c"]})
            return JSONResponse(out)

        if action == "stock_add":
            pid = int(body.get("product_id", 0))
            items = body.get("items", [])
            if not pid or not items:
                return JSONResponse({"error": "invalid"},
                                    status_code=400)
            for item in items:
                await asyncio.to_thread(
                    db_execute,
                    "INSERT INTO stock_items (product_id, content) "
                    "VALUES (?, ?)", (pid, item))
            return JSONResponse({"ok": True, "added": len(items)})

        if action == "orders_list":
            rows = await asyncio.to_thread(
                db_fetchall,
                """SELECT o.*, p.name AS product_name, u.username
                   FROM orders o JOIN products p ON p.id = o.product_id
                   LEFT JOIN users u ON u.user_id = o.user_id
                   ORDER BY o.id DESC LIMIT 100""")
            return JSONResponse(rows)

        if action == "users_list":
            rows = await asyncio.to_thread(
                db_fetchall,
                "SELECT * FROM users ORDER BY created_at DESC LIMIT 200")
            return JSONResponse(rows)

        if action == "topups_list":
            rows = await asyncio.to_thread(
                db_fetchall,
                """SELECT r.*, u.username FROM topup_requests r
                   LEFT JOIN users u ON u.user_id = r.user_id
                   ORDER BY r.id DESC LIMIT 100""")
            return JSONResponse(rows)

        if action == "topup_approve":
            rid = int(body.get("id", 0))
            app = await get_tg_app()
            ok = await _approve_topup(rid, app.bot)
            return JSONResponse({"ok": ok})

        if action == "topup_reject":
            rid = int(body.get("id", 0))
            app = await get_tg_app()
            ok = await _reject_topup(rid, app.bot)
            return JSONResponse({"ok": ok})

        if action == "settings_get":
            return JSONResponse({
                "payment_note": await get_setting("payment_note", ""),
                "payment_method": await get_setting("payment_method", ""),
                "payment_qr": await get_qr()})

        if action == "settings_save":
            for k in ("payment_note", "payment_method"):
                if k in body:
                    await set_setting(k, body[k])
            return JSONResponse({"ok": True})

        if action == "qr_set":
            qtype = body.get("type")
            value = body.get("value", "")
            if qtype not in ("url", "b64", "file_id"):
                return JSONResponse({"error": "bad type"},
                                    status_code=400)
            await set_qr(qtype, value)
            return JSONResponse({"ok": True})

        if action == "broadcast":
            msg = (body.get("message") or "").strip()
            if not msg:
                return JSONResponse({"error": "empty"},
                                    status_code=400)
            users = await asyncio.to_thread(
                db_fetchall, "SELECT user_id FROM users")
            app = await get_tg_app()
            sent = failed = 0
            for u in users:
                try:
                    await app.bot.send_message(
                        chat_id=u["user_id"], text=msg)
                    sent += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(0.05)
            return JSONResponse({"sent": sent, "failed": failed})

        return JSONResponse({"error": "unknown action"},
                            status_code=400)

    # ═════════════════════════════════════════════
    # Auth
    # ═════════════════════════════════════════════
    def check_auth(request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            u, p = decoded.split(":", 1)
            return u == ADMIN_PANEL_USER and p == ADMIN_PANEL_PASS
        except Exception:
            return False

    def auth_challenge():
        return PlainTextResponse(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Shop Admin"'})

    # ═════════════════════════════════════════════
    # Routes
    # ═════════════════════════════════════════════
    async def debug_handler(request):
        try:
            import telegram
            ptb = getattr(telegram, "__version__", "unknown")
        except Exception as e:
            ptb = f"ERROR: {e}"
        try:
            import starlette
            sll = getattr(starlette, "__version__", "unknown")
        except Exception as e:
            sll = f"ERROR: {e}"

        tg_status = "not_initialized"
        tg_error = None
        try:
            await get_tg_app()
            tg_status = "ok"
        except Exception as e:
            tg_status = "error"
            tg_error = str(e)

        return JSONResponse({
            "status": "ok",
            "python": sys.version.split()[0],
            "ptb_version": ptb,
            "starlette_version": sll,
            "boot_errors": _boot_errors,
            "bot_token_set": bool(TOKEN),
            "admin_ids": ADMIN_IDS,
            "admin_panel_pass_customized":
                ADMIN_PANEL_PASS != "changeme",
            "db_exists": os.path.exists(DB_PATH),
            "db_ready": _db_ready,
            "tg_app_status": tg_status,
            "tg_app_error": tg_error,
        })

    async def webhook_entry(request):
        if request.query_params.get("action") == "debug":
            return await debug_handler(request)

        try:
            await ensure_db()
        except Exception as e:
            logger.exception("DB init failed")
            return PlainTextResponse(
                f"DB init failed: {e}", status_code=500)

        if request.method == "GET":
            if not check_auth(request):
                return auth_challenge()
            return HTMLResponse(ADMIN_HTML)

        try:
            body = await request.json()
        except Exception:
            return PlainTextResponse("Bad request", status_code=400)

        # Telegram update — MUST be processed before auth check
        if "update_id" in body:
            try:
                app = await get_tg_app()
                update = Update.de_json(body, app.bot)
                await app.process_update(update)
                logger.info("Processed update %s", body.get("update_id"))
            except Exception as e:
                logger.exception("Telegram update failed: %s", e)
                # Return 200 anyway so Telegram doesn't retry forever
            return PlainTextResponse("OK")

        if not check_auth(request):
            return auth_challenge()

        action = body.get("action", "")
        try:
            return await dispatch_admin(action, body)
        except Exception as e:
            logger.exception("Admin action failed: %s", action)
            return JSONResponse({"error": str(e), "action": action},
                                status_code=500)

    return Starlette(routes=[
        Route("/", endpoint=webhook_entry, methods=["GET", "POST"]),
        Route("/admin", endpoint=webhook_entry, methods=["GET"]),
        Route("/api/webhook", endpoint=webhook_entry,
              methods=["GET", "POST"]),
        Route("/debug", endpoint=debug_handler, methods=["GET"]),
    ])


ADMIN_HTML = open(
    os.path.join(os.path.dirname(__file__), "admin.html")
).read() if os.path.exists(
    os.path.join(os.path.dirname(__file__), "admin.html")
) else "<h1>admin.html missing</h1>"


try:
    app = build_app() if BOOT_OK else _fallback_asgi
    logger.info("App ready: %s", type(app).__name__)
except Exception as e:
    logger.exception("build_app failed: %s", e)
    _boot_errors["build"] = repr(e)
    app = _fallback_asgi
