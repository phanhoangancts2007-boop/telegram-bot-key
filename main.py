import os
import html
import logging
from io import BytesIO
import threading

import qrcode
import uvicorn

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # tránh in token bot vào log

# ==================== BIẾN MÔI TRƯỜNG ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # dùng service_role key (giữ bí mật)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

BANK_NAME = os.environ.get("BANK_NAME", "MB Bank")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "0394569153")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "Phan Hoang An")
BANK_BIN = os.environ.get("BANK_BIN", "970422")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()


# ==================== HÀM TRỢ GIÚP ====================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def esc(s) -> str:
    return html.escape(str(s or ""))


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _crc16(data: str) -> str:
    crc = 0xFFFF
    for b in data.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def vietqr_payload(amount: int, content: str) -> str:
    """Chuỗi VietQR (NAPAS 247) chuẩn, có CRC."""
    bank = _tlv("00", BANK_BIN) + _tlv("01", ACCOUNT_NUMBER)
    merchant = _tlv("00", "A000000727") + _tlv("01", bank) + _tlv("02", "QRIBFTTA")
    p = (
        _tlv("00", "01") + _tlv("01", "12") + _tlv("38", merchant)
        + _tlv("53", "704") + _tlv("54", str(amount)) + _tlv("58", "VN")
        + _tlv("62", _tlv("08", content)) + "6304"
    )
    return p + _crc16(p)


def make_qr_png(amount: int, content: str) -> BytesIO:
    img = qrcode.make(vietqr_payload(amount, content), border=2, box_size=8)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    bio.name = "qr.png"
    return bio


def stock_of(product_id: str) -> int:
    res = (
        supabase.table("keys")
        .select("id", count="exact")
        .eq("product_id", product_id)
        .eq("is_sold", False)
        .execute()
    )
    return res.count or 0


async def safe_edit(query, text, kb=None):
    """Sửa tin nhắn; nếu tin nhắn là ảnh (không sửa được text) thì gửi tin mới."""
    try:
        await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


BTN_SHOP = "🛒 Mua Key"
BTN_ADMIN = "🛠 Admin Panel"


def reply_menu(user_id: int) -> ReplyKeyboardMarkup:
    rows = [[BTN_SHOP]]
    if is_admin(user_id):
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def products_view():
    """Giao diện 'Danh sách sản phẩm' + nút Mua cho mặt hàng còn hàng."""
    products = supabase.table("products").select("*").order("sort_order").execute().data or []
    if not products:
        return "🛒 <b>Danh sách sản phẩm</b>\n\nHiện chưa có sản phẩm nào.", None

    text = "🛒 <b>Danh sách sản phẩm</b>\n\n"
    buttons = []
    for p in products:
        stock = stock_of(p["id"])
        status = f"✅ Còn {stock}" if stock > 0 else "❌ Hết hàng"
        text += f"• <b>{esc(p['name'])}</b>\n💰 <code>{p['price']:,}đ</code> | {status}\n"
        if p.get("description"):
            text += f"{esc(p['description'])}\n"
        text += "\n"
        if stock > 0:
            buttons.append(
                [InlineKeyboardButton(f"Mua {p['name']} - {p['price']:,}đ", callback_data=f"buy:{p['id']}")]
            )
    if not buttons:
        text += "⚠️ Hiện tại tất cả sản phẩm đều hết hàng."
    return text, (InlineKeyboardMarkup(buttons) if buttons else None)


def admin_panel_view():
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Thêm Key", callback_data="admin_addkey")],
            [InlineKeyboardButton("📦 Tồn kho key", callback_data="admin_stock")],
            [InlineKeyboardButton("📋 Đơn chờ duyệt", callback_data="admin_pending")],
            [InlineKeyboardButton("🛍 Quản lý sản phẩm", callback_data="admin_prodhelp")],
        ]
    )
    return "🛠 <b>Admin Panel</b>", kb


def addkey_help_text() -> str:
    products = supabase.table("products").select("id").order("sort_order").execute().data or []
    ids = "\n".join(f"• <code>{esc(p['id'])}</code>" for p in products) or "(chưa có sản phẩm)"
    return (
        "➕ <b>Thêm Key</b>\n\nCú pháp:\n<code>/addkey product_id key1 key2 key3...</code>\n\n"
        "Ví dụ:\n<code>/addkey key_1m KEY-ABC-123 KEY-DEF-456</code>\n\n"
        f"Product ID hiện có:\n{ids}"
    )


def orders_view(user_id: int) -> str:
    orders = (
        supabase.table("orders").select("id, status, products(name)")
        .eq("user_id", user_id).order("id", desc=True).limit(10).execute().data or []
    )
    if not orders:
        return "Bạn chưa có đơn hàng nào."
    status_map = {"pending": "⏳ Chờ duyệt", "completed": "🎉 Hoàn thành", "cancelled": "❌ Đã hủy"}
    text = "📦 <b>Đơn hàng của bạn:</b>\n\n"
    for o in orders:
        p_name = (o.get("products") or {}).get("name", "N/A")
        text += f"#{o['id']} | {esc(p_name)}\n{status_map.get(o['status'], o['status'])}\n\n"
    return text


ADMIN_HELP = (
    "⚙️ <b>Lệnh quản trị</b>\n\n"
    "<b>Sản phẩm</b>\n"
    "<code>/addprod id | Tên | giá | mô tả</code>\n"
    "VD: <code>/addprod key_1m | Premium Key 1 Tháng | 99000 | Dùng 30 ngày</code>\n"
    "<code>/setprice id giá</code>\n"
    "<code>/setname id Tên mới</code>\n"
    "<code>/setdesc id Mô tả mới</code>\n"
    "<code>/delprod id</code>\n\n"
    "<b>Key</b>\n"
    "<code>/addkey id key1 key2 key3...</code> (có thể xuống dòng)\n"
    "<code>/stock</code> - xem tồn kho\n\n"
    "<b>Khác</b>\n"
    "<code>/broadcast nội dung</code> - gửi cho mọi người dùng"
)


# ==================== USER ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        supabase.table("bot_users").upsert(
            {"user_id": user.id, "full_name": user.full_name}
        ).execute()
    except Exception as e:
        logger.error(f"Lỗi lưu user: {e}")
    await update.message.reply_text(
        f"Xin chào {esc(user.first_name)}!\nBấm <b>🛒 Mua Key</b> bên dưới để xem sản phẩm.",
        reply_markup=reply_menu(user.id),
        parse_mode="HTML",
    )


async def msg_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = products_view()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def msg_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, kb = admin_panel_view()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(orders_view(update.effective_user.id), parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    # ---------- Danh sách sản phẩm ----------
    if data == "show_products":
        text, kb = products_view()
        await safe_edit(query, text, kb)
        await query.answer()

    # ---------- Mua ----------
    elif data.startswith("buy:"):
        product_id = data.split(":", 1)[1]
        res = supabase.table("products").select("*").eq("id", product_id).execute()
        if not res.data:
            await query.answer("Sản phẩm không tồn tại!", show_alert=True)
            return
        if stock_of(product_id) <= 0:
            await query.answer("Sản phẩm đã hết hàng!", show_alert=True)
            return

        product = res.data[0]
        name, price = product["name"], product["price"]

        order = (
            supabase.table("orders")
            .insert({"user_id": user.id, "product_id": product_id, "status": "pending"})
            .execute()
            .data[0]
        )
        order_id = order["id"]
        content = f"DH{order_id}"

        try:
            admin_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"✅ Duyệt đơn #{order_id}", callback_data=f"approve:{order_id}"),
                  InlineKeyboardButton("❌ Hủy", callback_data=f"reject:{order_id}")]]
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 <b>ĐƠN HÀNG MỚI #{order_id}</b>\n\n"
                f"👤 <a href='tg://user?id={user.id}'>{esc(user.full_name)}</a>\n"
                f"🆔 <code>{user.id}</code>\n🎮 {esc(name)}\n💰 {price:,}đ",
                reply_markup=admin_kb, parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Lỗi gửi thông báo cho Admin: {e}")

        caption = (
            f"✅ <b>Đã tạo đơn hàng #{order_id}</b>\n\n"
            f"Sản phẩm: <b>{esc(name)}</b>\nGiá: <b>{price:,}đ</b>\n\n"
            f"💳 <b>Thông tin chuyển khoản:</b>\n"
            f"Ngân hàng: {esc(BANK_NAME)}\n"
            f"Số tài khoản: <code>{esc(ACCOUNT_NUMBER)}</code>\n"
            f"Chủ tài khoản: {esc(ACCOUNT_NAME)}\n"
            f"Nội dung: <code>{content}</code>\n\n"
            f"Hoặc quét mã QR bên dưới. Chuyển xong hãy bấm nút xác nhận."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"paid:{order_id}")],
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="show_products")],
            ]
        )
        try:
            await query.message.reply_photo(
                photo=make_qr_png(price, content), caption=caption, reply_markup=kb, parse_mode="HTML"
            )
        except Exception as e:  # QR lỗi thì vẫn gửi thông tin chuyển khoản dạng chữ
            logger.exception("Lỗi gửi QR")
            await query.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")
        await query.answer()

    # ---------- Khách báo đã chuyển khoản ----------
    elif data.startswith("paid:"):
        order_id = int(data.split(":")[1])
        res = supabase.table("orders").select("*, products(name, price)").eq("id", order_id).execute()
        if not res.data:
            await query.answer("Đơn hàng không tồn tại!", show_alert=True)
            return
        order = res.data[0]
        if order["user_id"] != user.id:
            await query.answer("Đây không phải đơn của bạn!", show_alert=True)
            return
        if order["status"] != "pending":
            await query.answer("Đơn hàng đã được xử lý!", show_alert=True)
            return

        p = order.get("products") or {}
        try:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"✅ Duyệt đơn #{order_id}", callback_data=f"approve:{order_id}"),
                  InlineKeyboardButton("❌ Hủy", callback_data=f"reject:{order_id}")]]
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"📢 <b>KHÁCH XÁC NHẬN ĐÃ CHUYỂN KHOẢN</b>\n\n"
                f"Đơn: <b>#{order_id}</b>\n"
                f"Khách: <a href='tg://user?id={user.id}'>{esc(user.full_name)}</a> (<code>{user.id}</code>)\n"
                f"Sản phẩm: {esc(p.get('name'))}\nSố tiền: {p.get('price', 0):,}đ\n\n"
                f"Hãy kiểm tra tài khoản rồi duyệt đơn.",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Lỗi thông báo xác nhận: {e}")

        note = "\n\n✅ Bạn đã xác nhận chuyển khoản. Vui lòng chờ admin duyệt."
        try:
            if query.message.caption is not None:
                await query.message.edit_caption(caption=query.message.caption + note, reply_markup=None)
            else:
                await query.message.edit_text(query.message.text + note, reply_markup=None)
        except BadRequest:
            pass
        await query.answer("Đã gửi xác nhận cho admin!")

    # ==================== ADMIN ====================
    elif data == "admin_panel":
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        text, kb = admin_panel_view()
        await safe_edit(query, text, kb)
        await query.answer()

    elif data in ("admin_addkey", "admin_prodhelp"):
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        text = addkey_help_text() if data == "admin_addkey" else ADMIN_HELP
        await safe_edit(
            query, text,
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]),
        )
        await query.answer()

    elif data == "admin_stock":
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        await safe_edit(
            query, stock_report(),
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]),
        )
        await query.answer()

    elif data == "admin_pending":
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        orders = (
            supabase.table("orders")
            .select("id, user_id, products(name, price)")
            .eq("status", "pending")
            .order("id")
            .execute()
            .data
            or []
        )
        back = [InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]
        if not orders:
            await safe_edit(query, "Không có đơn chờ duyệt.", InlineKeyboardMarkup([back]))
            await query.answer()
            return
        text = "📋 <b>Đơn chờ duyệt:</b>\n\n"
        buttons = []
        for o in orders:
            p = o.get("products") or {}
            text += f"#{o['id']} | {esc(p.get('name'))} | {p.get('price', 0):,}đ\nUser: <code>{o['user_id']}</code>\n\n"
            buttons.append(
                [InlineKeyboardButton(f"✅ Duyệt #{o['id']}", callback_data=f"approve:{o['id']}"),
                 InlineKeyboardButton("❌ Hủy", callback_data=f"reject:{o['id']}")]
            )
        buttons.append(back)
        await safe_edit(query, text, InlineKeyboardMarkup(buttons))
        await query.answer()

    elif data.startswith("approve:"):
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        order_id = int(data.split(":")[1])

        o_res = (
            supabase.table("orders")
            .select("*, products(name)")
            .eq("id", order_id)
            .eq("status", "pending")
            .execute()
        )
        if not o_res.data:
            await query.answer("Đơn không tồn tại hoặc đã xử lý", show_alert=True)
            return
        order = o_res.data[0]
        product_id = order["product_id"]
        customer_id = order["user_id"]
        product_name = (order.get("products") or {}).get("name", "")

        # Lấy 1 key chưa bán và "khóa" nó (chỉ thành công nếu chưa ai lấy)
        key_code = None
        for _ in range(3):
            k_res = (
                supabase.table("keys").select("id, key_code")
                .eq("product_id", product_id).eq("is_sold", False).limit(1).execute()
            )
            if not k_res.data:
                break
            k = k_res.data[0]
            claim = (
                supabase.table("keys").update({"is_sold": True})
                .eq("id", k["id"]).eq("is_sold", False).execute()
            )
            if claim.data:
                key_code = k["key_code"]
                break

        if not key_code:
            await query.answer("Hết key trong kho!", show_alert=True)
            return

        supabase.table("orders").update({"status": "completed", "key_code": key_code}).eq("id", order_id).execute()

        try:
            await context.bot.send_message(
                customer_id,
                f"🎉 <b>Đơn hàng #{order_id} đã được duyệt!</b>\n\n"
                f"Sản phẩm: {esc(product_name)}\nKey của bạn:\n<code>{esc(key_code)}</code>",
                parse_mode="HTML",
            )
            await query.answer("Đã gửi key thành công!")
        except Exception as e:
            await query.answer("Không gửi được tin cho khách, xem tin nhắn bên dưới", show_alert=True)
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ Không gửi được key cho khách <code>{customer_id}</code> (đơn #{order_id}): {esc(e)}\n"
                f"Key: <code>{esc(key_code)}</code>",
                parse_mode="HTML",
            )
        try:
            base = query.message.text or query.message.caption or ""
            await query.message.edit_text(base + f"\n\n✅ Đã duyệt đơn #{order_id}", reply_markup=None)
        except Exception:
            pass

    elif data.startswith("reject:"):
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        order_id = int(data.split(":")[1])
        res = (
            supabase.table("orders").update({"status": "cancelled"})
            .eq("id", order_id).eq("status", "pending").execute()
        )
        if not res.data:
            await query.answer("Đơn không tồn tại hoặc đã xử lý", show_alert=True)
            return
        try:
            await context.bot.send_message(res.data[0]["user_id"], f"❌ Đơn hàng #{order_id} đã bị hủy.")
        except Exception:
            pass
        try:
            base = query.message.text or ""
            await query.message.edit_text(base + f"\n\n❌ Đã hủy đơn #{order_id}", reply_markup=None)
        except Exception:
            pass
        await query.answer("Đã hủy đơn")


# ==================== LỆNH ADMIN ====================
def stock_report() -> str:
    products = supabase.table("products").select("*").order("price").execute().data or []
    if not products:
        return "Chưa có sản phẩm nào."
    text = "📦 <b>TỒN KHO KEY</b>\n\n"
    for p in products:
        text += f"• <code>{esc(p['id'])}</code> | {esc(p['name'])}\n  💰 {p['price']:,}đ | 📦 {stock_of(p['id'])} key\n"
    return text


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        try:
            await func(update, context)
        except Exception as e:
            logger.exception("Lỗi lệnh admin")
            await update.message.reply_text(f"⚠️ Lỗi: {e}")
    return wrapper


@admin_only
async def cmd_help(update, context):
    await update.message.reply_text(ADMIN_HELP, parse_mode="HTML")


@admin_only
async def cmd_addprod(update, context):
    raw = update.message.text.split(maxsplit=1)
    parts = [x.strip() for x in raw[1].split("|")] if len(raw) > 1 else []
    if len(parts) < 3 or not parts[2].replace(",", "").replace(".", "").isdigit():
        await update.message.reply_text("Cú pháp: /addprod id | Tên | giá | mô tả")
        return
    pid, name = parts[0], parts[1]
    price = int(parts[2].replace(",", "").replace(".", ""))
    desc = parts[3] if len(parts) > 3 else ""
    supabase.table("products").upsert(
        {"id": pid, "name": name, "price": price, "description": desc}
    ).execute()
    await update.message.reply_text(f"✅ Đã lưu sản phẩm {pid}: {name} - {price:,}đ")


@admin_only
async def cmd_setprice(update, context):
    if len(context.args) != 2 or not context.args[1].isdigit():
        await update.message.reply_text("Cú pháp: /setprice id giá")
        return
    pid, price = context.args[0], int(context.args[1])
    res = supabase.table("products").update({"price": price}).eq("id", pid).execute()
    await update.message.reply_text(
        f"✅ Đã đổi giá {pid} thành {price:,}đ" if res.data else "❌ Không tìm thấy sản phẩm."
    )


@admin_only
async def cmd_setname(update, context):
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Cú pháp: /setname id Tên mới")
        return
    res = supabase.table("products").update({"name": parts[2].strip()}).eq("id", parts[1]).execute()
    await update.message.reply_text("✅ Đã đổi tên." if res.data else "❌ Không tìm thấy sản phẩm.")


@admin_only
async def cmd_setdesc(update, context):
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text("Cú pháp: /setdesc id Mô tả mới")
        return
    res = supabase.table("products").update({"description": parts[2].strip()}).eq("id", parts[1]).execute()
    await update.message.reply_text("✅ Đã đổi mô tả." if res.data else "❌ Không tìm thấy sản phẩm.")


@admin_only
async def cmd_delprod(update, context):
    if len(context.args) != 1:
        await update.message.reply_text("Cú pháp: /delprod id")
        return
    res = supabase.table("products").delete().eq("id", context.args[0]).execute()
    await update.message.reply_text(
        "🗑 Đã xóa sản phẩm (kèm toàn bộ key của nó)." if res.data else "❌ Không tìm thấy sản phẩm."
    )


@admin_only
async def cmd_addkey(update, context):
    parts = update.message.text.split()
    if len(parts) < 3:
        await update.message.reply_text("Cú pháp: /addkey id key1 key2 key3...")
        return
    pid, keys = parts[1], list(dict.fromkeys(parts[2:]))  # bỏ key trùng trong cùng lệnh
    if not supabase.table("products").select("id").eq("id", pid).execute().data:
        await update.message.reply_text(f"❌ Không có sản phẩm '{pid}'. Xem /stock để biết ID.")
        return
    supabase.table("keys").insert(
        [{"product_id": pid, "key_code": k, "is_sold": False} for k in keys]
    ).execute()
    await update.message.reply_text(f"✅ Đã thêm {len(keys)} key cho {pid}. Tồn kho: {stock_of(pid)}")


@admin_only
async def cmd_stock(update, context):
    await update.message.reply_text(stock_report(), parse_mode="HTML")


@admin_only
async def cmd_broadcast(update, context):
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Cú pháp: /broadcast nội dung")
        return
    users = supabase.table("bot_users").select("user_id").execute().data or []
    ok = fail = 0
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], parts[1])
            ok += 1
        except Exception:
            fail += 1
    await update.message.reply_text(f"📣 Đã gửi: {ok} | Lỗi: {fail}")


# ==================== ĐĂNG KÝ HANDLERS ====================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("help", cmd_help))
telegram_app.add_handler(CommandHandler("orders", cmd_orders))
telegram_app.add_handler(MessageHandler(filters.Text([BTN_SHOP]), msg_shop))
telegram_app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]), msg_admin))
telegram_app.add_handler(CommandHandler("addprod", cmd_addprod))
telegram_app.add_handler(CommandHandler("setprice", cmd_setprice))
telegram_app.add_handler(CommandHandler("setname", cmd_setname))
telegram_app.add_handler(CommandHandler("setdesc", cmd_setdesc))
telegram_app.add_handler(CommandHandler("delprod", cmd_delprod))
telegram_app.add_handler(CommandHandler("addkey", cmd_addkey))
telegram_app.add_handler(CommandHandler("stock", cmd_stock))
telegram_app.add_handler(CommandHandler("broadcast", cmd_broadcast))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


# ==================== FASTAPI & MAIN ====================
@app.get("/")
def health():
    return {"status": "running"}

def run_web():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Chạy Web Server ở luồng riêng để đáp ứng yêu cầu port binding của Render
    threading.Thread(target=run_web, daemon=True).start()
    
    # Chạy Bot Telegram dạng Polling ở luồng chính
    telegram_app.run_polling()
