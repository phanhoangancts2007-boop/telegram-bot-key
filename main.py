import os
import logging
from io import BytesIO
import qrcode
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client

# ==================== CẤU HÌNH LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== LẤY BIẾN MÔI TRƯỜNG ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

BANK_NAME = os.environ.get("BANK_NAME", "MB Bank")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "0394569153")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "Phan Hoang An")
BANK_BIN = os.environ.get("BANK_BIN", "970422")

# Khởi tạo Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Khởi tạo FastAPI & Telegram Bot Application
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()


# ==================== HÀM TRỢ GIÚP ====================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def generate_vietqr(amount: int, content: str) -> BytesIO:
    """Tạo mã QR VietQR chuẩn"""
    payload = (
        f"00020101021238570010A00000072701270006{BANK_BIN}01{len(ACCOUNT_NUMBER):02d}{ACCOUNT_NUMBER}"
        f"0208QRIBFTTA530370454{len(str(amount)):02d}{amount}"
        f"5802VN62{len(content)+4:02d}08{len(content):02d}{content}6304"
    )
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


def get_available_keys_count(product_id: int) -> int:
    res = (
        supabase.table("keys")
        .select("id", count="exact")
        .eq("product_id", product_id)
        .eq("is_sold", False)
        .execute()
    )
    return res.count if res.count is not None else 0


# ==================== CÁC XỬ LÝ DÀNH CHO USER ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("🛒 Xem sản phẩm", callback_data="show_products")],
        [InlineKeyboardButton("📦 Đơn hàng của tôi", callback_data="my_orders")],
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"Xin chào {update.effective_user.first_name}!\n"
        "Chào mừng đến cửa hàng Game Key.\nChọn chức năng:"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    if data == "show_products":
        res = supabase.table("products").select("*").execute()
        products = res.data or []

        if not products:
            await query.message.edit_text("Hiện chưa có sản phẩm nào.")
            return

        text = "🎮 <b>Danh sách Game Key:</b>\n\n"
        buttons = []
        for p in products:
            pid = p["id"]
            name = p["name"]
            price = p["price"]
            desc = p.get("description", "")
            stock = get_available_keys_count(pid)

            text += f"<b>{name}</b>\n💰 Giá: {price:,}đ\n📦 Còn: {stock} key\n"
            if desc:
                text += f"📝 {desc}\n"
            text += "─" * 20 + "\n"

            if stock > 0:
                buttons.append(
                    [InlineKeyboardButton(f"Mua {name} - {price:,}đ", callback_data=f"buy_{pid}")]
                )

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back_main")])
        await query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
        )

    elif data.startswith("buy_"):
        product_id = int(data.split("_")[1])
        stock = get_available_keys_count(product_id)
        if stock <= 0:
            await query.answer("Sản phẩm đã hết hàng!", show_alert=True)
            return

        res = supabase.table("products").select("*").eq("id", product_id).execute()
        if not res.data:
            await query.answer("Sản phẩm không tồn tại!", show_alert=True)
            return

        product = res.data[0]
        name = product["name"]
        price = product["price"]

        # Tạo đơn hàng mới trong DB
        order_res = (
            supabase.table("orders")
            .insert({"user_id": user.id, "product_id": product_id, "status": "pending"})
            .execute()
        )
        order_id = order_res.data[0]["id"]
        content = f"DH{order_id}"

        # Tạo QR thanh toán
        qr_image = generate_vietqr(price, content)

        # Gửi thông báo đến Admin
        try:
            admin_text = (
                f"🔔 <b>ĐƠN HÀNG MỚI #{order_id}</b>\n\n"
                f"👤 Khách: <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"🎮 {name}\n"
                f"💰 {price:,}đ"
            )
            admin_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"✅ Duyệt đơn #{order_id}", callback_data=f"approve_{order_id}")]]
            )
            await context.bot.send_message(
                ADMIN_ID, admin_text, reply_markup=admin_kb, parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Lỗi gửi thông báo cho Admin: {e}")

        caption = (
            f"✅ <b>Đã tạo đơn hàng #{order_id}</b>\n\n"
            f"Sản phẩm: <b>{name}</b>\n"
            f"Giá: <b>{price:,}đ</b>\n\n"
            f"💳 <b>Thông tin chuyển khoản:</b>\n"
            f"Ngân hàng: {BANK_NAME}\n"
            f"Số tài khoản: <code>{ACCOUNT_NUMBER}</code>\n"
            f"Chủ tài khoản: {ACCOUNT_NAME}\n"
            f"Nội dung: <code>{content}</code>\n\n"
            f"Hoặc quét mã QR bên dưới để thanh toán nhanh.\n"
            f"Sau khi chuyển khoản xong, hãy bấm nút xác nhận."
        )

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"confirm_paid_{order_id}")],
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="show_products")],
            ]
        )

        await query.message.reply_photo(
            photo=BufferedInputFile(qr_image.read(), filename=f"qr_dh{order_id}.png"),
            caption=caption,
            reply_markup=kb,
            parse_mode="HTML",
        )
        await query.answer()

    elif data.startswith("confirm_paid_"):
        order_id = int(data.split("_")[2])
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

        try:
            text = (
                f"📢 <b>KHÁCH XÁC NHẬN ĐÃ CHUYỂN KHOẢN</b>\n\n"
                f"Đơn hàng: <b>#{order_id}</b>\n"
                f"Khách: <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
                f"ID: <code>{user.id}</code>\n"
                f"Sản phẩm: {order['products']['name']}\n"
                f"Số tiền: {order['products']['price']:,}đ\n\n"
                f"Vui lòng kiểm tra tài khoản và duyệt đơn."
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"✅ Duyệt đơn #{order_id}", callback_data=f"approve_{order_id}")]]
            )
            await context.bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Lỗi thông báo xác nhận: {e}")

        await query.message.edit_caption(
            caption=query.message.caption + "\n\n✅ Bạn đã xác nhận đã chuyển khoản. Vui lòng chờ admin duyệt.",
            reply_markup=None,
        )
        await query.answer("Đã gửi xác nhận cho admin!")

    elif data == "my_orders":
        res = (
            supabase.table("orders")
            .select("id, status, products(name)")
            .eq("user_id", user.id)
            .order("id", desc=True)
            .limit(10)
            .execute()
        )
        orders = res.data or []

        if not orders:
            await query.message.edit_text("Bạn chưa có đơn hàng nào.")
            return

        text = "📦 <b>Đơn hàng của bạn:</b>\n\n"
        status_map = {
            "pending": "⏳ Chờ thanh toán",
            "completed": "🎉 Hoàn thành",
            "cancelled": "❌ Đã hủy",
        }
        for o in orders:
            p_name = o["products"]["name"] if o.get("products") else "N/A"
            text += f"#{o['id']} | {p_name}\n{status_map.get(o['status'], o['status'])}\n" + "─" * 20 + "\n"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_main")]])
        await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "back_main":
        await cmd_start(update, context)

    # ==================== ADMIN CALLS ====================
    elif data == "admin_panel":
        if not is_admin(user.id):
            await query.answer("Không có quyền!", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 Đơn chờ duyệt", callback_data="admin_pending")],
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="back_main")],
            ]
        )
        await query.message.edit_text("⚙️ <b>Admin Panel</b>", reply_markup=kb, parse_mode="HTML")

    elif data == "admin_pending":
        if not is_admin(user.id):
            return
        res = (
            supabase.table("orders")
            .select("id, user_id, products(name, price)")
            .eq("status", "pending")
            .execute()
        )
        orders = res.data or []

        if not orders:
            await query.message.edit_text(
                "Không có đơn chờ duyệt.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]
                ),
            )
            return

        text = "📋 <b>Đơn chờ duyệt:</b>\n\n"
        buttons = []
        for o in orders:
            oid = o["id"]
            uid = o["user_id"]
            p_name = o["products"]["name"]
            p_price = o["products"]["price"]
            text += f"#{oid} | {p_name} | {p_price:,}đ\nUser: {uid}\n" + "─" * 20 + "\n"
            buttons.append([InlineKeyboardButton(f"✅ Duyệt #{oid}", callback_data=f"approve_{oid}")])

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")])
        await query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
        )

    elif data.startswith("approve_"):
        if not is_admin(user.id):
            return
        order_id = int(data.split("_")[1])

        # Lấy thông tin đơn hàng
        o_res = supabase.table("orders").select("*, products(name)").eq("id", order_id).eq("status", "pending").execute()
        if not o_res.data:
            await query.answer("Đơn không tồn tại hoặc đã xử lý", show_alert=True)
            return

        order = o_res.data[0]
        product_id = order["product_id"]
        customer_id = order["user_id"]
        product_name = order["products"]["name"]

        # Tìm 1 key chưa sử dụng
        k_res = (
            supabase.table("keys")
            .select("*")
            .eq("product_id", product_id)
            .eq("is_sold", False)
            .limit(1)
            .execute()
        )
        if not k_res.data:
            await query.answer("Hết key trong kho!", show_alert=True)
            return

        key_info = k_res.data[0]
        key_id = key_info["id"]
        key_code = key_info["key_code"]

        # Cập nhật DB
        supabase.table("keys").update({"is_sold": True}).eq("id", key_id).execute()
        supabase.table("orders").update({"status": "completed"}).eq("id", order_id).execute()

        try:
            await context.bot.send_message(
                customer_id,
                f"🎉 <b>Đơn hàng #{order_id} đã được duyệt!</b>\n\n"
                f"Sản phẩm: {product_name}\n"
                f"Key của bạn:\n<code>{key_code}</code>",
                parse_mode="HTML",
            )
            await query.answer("Đã gửi key thành công!")
        except Exception as e:
            await query.answer(f"Lỗi gửi tin nhắn: {e}", show_alert=True)
            # ==================== LỆNH THÊM SẢN PHẨM & KEY DÀNH CHO ADMIN ====================
async def cmd_addproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Cú pháp: /addproduct <id> <price> <name>
    if len(context.args) < 3:
        await update.message.reply_text("❌ Cú pháp: `/addproduct <id> <price> <name>`\nVí dụ: `/addproduct 1 50000 Key Windows 11`", parse_mode="Markdown")
        return

    try:
        pid = int(context.args[0])
        price = int(context.args[1])
        name = " ".join(context.args[2:])

        supabase.table("products").insert({
            "id": pid,
            "name": name,
            "price": price,
            "description": name
        }).execute()

        await update.message.reply_text(f"✅ Đã thêm sản phẩm:\n<b>ID:</b> {pid}\n<b>Tên:</b> {name}\n<b>Giá:</b> {price:,}đ", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Cú pháp: /addkey <product_id> <key_code>
    if len(context.args) < 2:
        await update.message.reply_text("❌ Cú pháp: `/addkey <product_id> <key_code>`\nVí dụ: `/addkey 1 XXXX-YYYY-ZZZZ`", parse_mode="Markdown")
        return

    try:
        product_id = int(context.args[0])
        key_code = context.args[1]

        supabase.table("keys").insert({
            "product_id": product_id,
            "key_code": key_code,
            "is_sold": False
        }).execute()

        await update.message.reply_text(f"✅ Đã thêm 1 key cho sản phẩm ID `{product_id}`!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

# Đăng ký handler lệnh Admin
telegram_app.add_handler(CommandHandler("addproduct", cmd_addproduct))
telegram_app.add_handler(CommandHandler("addkey", cmd_addkey))


# ==================== ĐĂNG KÝ HANDLERS ====================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


# ==================== FASTAPI WEBHOOK CONFIG ====================
@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    logger.info("Telegram Bot Application đã khởi tạo thành công.")


@app.post("/")
async def webhook(request: Request):
    json_data = await request.json()
    update = Update.de_json(json_data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}
