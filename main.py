!pip uninstall -y python-telegram-bot telegram -q
!pip install -U --force-reinstall "python-telegram-bot==22.8" nest_asyncio requests qrcode[pil] pillow -q
import nest_asyncio
nest_asyncio.apply()

import os
import json
import logging
import uuid
import requests
import asyncio
from datetime import datetime
from io import BytesIO

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ==================== CẤU HÌNH ====================
BOT_TOKEN = "8958149815:AAElJ97BtD3IOWiXMH0iyzm0SJQczOPWkzg"          # ← Thay token
ADMIN_ID = 8428411178                       # ← Thay Telegram ID của bạn

BANK_INFO = {
    "bank_id": "970422",                   # BIN Vietcombank (đổi theo ngân hàng)
    "bank_name": "MB BANK",
    "account_number": "0352836546",
    "account_name": "Phan Hoàng An",
    "template": "compact2"
}

PRODUCTS = {
    "key_1m": {
        "name": "Premium Key 1 Tháng",
        "price": 99000,
        "description": "Key Premium dùng trong 30 ngày",
        "stock": ["KEY-1M-AAAA-1111", "KEY-1M-BBBB-2222", "KEY-1M-CCCC-3333"]
    },
    "key_3m": {
        "name": "Premium Key 3 Tháng",
        "price": 249000,
        "description": "Key Premium dùng trong 90 ngày",
        "stock": ["KEY-3M-XXXX-4444", "KEY-3M-YYYY-5555"]
    },
    "key_lifetime": {
        "name": "Premium Key Vĩnh Viễn",
        "price": 499000,
        "description": "Key Premium không giới hạn thời gian",
        "stock": ["KEY-LT-ZZZZ-6666"]
    }
}

ORDERS_FILE = "orders.json"
USERS_FILE = "users.json"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== HELPER ====================
def load_json(filename: str, default=None):
    if default is None:
        default = {}
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filename: str, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_order_id() -> str:
    return f"ORD-{datetime.now().strftime('%y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

def create_vietqr_image(amount: int, content: str, order_id: str) -> BytesIO:
    transfer_content = f"{content} {order_id}".replace(" ", "%20")
    account_name = BANK_INFO["account_name"].replace(" ", "%20")
    url = (
        f"https://img.vietqr.io/image/"
        f"{BANK_INFO['bank_id']}-{BANK_INFO['account_number']}-{BANK_INFO['template']}.jpg"
        f"?amount={amount}&addInfo={transfer_content}&accountName={account_name}"
    )
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        bio = BytesIO(response.content)
        bio.name = f"qr_{order_id}.jpg"
        return bio
    except Exception as e:
        logger.error(f"Lỗi tạo VietQR: {e}")
        import qrcode
        from PIL import Image
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(f"{BANK_INFO['account_number']}|{amount}|{content} {order_id}")
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = BytesIO()
        img.save(bio, "PNG")
        bio.seek(0)
        bio.name = f"qr_{order_id}.png"
        return bio

# ==================== USER HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users = load_json(USERS_FILE, {})
    users[str(user.id)] = {
        "username": user.username,
        "full_name": user.full_name,
        "joined": datetime.now().isoformat()
    }
    save_json(USERS_FILE, users)

    text = (
        f"Xin chào <b>{user.first_name}</b>!\n\n"
        "Chào mừng đến <b>Key Store Pro</b>\n"
        "Mua key nhanh • Thanh toán VietQR • Duyệt thủ công bởi Admin"
    )
    keyboard = [
        [KeyboardButton("🛒 Mua Key"), KeyboardButton("📋 Đơn hàng của tôi")],
        [KeyboardButton("ℹ️ Hướng dẫn"), KeyboardButton("📞 Liên hệ Admin")]
    ]
    if user.id == ADMIN_ID:
        keyboard.append([KeyboardButton("🛠 Admin Panel")])

    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>🛒 Danh sách sản phẩm</b>\n\n"
    keyboard = []
    for pid, p in PRODUCTS.items():
        stock = len(p["stock"])
        status = f"✅ Còn {stock}" if stock > 0 else "❌ Hết hàng"
        text += f"• <b>{p['name']}</b>\n  💰 <code>{p['price']:,}đ</code> | {status}\n  {p['description']}\n\n"
        if stock > 0:
            keyboard.append([InlineKeyboardButton(
                f"Mua {p['name']} - {p['price']:,}đ", callback_data=f"buy_{pid}"
            )])
    if not keyboard:
        text += "⚠️ Hiện tại tất cả sản phẩm đều hết hàng."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.replace("buy_", "")
    if pid not in PRODUCTS or not PRODUCTS[pid]["stock"]:
        await query.edit_message_text("❌ Sản phẩm không tồn tại hoặc đã hết hàng.")
        return

    product = PRODUCTS[pid]
    order_id = generate_order_id()
    amount = product["price"]
    content = f"MUA{pid.upper()}"

    orders = load_json(ORDERS_FILE, {})
    orders[order_id] = {
        "user_id": query.from_user.id,
        "username": query.from_user.username,
        "full_name": query.from_user.full_name,
        "product_id": pid,
        "product_name": product["name"],
        "amount": amount,
        "status": "pending_payment",
        "created_at": datetime.now().isoformat(),
        "key": None
    }
    save_json(ORDERS_FILE, orders)

    qr_image = create_vietqr_image(amount, content, order_id)

    caption = (
        f"<b>📦 Đơn hàng: <code>{order_id}</code></b>\n\n"
        f"Sản phẩm: <b>{product['name']}</b>\n"
        f"Số tiền: <code>{amount:,} VNĐ</code>\n\n"
        f"<b>Thông tin chuyển khoản:</b>\n"
        f"• Ngân hàng: {BANK_INFO['bank_name']}\n"
        f"• Số TK: <code>{BANK_INFO['account_number']}</code>\n"
        f"• Chủ TK: {BANK_INFO['account_name']}\n"
        f"• Nội dung: <code>{content} {order_id}</code>\n\n"
        f"⚠️ <b>Vui lòng chuyển đúng nội dung</b> để được duyệt nhanh."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Tôi đã thanh toán", callback_data=f"paid_{order_id}")],
        [InlineKeyboardButton("❌ Hủy đơn", callback_data=f"cancel_{order_id}")]
    ]
    await query.message.reply_photo(
        photo=InputFile(qr_image), caption=caption,
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await query.edit_message_text(f"✅ Đã tạo đơn <code>{order_id}</code>. Vui lòng xem tin nhắn bên dưới.", parse_mode=ParseMode.HTML)

async def paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.replace("paid_", "")
    orders = load_json(ORDERS_FILE, {})

    if order_id not in orders or orders[order_id]["status"] != "pending_payment":
        await query.answer("Đơn hàng không hợp lệ hoặc đã xử lý.", show_alert=True)
        return

    order = orders[order_id]
    order["status"] = "waiting_admin"
    order["paid_at"] = datetime.now().isoformat()
    save_json(ORDERS_FILE, orders)

    admin_text = (
        f"🔔 <b>ĐƠN HÀNG MỚI CẦN DUYỆT</b>\n\n"
        f"Mã: <code>{order_id}</code>\n"
        f"Khách: {order['full_name']} (@{order.get('username') or 'N/A'})\n"
        f"ID: <code>{order['user_id']}</code>\n"
        f"Sản phẩm: <b>{order['product_name']}</b>\n"
        f"Số tiền: <code>{order['amount']:,}đ</code>\n"
        f"Thời gian: {order['paid_at']}"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Duyệt & Gửi Key", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Từ chối", callback_data=f"reject_{order_id}")
    ]]
    await context.bot.send_message(ADMIN_ID, admin_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    await query.edit_message_caption(
        caption=query.message.caption + "\n\n⏳ <b>Đã gửi cho Admin. Vui lòng chờ duyệt.</b>",
        parse_mode=ParseMode.HTML
    )

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.replace("cancel_", "")
    orders = load_json(ORDERS_FILE, {})
    if order_id in orders and orders[order_id]["status"] == "pending_payment":
        orders[order_id]["status"] = "cancelled"
        save_json(ORDERS_FILE, orders)
        await query.edit_message_caption(caption=f"🚫 Đã hủy đơn <code>{order_id}</code>.", parse_mode=ParseMode.HTML)
    else:
        await query.answer("Không thể hủy đơn này.", show_alert=True)

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    orders = load_json(ORDERS_FILE, {})
    user_orders = [(oid, o) for oid, o in orders.items() if o["user_id"] == user_id]
    if not user_orders:
        await update.message.reply_text("Bạn chưa có đơn hàng nào.")
        return
    text = "<b>📋 Đơn hàng của bạn</b>\n\n"
    status_map = {
        "pending_payment": "⏳ Chờ thanh toán",
        "waiting_admin": "🕐 Chờ Admin duyệt",
        "completed": "✅ Hoàn thành",
        "rejected": "❌ Từ chối",
        "cancelled": "🚫 Đã hủy"
    }
    for oid, o in sorted(user_orders, key=lambda x: x[1]["created_at"], reverse=True)[:8]:
        text += f"• <code>{oid}</code> | {o['product_name']}\n  {status_map.get(o['status'], o['status'])} | {o['amount']:,}đ\n"
        if o.get("key"):
            text += f"  🔑 <code>{o['key']}</code>\n"
        text += "\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📖 Hướng dẫn mua hàng</b>\n\n"
        "1. Nhấn <b>🛒 Mua Key</b>\n"
        "2. Chọn sản phẩm\n"
        "3. Quét mã VietQR hoặc chuyển khoản đúng nội dung\n"
        "4. Nhấn <b>Tôi đã thanh toán</b>\n"
        "5. Chờ Admin duyệt (thường 1-5 phút)\n"
        "6. Nhận key ngay trong chat\n\n"
        "⚠️ Phải chuyển <b>đúng nội dung</b> để được duyệt nhanh nhất."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📞 Liên hệ Admin: tg://user?id={ADMIN_ID}")

# ==================== ADMIN PANEL ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = "<b>🛠 ADMIN PANEL</b>\n\nChọn chức năng bên dưới:"
    keyboard = [
        [InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"),
         InlineKeyboardButton("📦 Tồn kho", callback_data="admin_stock")],
        [InlineKeyboardButton("🕐 Đơn chờ duyệt", callback_data="admin_pending"),
         InlineKeyboardButton("📋 Tất cả đơn", callback_data="admin_all_orders")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_info"),
         InlineKeyboardButton("➕ Thêm Key", callback_data="admin_addkey_info")]
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("Bạn không có quyền!", show_alert=True)
        return

    data = query.data
    if data == "admin_stats":
        orders = load_json(ORDERS_FILE, {})
        users = load_json(USERS_FILE, {})
        completed = [o for o in orders.values() if o["status"] == "completed"]
        revenue = sum(o["amount"] for o in completed)
        pending = sum(1 for o in orders.values() if o["status"] == "waiting_admin")
        text = (
            f"<b>📊 THỐNG KÊ</b>\n\n"
            f"👥 Tổng user: <b>{len(users)}</b>\n"
            f"📦 Tổng đơn: <b>{len(orders)}</b>\n"
            f"✅ Hoàn thành: <b>{len(completed)}</b>\n"
            f"🕐 Chờ duyệt: <b>{pending}</b>\n"
            f"💰 Doanh thu: <b>{revenue:,} VNĐ</b>"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)

    elif data == "admin_stock":
        text = "<b>📦 TỒN KHO KEY</b>\n\n"
        for pid, p in PRODUCTS.items():
            text += f"• <b>{p['name']}</b>\n  Còn lại: <code>{len(p['stock'])}</code> key\n\n"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)

    elif data == "admin_pending":
        orders = load_json(ORDERS_FILE, {})
        pending = [(oid, o) for oid, o in orders.items() if o["status"] == "waiting_admin"]
        if not pending:
            await query.edit_message_text("✅ Không có đơn nào đang chờ duyệt.")
            return
        text = "<b>🕐 ĐƠN ĐANG CHỜ DUYỆT</b>\n\n"
        keyboard = []
        for oid, o in pending[:10]:
            text += f"• <code>{oid}</code> | {o['product_name']} | {o['amount']:,}đ\n  {o['full_name']}\n\n"
            keyboard.append([
                InlineKeyboardButton(f"✅ {oid[-6:]}", callback_data=f"approve_{oid}"),
                InlineKeyboardButton(f"❌ {oid[-6:]}", callback_data=f"reject_{oid}")
            ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "admin_broadcast_info":
        await query.edit_message_text(
            "📢 <b>Broadcast</b>\n\nGửi lệnh:\n<code>/broadcast Nội dung thông báo của bạn</code>",
            parse_mode=ParseMode.HTML
        )

    elif data == "admin_addkey_info":
        await query.edit_message_text(
            "➕ <b>Thêm Key</b>\n\n"
            "Cú pháp:\n<code>/addkey product_id key1 key2 key3...</code>\n\n"
            "Ví dụ:\n<code>/addkey key_1m KEY-ABC-123 KEY-DEF-456</code>\n\n"
            "Product ID hiện có:\n" + "\n".join([f"• <code>{k}</code>" for k in PRODUCTS.keys()]),
            parse_mode=ParseMode.HTML
        )

    elif data == "admin_all_orders":
        orders = load_json(ORDERS_FILE, {})
        text = "<b>📋 10 ĐƠN GẦN NHẤT</b>\n\n"
        for oid, o in sorted(orders.items(), key=lambda x: x[1]["created_at"], reverse=True)[:10]:
            text += f"• <code>{oid}</code> | {o['status']} | {o['amount']:,}đ\n"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)

async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    order_id = query.data.replace("approve_", "")
    orders = load_json(ORDERS_FILE, {})
    if order_id not in orders or orders[order_id]["status"] != "waiting_admin":
        await query.answer("Đơn không hợp lệ hoặc đã xử lý.", show_alert=True)
        return
    order = orders[order_id]
    pid = order["product_id"]
    if not PRODUCTS[pid]["stock"]:
        await query.edit_message_text("❌ Hết key. Không thể duyệt.")
        return
    key = PRODUCTS[pid]["stock"].pop(0)
    order["status"] = "completed"
    order["key"] = key
    order["approved_at"] = datetime.now().isoformat()
    save_json(ORDERS_FILE, orders)
    try:
        await context.bot.send_message(
            order["user_id"],
            f"🎉 <b>Đơn hàng {order_id} đã được duyệt!</b>\n\n"
            f"Sản phẩm: <b>{order['product_name']}</b>\n"
            f"🔑 Key của bạn:\n<code>{key}</code>\n\nCảm ơn bạn đã mua hàng!",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(e)
    await query.edit_message_text(
        f"✅ Đã duyệt <code>{order_id}</code>\nKey: <code>{key}</code>\nĐã gửi cho khách.",
        parse_mode=ParseMode.HTML
    )

async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    order_id = query.data.replace("reject_", "")
    orders = load_json(ORDERS_FILE, {})
    if order_id not in orders:
        return
    order = orders[order_id]
    order["status"] = "rejected"
    order["rejected_at"] = datetime.now().isoformat()
    save_json(ORDERS_FILE, orders)
    try:
        await context.bot.send_message(
            order["user_id"],
            f"❌ Đơn hàng <code>{order_id}</code> đã bị từ chối.\n"
            f"Lý do: Không tìm thấy giao dịch / sai nội dung.\n"
            f"Vui lòng liên hệ Admin nếu bạn đã chuyển khoản.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    await query.edit_message_text(f"❌ Đã từ chối đơn <code>{order_id}</code>.", parse_mode=ParseMode.HTML)

# ==================== ADMIN COMMANDS ====================
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Cú pháp: /broadcast <nội dung>")
        return
    message = " ".join(context.args)
    users = load_json(USERS_FILE, {})
    success = fail = 0
    for uid in users:
        try:
            await context.bot.send_message(int(uid), f"📢 <b>Thông báo từ Admin</b>\n\n{message}", parse_mode=ParseMode.HTML)
            success += 1
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ Gửi xong\nThành công: {success}\nThất bại: {fail}")

async def admin_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Cú pháp: /addkey <product_id> <key1> <key2> ...")
        return
    pid = context.args[0]
    if pid not in PRODUCTS:
        await update.message.reply_text("❌ Product ID không tồn tại.")
        return
    keys = context.args[1:]
    PRODUCTS[pid]["stock"].extend(keys)
    await update.message.reply_text(
        f"✅ Đã thêm {len(keys)} key vào <b>{PRODUCTS[pid]['name']}</b>\nTồn kho hiện tại: {len(PRODUCTS[pid]['stock'])}",
        parse_mode=ParseMode.HTML
    )

async def admin_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    orders = load_json(ORDERS_FILE, {})
    users = load_json(USERS_FILE, {})
    completed = [o for o in orders.values() if o["status"] == "completed"]
    revenue = sum(o["amount"] for o in completed)
    text = (
        f"<b>📊 THỐNG KÊ</b>\n\n"
        f"👥 User: {len(users)}\n"
        f"📦 Đơn: {len(orders)}\n"
        f"✅ Hoàn thành: {len(completed)}\n"
        f"💰 Doanh thu: {revenue:,} VNĐ"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ==================== QUẢN LÝ SẢN PHẨM ====================
async def admin_listprod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = "<b>📦 DANH SÁCH SẢN PHẨM</b>\n\n"
    for pid, p in PRODUCTS.items():
        text += (
            f"• <b>ID:</b> <code>{pid}</code>\n"
            f"  Tên: {p['name']}\n"
            f"  Giá: <code>{p['price']:,}đ</code>\n"
            f"  Tồn kho: {len(p['stock'])} key\n"
            f"  Mô tả: {p['description']}\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def admin_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Cú pháp: /setprice <product_id> <giá mới>\nVí dụ: /setprice key_1m 89000")
        return
    pid = context.args[0]
    if pid not in PRODUCTS:
        await update.message.reply_text("❌ Product ID không tồn tại.")
        return
    try:
        new_price = int(context.args[1])
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Giá phải là số nguyên dương.")
        return
    old_price = PRODUCTS[pid]["price"]
    PRODUCTS[pid]["price"] = new_price
    await update.message.reply_text(
        f"✅ Đã đổi giá <b>{PRODUCTS[pid]['name']}</b>\nTừ <code>{old_price:,}đ</code> → <code>{new_price:,}đ</code>",
        parse_mode=ParseMode.HTML
    )

async def admin_editprod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Cú pháp: /editprod <product_id> <field> <giá trị mới>\n\n"
            "Field hỗ trợ:\n• name\n• description\n• price\n\n"
            "Ví dụ:\n<code>/editprod key_1m name Premium Key 30 Ngày</code>\n"
            "<code>/editprod key_1m price 79000</code>",
            parse_mode=ParseMode.HTML
        )
        return
    pid = context.args[0]
    field = context.args[1].lower()
    value = " ".join(context.args[2:])
    if pid not in PRODUCTS:
        await update.message.reply_text("❌ Product ID không tồn tại.")
        return
    if field == "name":
        PRODUCTS[pid]["name"] = value
        await update.message.reply_text(f"✅ Đã đổi tên thành: <b>{value}</b>", parse_mode=ParseMode.HTML)
    elif field == "description":
        PRODUCTS[pid]["description"] = value
        await update.message.reply_text(f"✅ Đã đổi mô tả thành: {value}")
    elif field == "price":
        try:
            new_price = int(value)
            PRODUCTS[pid]["price"] = new_price
            await update.message.reply_text(f"✅ Đã đổi giá thành: <code>{new_price:,}đ</code>", parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("❌ Giá phải là số.")
    else:
        await update.message.reply_text("❌ Field không hợp lệ. Chỉ hỗ trợ: name, description, price")

async def admin_addprod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text("Cú pháp: /addprod <product_id> <giá> <tên sản phẩm>\nVí dụ: /addprod key_6m 399000 Premium Key 6 Tháng")
        return
    pid = context.args[0]
    if pid in PRODUCTS:
        await update.message.reply_text("❌ Product ID này đã tồn tại.")
        return
    try:
        price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Giá phải là số.")
        return
    name = " ".join(context.args[2:])
    PRODUCTS[pid] = {
        "name": name,
        "price": price,
        "description": name,
        "stock": []
    }
    await update.message.reply_text(
        f"✅ Đã thêm sản phẩm mới:\n• ID: <code>{pid}</code>\n• Tên: <b>{name}</b>\n• Giá: <code>{price:,}đ</code>",
        parse_mode=ParseMode.HTML
    )

async def admin_delprod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Cú pháp: /delprod <product_id>")
        return
    pid = context.args[0]
    if pid not in PRODUCTS:
        await update.message.reply_text("❌ Product ID không tồn tại.")
        return
    name = PRODUCTS[pid]["name"]
    del PRODUCTS[pid]
    await update.message.reply_text(f"✅ Đã xóa sản phẩm <b>{name}</b> (ID: <code>{pid}</code>)", parse_mode=ParseMode.HTML)

async def admin_renameprod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Cú pháp: /renameprod <old_id> <new_id>\nVí dụ: /renameprod key_1m key_30d")
        return
    old_id = context.args[0]
    new_id = context.args[1]
    if old_id not in PRODUCTS:
        await update.message.reply_text("❌ Product ID cũ không tồn tại.")
        return
    if new_id in PRODUCTS:
        await update.message.reply_text("❌ Product ID mới đã tồn tại.")
        return
    PRODUCTS[new_id] = PRODUCTS.pop(old_id)
    await update.message.reply_text(f"✅ Đã đổi Product ID:\n<code>{old_id}</code> → <code>{new_id}</code>", parse_mode=ParseMode.HTML)

# ==================== MAIN ====================
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # User
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🛒 Mua Key$"), show_products))
    application.add_handler(MessageHandler(filters.Regex("^📋 Đơn hàng của tôi$"), my_orders))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ Hướng dẫn$"), guide))
    application.add_handler(MessageHandler(filters.Regex("^📞 Liên hệ Admin$"), contact_admin))
    application.add_handler(MessageHandler(filters.Regex("^🛠 Admin Panel$"), admin_panel))

    # Callbacks
    application.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(paid_callback, pattern="^paid_"))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))
    application.add_handler(CallbackQueryHandler(admin_approve, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(admin_reject, pattern="^reject_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    # Admin commands
    application.add_handler(CommandHandler("broadcast", admin_broadcast))
    application.add_handler(CommandHandler("addkey", admin_addkey))
    application.add_handler(CommandHandler("stats", admin_stats_cmd))
    application.add_handler(CommandHandler("listprod", admin_listprod))
    application.add_handler(CommandHandler("setprice", admin_setprice))
    application.add_handler(CommandHandler("editprod", admin_editprod))
    application.add_handler(CommandHandler("addprod", admin_addprod))
    application.add_handler(CommandHandler("delprod", admin_delprod))
    application.add_handler(CommandHandler("renameprod", admin_renameprod))

    print("✅ Bot đang chạy...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
