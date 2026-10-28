import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "8807189325:AAGDQxllEFXBz4deVtKyBiHwb0quAxTSJhE")
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "8430063155").split(",")
    if x.strip().isdigit()
]
DB_PATH = "/tmp/shop.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────
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
        is_active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS stock_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
        content TEXT NOT NULL, is_sold INTEGER DEFAULT 0, sold_to INTEGER,
        sold_at TEXT,
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
    last_id = cur.lastrowid
    conn.close()
    return last_id


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


# ─────────────────────────────────────────────────────────────
# Settings helpers
# ─────────────────────────────────────────────────────────────
async def get_setting(key, default=None):
    row = await asyncio.to_thread(
        db_fetchone, "SELECT value FROM settings WHERE key = ?", (key,)
    )
    return row["value"] if row else default


async def set_setting(key, value):
    await asyncio.to_thread(
        db_execute,
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def is_admin(user_id):
    return user_id in ADMIN_IDS


async def ensure_user(update):
    u = update.effective_user
    if not u:
        return
    await asyncio.to_thread(
        db_execute,
        """INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username = excluded.username, first_name = excluded.first_name""",
        (u.id, u.username, u.first_name),
    )


async def get_user(user_id):
    return await asyncio.to_thread(
        db_fetchone, "SELECT * FROM users WHERE user_id = ?", (user_id,)
    )


def main_menu_kb(user_id):
    kb = [
        [InlineKeyboardButton("🛍 Shop", callback_data="menu_shop")],
        [InlineKeyboardButton("💰 Balance", callback_data="menu_balance")],
        [InlineKeyboardButton("📦 My Orders", callback_data="menu_orders")],
        [InlineKeyboardButton("🆘 Support", callback_data="menu_support")],
    ]
    if is_admin(user_id):
        kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_main")])
    return InlineKeyboardMarkup(kb)


def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Products", callback_data="admin_products")],
        [InlineKeyboardButton("📊 Stock", callback_data="admin_stock")],
        [InlineKeyboardButton("🧾 Orders", callback_data="admin_orders")],
        [InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("🎫 Tickets", callback_data="admin_tickets")],
        [InlineKeyboardButton("💳 Top‑ups", callback_data="admin_topups")],
        [InlineKeyboardButton("⚙️ Payment Settings", callback_data="admin_payment")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📈 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
    ])


def products_list_kb(products):
    kb = [
        [InlineKeyboardButton(
            f"{p['name']} – ${p['price']:.2f}", callback_data=f"buy_{p['id']}")]
        for p in products
    ]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(kb)


# ─────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await update.message.reply_text(
        f"👋 Welcome, <b>{update.effective_user.first_name}</b>!\n\n"
        "Use the menu below to shop, check your balance, view orders, or contact support.",
        reply_markup=main_menu_kb(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    products = await asyncio.to_thread(
        db_fetchall, "SELECT * FROM products WHERE is_active = 1 ORDER BY id")
    if not products:
        await update.message.reply_text("🛒 Shop is empty right now.")
        return
    await update.message.reply_text(
        "🛍 <b>Available products:</b>",
        reply_markup=products_list_kb(products),
        parse_mode=ParseMode.HTML,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    user = await get_user(update.effective_user.id)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Top Up", callback_data="topup_start")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_main")],
    ])
    await update.message.reply_text(
        f"💰 <b>Your balance:</b> <code>${user['balance']:.2f}</code>\n\n"
        "Tap below to top up.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    orders = await asyncio.to_thread(
        db_fetchall,
        """SELECT o.*, p.name AS product_name FROM orders o
           JOIN products p ON p.id = o.product_id
           WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20""",
        (update.effective_user.id,),
    )
    if not orders:
        await update.message.reply_text("📭 You have no orders yet.")
        return
    lines = ["🧾 <b>Your recent orders:</b>\n"]
    for o in orders:
        lines.append(
            f"• <b>{o['product_name']}</b> – ${o['amount']:.2f} "
            f"({o['status']}) – {o['created_at'][:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    context.user_data["support_state"] = "awaiting_subject"
    await update.message.reply_text(
        "🆘 <b>Support</b>\n\nDescribe your issue in one message.",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────
# Callback handler
# ─────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    await ensure_user(update)
    user = await get_user(uid)

    # ── Main menu ────────────────────────────────────────────
    if data == "menu_main":
        for k in ("admin_state", "support_state", "topup_state", "topup_request_id"):
            context.user_data.pop(k, None)
        await q.edit_message_text(
            "🏠 <b>Main menu</b>",
            reply_markup=main_menu_kb(uid),
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Shop ─────────────────────────────────────────────────
    if data == "menu_shop":
        products = await asyncio.to_thread(
            db_fetchall, "SELECT * FROM products WHERE is_active = 1 ORDER BY id")
        if not products:
            await q.edit_message_text(
                "🛒 Shop is empty right now.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="menu_main")]]))
            return
        await q.edit_message_text(
            "🛍 <b>Available products:</b>",
            reply_markup=products_list_kb(products),
            parse_mode=ParseMode.HTML,
        )
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
            "SELECT COUNT(*) AS cnt FROM stock_items WHERE product_id = ? AND is_sold = 0",
            (pid,))
        if stock["cnt"] == 0:
            await q.edit_message_text(
                "❌ Out of stock.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="menu_shop")]]))
            return
        if user["balance"] < product["price"]:
            await q.edit_message_text(
                f"❌ Insufficient balance.\n\n"
                f"Price: <b>${product['price']:.2f}</b>\n"
                f"Balance: <b>${user['balance']:.2f}</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Top Up", callback_data="topup_start")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_shop")]]),
                parse_mode=ParseMode.HTML)
            return
        item = await asyncio.to_thread(
            db_fetchone,
            "SELECT * FROM stock_items WHERE product_id = ? AND is_sold = 0 LIMIT 1",
            (pid,))
        if not item:
            await q.edit_message_text("❌ Stock gone. Try again.")
            return
        await asyncio.to_thread(
            db_execute, "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (product["price"], uid))
        await asyncio.to_thread(
            db_execute,
            "UPDATE stock_items SET is_sold = 1, sold_to = ?, sold_at = ? WHERE id = ?",
            (uid, datetime.utcnow().isoformat(), item["id"]))
        await asyncio.to_thread(
            db_execute,
            """INSERT INTO orders (user_id, product_id, stock_item_id, amount, status)
               VALUES (?, ?, ?, ?, 'completed')""",
            (uid, pid, item["id"], product["price"]))
        await q.edit_message_text(
            f"✅ <b>Purchase successful!</b>\n\n"
            f"Product: <b>{product['name']}</b>\n"
            f"Price: <b>${product['price']:.2f}</b>\n\n"
            f"<b>Your item:</b>\n<code>{item['content']}</code>",
            parse_mode=ParseMode.HTML)
        return

    # ── Balance ──────────────────────────────────────────────
    if data == "menu_balance":
        await q.edit_message_text(
            f"💰 <b>Your balance:</b> <code>${user['balance']:.2f}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Top Up", callback_data="topup_start")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_main")]]),
            parse_mode=ParseMode.HTML)
        return

    # ── Top-up flow start ───────────────────────────────────
    if data == "topup_start":
        qr = await get_setting("payment_qr_file_id")
        if not qr:
            await q.edit_message_text(
                "⚠️ Top‑up is temporarily unavailable.\nPlease contact support.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="menu_main")]]))
            return
        context.user_data["topup_state"] = "awaiting_amount"
        await q.edit_message_text(
            "💳 <b>Top Up</b>\n\n"
            "Send the amount you want to add (e.g. <code>10</code>).\n"
            "Minimum: <b>$1</b>",
            parse_mode=ParseMode.HTML)
        return

    # ── Orders ───────────────────────────────────────────────
    if data == "menu_orders":
        orders = await asyncio.to_thread(
            db_fetchall,
            """SELECT o.*, p.name AS product_name FROM orders o
               JOIN products p ON p.id = o.product_id
               WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20""",
            (uid,))
        if not orders:
            await q.edit_message_text(
                "📭 No orders yet.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="menu_main")]]))
            return
        lines = ["🧾 <b>Your recent orders:</b>\n"]
        for o in orders:
            lines.append(
                f"• <b>{o['product_name']}</b> – ${o['amount']:.2f} "
                f"({o['status']}) – {o['created_at'][:10]}")
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="menu_main")]]))
        return

    # ── Support ──────────────────────────────────────────────
    if data == "menu_support":
        context.user_data["support_state"] = "awaiting_subject"
        await q.edit_message_text(
            "🆘 <b>Support</b>\n\nDescribe your issue in one message.",
            parse_mode=ParseMode.HTML)
        return

    # ═════════════════════════════════════════════════════════
    # ADMIN PANEL
    # ═════════════════════════════════════════════════════════
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
        lines = ["📦 <b>Products</b>\n"] or ["📦 <b>Products</b>\n"]
        if not products:
            lines.append("No products yet.")
        for p in products:
            lines.append(
                f"• #{p['id']} <b>{p['name']}</b> – ${p['price']:.2f} "
                f"({'active' if p['is_active'] else 'inactive'})")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Product", callback_data="admin_addproduct")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]),
            parse_mode=ParseMode.HTML)
        return

    if data == "admin_addproduct":
        context.user_data["admin_state"] = "add_product_name"
        await q.edit_message_text("📝 Send the product name.")
        return

    if data == "admin_stock":
        products = await asyncio.to_thread(
            db_fetchall, "SELECT * FROM products ORDER BY id")
        lines = ["📊 <b>Stock overview</b>\n"]
        for p in products:
            cnt = await asyncio.to_thread(
                db_fetchone,
                "SELECT COUNT(*) AS c FROM stock_items WHERE product_id = ? AND is_sold = 0",
                (p["id"],))
            lines.append(f"• <b>{p['name']}</b> – {cnt['c']} in stock")
        if not products:
            lines.append("No products yet.")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Stock", callback_data="admin_addstock")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]),
            parse_mode=ParseMode.HTML)
        return

    if data == "admin_addstock":
        products = await asyncio.to_thread(
            db_fetchall, "SELECT * FROM products ORDER BY id")
        if not products:
            await q.edit_message_text("Add a product first.")
            return
        context.user_data["admin_state"] = "add_stock_product"
        lines = ["Send the <b>product ID</b> to add stock to:\n"]
        for p in products:
            lines.append(f"#{p['id']} – {p['name']}")
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)
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
                f"• #{o['id']} {o['product_name']} – ${o['amount']:.2f} "
                f"by @{o['username'] or o['user_id']} ({o['status']})")
        await q.edit_message_text(
            "\n".join(lines) or "No orders.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]))
        return

    if data == "admin_users":
        users = await asyncio.to_thread(
            db_fetchall, "SELECT * FROM users ORDER BY created_at DESC LIMIT 30")
        lines = ["👥 <b>Users</b>\n"]
        for u in users:
            lines.append(
                f"• <code>{u['user_id']}</code> @{u['username'] or '—'} "
                f"– ${u['balance']:.2f}")
        await q.edit_message_text(
            "\n".join(lines) or "No users.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]))
        return

    if data == "admin_tickets":
        tickets = await asyncio.to_thread(
            db_fetchall,
            """SELECT t.*, u.username FROM tickets t
               LEFT JOIN users u ON u.user_id = t.user_id
               WHERE t.status = 'open' ORDER BY t.id DESC LIMIT 20""")
        lines = ["🎫 <b>Open tickets</b>\n"]
        for t in tickets:
            lines.append(
                f"• #{t['id']} @{t['username'] or t['user_id']}: "
                f"{(t['subject'] or '')[:60]}")
        await q.edit_message_text(
            "\n".join(lines) or "No open tickets.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]))
        return

    # ── Top-up requests with receipts ───────────────────────
    if data == "admin_topups":
        reqs = await asyncio.to_thread(
            db_fetchall,
            """SELECT r.*, u.username FROM topup_requests r
               LEFT JOIN users u ON u.user_id = r.user_id
               WHERE r.status = 'pending' ORDER BY r.id DESC LIMIT 20""")
        if not reqs:
            await q.edit_message_text(
                "✅ No pending top‑ups.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]))
            return
        kb = []
        for r in reqs:
            kb.append([InlineKeyboardButton(
                f"👁 #{r['id']} @{r['username'] or r['user_id']} – ${r['amount']:.2f}",
                callback_data=f"tu_view_{r['id']}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_main")])
        await q.edit_message_text(
            "💳 <b>Pending top‑ups with receipts</b>\n"
            "Tap one to review the receipt and approve/reject.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML)
        return

    if data.startswith("tu_view_"):
        req_id = int(data.split("_")[2])
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (req_id,))
        if not req:
            await q.edit_message_text("Not found.")
            return
        u = await get_user(req["user_id"])
        caption = (
            f"💳 <b>Top‑up Request #{req_id}</b>\n\n"
            f"<b>User:</b> @{u['username'] or 'N/A'} (<code>{req['user_id']}</code>)\n"
            f"<b>Amount:</b> <code>${req['amount']:.2f}</code>\n"
            f"<b>Reference:</b> <code>{req['reference'] or '—'}</code>\n"
            f"<b>Status:</b> {req['status']}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"tu_ok_{req_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"tu_no_{req_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_topups")],
        ])
        if req["receipt_file_id"]:
            if req["receipt_type"] == "photo":
                await q.message.reply_photo(
                    photo=req["receipt_file_id"], caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await q.message.reply_document(
                    document=req["receipt_file_id"], caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await q.message.reply_text(
                caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("tu_ok_"):
        req_id = int(data.split("_")[2])
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (req_id,))
        if not req or req["status"] != "pending":
            try:
                await q.edit_message_caption(caption="⚠️ Already processed.")
            except Exception:
                await q.edit_message_text("⚠️ Already processed.")
            return
        await asyncio.to_thread(
            db_execute, "UPDATE topup_requests SET status = 'approved' WHERE id = ?",
            (req_id,))
        await asyncio.to_thread(
            db_execute, "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (req["amount"], req["user_id"]))
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"✅ <b>Top‑up approved!</b>\n\n"
                     f"<b>${req['amount']:.2f}</b> has been added to your balance.\n"
                     f"Request: <code>#{req_id}</code>",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        try:
            await q.edit_message_caption(
                caption=f"✅ Approved #{req_id} — ${req['amount']:.2f} credited.")
        except Exception:
            await q.edit_message_text(
                f"✅ Approved #{req_id} — ${req['amount']:.2f} credited.")
        return

    if data.startswith("tu_no_"):
        req_id = int(data.split("_")[2])
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (req_id,))
        if not req:
            return
        await asyncio.to_thread(
            db_execute, "UPDATE topup_requests SET status = 'rejected' WHERE id = ?",
            (req_id,))
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"❌ <b>Top‑up rejected</b>\n\n"
                     f"Request <code>#{req_id}</code> was rejected.\n"
                     f"If you think this is a mistake, contact support.",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        try:
            await q.edit_message_caption(caption=f"❌ Rejected #{req_id}.")
        except Exception:
            await q.edit_message_text(f"❌ Rejected #{req_id}.")
        return

    # ── Payment settings ────────────────────────────────────
    if data == "admin_payment":
        qr = await get_setting("payment_qr_file_id")
        note = await get_setting("payment_note") or "(not set)"
        method = await get_setting("payment_method") or "(not set)"
        text = (
            f"⚙️ <b>Payment Settings</b>\n\n"
            f"<b>Method:</b> {method}\n"
            f"<b>Note:</b> {note}\n"
            f"<b>QR Code:</b> {'✅ Set' if qr else '❌ Not set'}"
        )
        await q.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼 Set QR Code", callback_data="admin_setqr")],
                [InlineKeyboardButton("📝 Set Note", callback_data="admin_setnote")],
                [InlineKeyboardButton("🏦 Set Method", callback_data="admin_setmethod")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_main")],
            ]),
            parse_mode=ParseMode.HTML)
        return

    if data == "admin_setqr":
        context.user_data["admin_state"] = "set_payment_qr"
        await q.edit_message_text("📸 Send the QR code image now.")
        return

    if data == "admin_setnote":
        context.user_data["admin_state"] = "set_payment_note"
        await q.edit_message_text(
            "📝 Send the payment note (bank details, instructions, etc.).")
        return

    if data == "admin_setmethod":
        context.user_data["admin_state"] = "set_payment_method"
        await q.edit_message_text(
            "🏦 Send the payment method name (e.g. ABA Bank, KBZ Pay, USDT).")
        return

    # ── Broadcast & stats ───────────────────────────────────
    if data == "admin_broadcast":
        context.user_data["admin_state"] = "broadcast"
        await q.edit_message_text(
            "📢 Send the message to broadcast to all users.")
        return

    if data == "admin_stats":
        u = await asyncio.to_thread(db_fetchone, "SELECT COUNT(*) AS c FROM users")
        p = await asyncio.to_thread(db_fetchone, "SELECT COUNT(*) AS c FROM products")
        o = await asyncio.to_thread(db_fetchone, "SELECT COUNT(*) AS c FROM orders")
        await q.edit_message_text(
            f"📈 <b>Stats</b>\n\n"
            f"👥 Users: <b>{u['c']}</b>\n"
            f"📦 Products: <b>{p['c']}</b>\n"
            f"🧾 Orders: <b>{o['c']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="admin_main")]]))
        return


# ─────────────────────────────────────────────────────────────
# Photo / document handler (receipts + QR setup)
# ─────────────────────────────────────────────────────────────
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    uid = update.effective_user.id
    msg = update.message

    # ── Admin setting payment QR ────────────────────────────
    if is_admin(uid) and context.user_data.get("admin_state") == "set_payment_qr":
        file_id = msg.photo[-1].file_id
        await set_setting("payment_qr_file_id", file_id)
        context.user_data.pop("admin_state", None)
        await msg.reply_text("✅ Payment QR code saved successfully!")
        return

    # ── User uploading receipt ──────────────────────────────
    if context.user_data.get("topup_state") == "awaiting_receipt":
        req_id = context.user_data.get("topup_request_id")
        if not req_id:
            return
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (req_id,))
        if not req or req["status"] != "awaiting_payment":
            await msg.reply_text("⚠️ This request is no longer active.")
            context.user_data.pop("topup_state", None)
            context.user_data.pop("topup_request_id", None)
            return

        file_id = msg.photo[-1].file_id
        await asyncio.to_thread(
            db_execute,
            "UPDATE topup_requests SET receipt_file_id = ?, receipt_type = 'photo', "
            "status = 'pending' WHERE id = ?",
            (file_id, req_id))
        context.user_data.pop("topup_state", None)
        context.user_data.pop("topup_request_id", None)

        await msg.reply_text(
            f"✅ Receipt received for <b>#{req_id}</b>.\n"
            f"An admin will verify and credit your balance shortly.",
            parse_mode=ParseMode.HTML)

        user = await get_user(uid)
        caption = (
            f"💳 <b>New Top‑up Request</b>\n\n"
            f"<b>Request:</b> #{req_id}\n"
            f"<b>User:</b> @{user['username'] or 'N/A'} (<code>{uid}</code>)\n"
            f"<b>Amount:</b> <code>${req['amount']:.2f}</code>\n"
            f"<b>Reference:</b> <code>{req['reference'] or '—'}</code>")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"tu_ok_{req_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"tu_no_{req_id}"),
        ]])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=admin_id, photo=file_id, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        return


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    uid = update.effective_user.id
    msg = update.message

    if context.user_data.get("topup_state") == "awaiting_receipt":
        req_id = context.user_data.get("topup_request_id")
        if not req_id:
            return
        req = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM topup_requests WHERE id = ?", (req_id,))
        if not req or req["status"] != "awaiting_payment":
            await msg.reply_text("⚠️ This request is no longer active.")
            return
        file_id = msg.document.file_id
        await asyncio.to_thread(
            db_execute,
            "UPDATE topup_requests SET receipt_file_id = ?, receipt_type = 'document', "
            "status = 'pending' WHERE id = ?",
            (file_id, req_id))
        context.user_data.pop("topup_state", None)
        context.user_data.pop("topup_request_id", None)
        await msg.reply_text(
            f"✅ Receipt received for <b>#{req_id}</b>.",
            parse_mode=ParseMode.HTML)
        user = await get_user(uid)
        caption = (
            f"💳 <b>New Top‑up Request</b>\n\n"
            f"<b>Request:</b> #{req_id}\n"
            f"<b>User:</b> @{user['username'] or 'N/A'} (<code>{uid}</code>)\n"
            f"<b>Amount:</b> <code>${req['amount']:.2f}</code>\n"
            f"<b>Reference:</b> <code>{req['reference'] or '—'}</code>")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"tu_ok_{req_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"tu_no_{req_id}"),
        ]])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_document(
                    chat_id=admin_id, document=file_id, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# Text message handler
# ─────────────────────────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    uid = update.effective_user.id
    user = await get_user(uid)
    text = update.message.text.strip()

    # ── Top-up amount → show QR + note ─────────────────────
    if context.user_data.get("topup_state") == "awaiting_amount":
        context.user_data.pop("topup_state", None)
        try:
            amount = float(text)
            if amount < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid amount. Send a number ≥ 1.")
            return

        qr = await get_setting("payment_qr_file_id")
        note = await get_setting("payment_note") or "Send the receipt after payment."
        method = await get_setting("payment_method") or "Payment"

        if not qr:
            await update.message.reply_text("⚠️ Payment not configured. Try later.")
            return

        ref = f"TP{int(datetime.utcnow().timestamp())}{uid % 10000}"
        req_id = await asyncio.to_thread(
            db_execute,
            """INSERT INTO topup_requests (user_id, amount, reference, status)
               VALUES (?, ?, ?, 'awaiting_payment')""",
            (uid, amount, ref))
        context.user_data["topup_state"] = "awaiting_receipt"
        context.user_data["topup_request_id"] = req_id

        caption = (
            f"💳 <b>Complete your payment</b>\n\n"
            f"<b>Amount:</b> <code>${amount:.2f}</code>\n"
            f"<b>Method:</b> {method}\n"
            f"<b>Reference:</b> <code>{ref}</code>\n\n"
            f"📝 <b>Note:</b>\n{note}\n\n"
            f"📸 After paying, <b>send your receipt here</b> "
            f"(photo or file)."
        )
        try:
            await update.message.reply_photo(
                photo=qr, caption=caption, parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML)
        return

    # ── Support ticket ──────────────────────────────────────
    if context.user_data.get("support_state") == "awaiting_subject":
        context.user_data.pop("support_state", None)
        ticket_id = await asyncio.to_thread(
            db_execute,
            "INSERT INTO tickets (user_id, subject) VALUES (?, ?)",
            (uid, text[:200]))
        await asyncio.to_thread(
            db_execute,
            """INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, message)
               VALUES (?, ?, 0, ?)""",
            (ticket_id, uid, text))
        await update.message.reply_text(
            f"✅ Ticket <b>#{ticket_id}</b> created.",
            parse_mode=ParseMode.HTML)
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🎫 New ticket #{ticket_id} from @{user['username'] or uid}:\n{text}")
            except Exception:
                pass
        return

    # ── Admin states ────────────────────────────────────────
    if not is_admin(uid):
        return
    state = context.user_data.get("admin_state")

    if state == "set_payment_note":
        await set_setting("payment_note", text)
        context.user_data.pop("admin_state", None)
        await update.message.reply_text("✅ Payment note saved.")
        return

    if state == "set_payment_method":
        await set_setting("payment_method", text)
        context.user_data.pop("admin_state", None)
        await update.message.reply_text("✅ Payment method saved.")
        return

    if state == "add_product_name":
        context.user_data["new_product_name"] = text
        context.user_data["admin_state"] = "add_product_price"
        await update.message.reply_text("Send the price (e.g. 9.99).")
        return

    if state == "add_product_price":
        try:
            price = float(text)
        except ValueError:
            await update.message.reply_text("❌ Invalid price.")
            return
        context.user_data["new_product_price"] = price
        context.user_data["admin_state"] = "add_product_desc"
        await update.message.reply_text("Send a description (or /skip).")
        return

    if state == "add_product_desc":
        name = context.user_data.pop("new_product_name")
        price = context.user_data.pop("new_product_price")
        desc = "" if text == "/skip" else text
        pid = await asyncio.to_thread(
            db_execute,
            "INSERT INTO products (name, description, price) VALUES (?, ?, ?)",
            (name, desc, price))
        context.user_data.pop("admin_state", None)
        await update.message.reply_text(
            f"✅ Product <b>#{pid}</b> added.", parse_mode=ParseMode.HTML)
        return

    if state == "add_stock_product":
        try:
            pid = int(text)
        except ValueError:
            await update.message.reply_text("❌ Send a numeric product ID.")
            return
        p = await asyncio.to_thread(
            db_fetchone, "SELECT * FROM products WHERE id = ?", (pid,))
        if not p:
            await update.message.reply_text("❌ Product not found.")
            return
        context.user_data["stock_product_id"] = pid
        context.user_data["admin_state"] = "add_stock_content"
        await update.message.reply_text(
            f"Send stock content for <b>{p['name']}</b> "
            f"(one item per line).", parse_mode=ParseMode.HTML)
        return

    if state == "add_stock_content":
        pid = context.user_data.pop("stock_product_id")
        context.user_data.pop("admin_state", None)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for line in lines:
            await asyncio.to_thread(
                db_execute,
                "INSERT INTO stock_items (product_id, content) VALUES (?, ?)",
                (pid, line))
        await update.message.reply_text(
            f"✅ Added {len(lines)} stock item(s) to product #{pid}.")
        return

    if state == "broadcast":
        context.user_data.pop("admin_state", None)
        users = await asyncio.to_thread(db_fetchall, "SELECT user_id FROM users")
        sent = failed = 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u["user_id"], text=text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await update.message.reply_text(
            f"📢 Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")
        return


# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))


async def set_bot_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("shop", "Browse products"),
        BotCommand("balance", "Check balance / top up"),
        BotCommand("orders", "My orders"),
        BotCommand("support", "Contact support"),
    ])


# ─────────────────────────────────────────────────────────────
# Vercel entrypoint
# ─────────────────────────────────────────────────────────────
_application = None
_lock = asyncio.Lock()


async def get_application():
    global _application
    if _application is None:
        async with _lock:
            if _application is None:
                app = Application.builder().token(TOKEN).build()
                register_handlers(app)
                await app.initialize()
                await set_bot_commands(app)
                _application = app
    return _application


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            update_data = json.loads(body)
            asyncio.run(self._process(update_data))
        except Exception as e:
            logger.exception("Error: %s", e)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    async def _process(self, update_data):
        app = await get_application()
        update = Update.de_json(update_data, app.bot)
        await app.process_update(update)


init_db()
