import os
import logging
from io import BytesIO
import urllib.request
import qrcode
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from supabase import create_client, Client

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== LẤY BIẾN MÔI TRƯỜNG AN TOÀN ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

try:
    admin_env = os.environ.get("ADMIN_ID", "0")
    ADMIN_ID = int(str(admin_env).strip()) if admin_env else 0
except Exception:
    ADMIN_ID = 0

BANK_NAME = os.environ.get("BANK_NAME", "MB Bank")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "0394569153")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "Phan Hoang An")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://telegram-bot-key-qzt7.onrender.com/webhook")

# Khởi tạo Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Khởi tạo Telegram Application (dùng updater=None cho chế độ Webhook)
telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()

# ==================== CÁC HÀM XỬ LÝ (HANDLERS) ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("🛒 Xem sản phẩm", callback_data="view_products")],
        [InlineKeyboardButton("📜 Đơn hàng của tôi", callback_data="my_orders")],
        [InlineKeyboardButton("📞 Hỗ trợ / Liên hệ", callback_data="support")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 **Chào mừng bạn đến với cửa hàng Game Key!**\n\nChọn chức năng bên dưới:"
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def add_product_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Bạn không có quyền dùng lệnh này.")
        return
    
    try:
        args = context.args
        if len(args) < 3:
            await update.message.reply_text("⚠️ Sai cú pháp! Dùng: `/addproduct <id> <giá> <tên sản phẩm>`", parse_mode="Markdown")
            return
        
        prod_id = int(args[0])
        price = int(args[1])
        name = " ".join(args[2:])

        supabase.table("products").upsert({
            "id": prod_id,
            "name": name,
            "price": price,
            "description": name
        }).execute()

        await update.message.reply_text(f"✅ Đã thêm/cập nhật sản phẩm:\n🆔 ID: {prod_id}\n📦 Tên: {name}\n💰 Giá: {price:,}đ")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

async def add_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Bạn không có quyền dùng lệnh này.")
        return

    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("⚠️ Sai cú pháp! Dùng: `/addkey <product_id> <key_code>`", parse_mode="Markdown")
            return
        
        prod_id = int(args[0])
        key_code = " ".join(args[1:])

        supabase.table("keys").insert({
            "product_id": prod_id,
            "key_code": key_code,
            "is_sold": False
        }).execute()

        await update.message.reply_text(f"✅ Đã thêm key thành công cho sản phẩm ID {prod_id}!")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "view_products":
        res = supabase.table("products").select("*").execute()
        products = res.data
        if not products:
            await query.message.edit_text("📭 Hiện chưa có sản phẩm nào.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")]]))
            return

        keyboard = []
        text = "🛒 **Danh sách sản phẩm:**\n\n"
        for p in products:
            keys_res = supabase.table("keys").select("*", count="exact").eq("product_id", p["id"]).eq("is_sold", False).execute()
            stock = keys_res.count if keys_res.count is not None else 0
            stock_status = f"✅ Còn {stock}" if stock > 0 else "❌ Hết hàng"
            
            text += f"• **{p['name']}**\n💰 `{p['price']:,}đ` | {stock_status}\n\n"
            if stock > 0:
                keyboard.append([InlineKeyboardButton(f"Mua {p['name']} - {p['price']:,}đ", callback_data=f"buy_{p['id']}")])
        
        keyboard.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("buy_"):
        prod_id = int(data.split("_")[1])
        prod_res = supabase.table("products").select("*").eq("id", prod_id).execute()
        if not prod_res.data:
            await query.answer("Sản phẩm không tồn tại!", show_alert=True)
            return
        
        product = prod_res.data[0]
        
        order_res = supabase.table("orders").insert({
            "user_id": user_id,
            "product_id": prod_id,
            "amount": product["price"],
            "status": "pending"
        }).execute()
        
        order_id = order_res.data[0]["id"]
        
        # Tạo mã VietQR
        content = f"DH{order_id}"
        qr_url = f"https://img.vietqr.io/image/{BANK_NAME}-{ACCOUNT_NUMBER}-compact2.png?amount={product['price']}&addInfo={content}&accountName={ACCOUNT_NAME}"
        
        try:
            with urllib.request.urlopen(qr_url) as response:
                qr_bytes = response.read()
        except Exception:
            qr = qrcode.QRCode(box_size=10, border=2)
            qr.add_data(qr_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            img.save(bio, format="PNG")
            bio.seek(0)
            qr_bytes = bio.getvalue()

        caption = (
            f"💳 **Thanh toán đơn hàng #{order_id}**\n\n"
            f"• Sản phẩm: {product['name']}\n"
            f"• Số tiền: **{product['price']:,}đ**\n"
            f"• Ngân hàng: {BANK_NAME}\n"
            f"• Số TK: `{ACCOUNT_NUMBER}`\n"
            f"• Chủ TK: {ACCOUNT_NAME}\n"
            f"• Nội dung chuyển khoản: `DH{order_id}`\n\n"
            f"⚠️ *Vui lòng quét mã QR hoặc chuyển đúng nội dung trên. Sau khi chuyển xong, bấm nút bên dưới để xác nhận!*"
        )
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"paid_{order_id}")],
            [InlineKeyboardButton("❌ Hủy đơn", callback_data="view_products")]
        ])
        
        await query.message.delete()
        await query.message.reply_photo(
            photo=InputFile(BytesIO(qr_bytes), filename=f"qr_dh{order_id}.png"),
            caption=caption,
            reply_markup=kb,
            parse_mode="Markdown"
        )

    elif data.startswith("paid_"):
        order_id = int(data.split("_")[1])
        supabase.table("orders").update({"status": "waiting_approval"}).eq("id", order_id).execute()
        
        await query.message.edit_caption(
            caption=f"⏳ **Đã gửi xác nhận cho Đơn hàng #{order_id}!**\nAdmin sẽ kiểm tra tài khoản và duyệt đơn cho bạn sớm nhất.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Về trang chủ", callback_data="back_home")]]),
            parse_mode="Markdown"
        )
        
        try:
            admin_text = f"🔔 **Có đơn hàng chờ duyệt mới!**\n\n• Mã đơn: `#{order_id}`\n• Khách ID: `{user_id}`"
            admin_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Duyệt đơn #{order_id}", callback_data=f"approve_{order_id}")]
            ])
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_kb, parse_mode="Markdown")
        except Exception:
            pass

    elif data.startswith("approve_"):
        if user_id != ADMIN_ID:
            await query.answer("Bạn không phải Admin!", show_alert=True)
            return
        
        order_id = int(data.split("_")[1])
        order_res = supabase.table("orders").select("*").eq("id", order_id).execute()
        if not order_res.data:
            await query.answer("Đơn hàng không tồn tại!", show_alert=True)
            return
        
        order = order_res.data[0]
        if order["status"] == "completed":
            await query.answer("Đơn này đã được duyệt trước đó rồi!", show_alert=True)
            return
        
        keys_res = supabase.table("keys").select("*").eq("product_id", order["product_id"]).eq("is_sold", False).limit(1).execute()
        if not keys_res.data:
            await query.answer("❌ Kho đã hết key cho sản phẩm này!", show_alert=True)
            return
        
        key_item = keys_res.data[0]
        key_code = key_item["key_code"]
        
        supabase.table("keys").update({"is_sold": True}).eq("id", key_item["id"]).execute()
        supabase.table("orders").update({"status": "completed", "key_code": key_code}).eq("id", order_id).execute()
        
        await query.message.edit_text(f"✅ Đã duyệt thành công đơn hàng `#{order_id}`!\nKey đã cấp: `{key_code}`", parse_mode="Markdown")
        
        try:
            customer_text = f"🎉 **Đơn hàng #{order_id} của bạn đã được duyệt thành công!**\n\n🔑 Key của bạn:\n`{key_code}`\n\nCảm ơn bạn đã ủng hộ shop!"
            await context.bot.send_message(chat_id=order["user_id"], text=customer_text, parse_mode="Markdown")
        except Exception:
            pass

    elif data == "my_orders":
        orders_res = supabase.table("orders").select("id, amount, status, key_code, created_at").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
        orders = orders_res.data
        
        if not orders:
            text = "📜 Bạn chưa có đơn hàng nào."
        else:
            text = "📜 **Lịch sử 5 đơn hàng gần nhất:**\n\n"
            for o in orders:
                status_vn = "Đang chờ" if o["status"] == "pending" else ("Chờ duyệt" if o["status"] == "waiting_approval" else "Hoàn thành ✅")
                text += f"• Đơn `#{o['id']}` - {o['amount']:,}đ\n  Trạng thái: {status_vn}\n"
                if o["key_code"]:
                    text += f"  🔑 Key: `{o['key_code']}`\n"
                text += "\n"
        
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")]]), parse_mode="Markdown")

    elif data == "support":
        text = f"📞 **Hỗ trợ khách hàng**\n\nAdmin: @{ACCOUNT_NAME}\nHotline: {ACCOUNT_NUMBER}"
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")]]), parse_mode="Markdown")

    elif data == "admin_panel":
        if user_id != ADMIN_ID:
            await query.answer("Bạn không có quyền truy cập!", show_alert=True)
            return
        
        text = "⚙️ **Admin Control Panel**\n\nQuản lý nhanh:\n• Thêm sản phẩm: `/addproduct <id> <giá> <tên>`\n• Thêm key: `/addkey <product_id> <key>`"
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")]]), parse_mode="Markdown")

    elif data == "back_home":
        await start_command(update, context)

# ==================== CẤU HÌNH FASTAPI & WEBHOOK ====================
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    try:
        await telegram_app.initialize()
        await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
        await telegram_app.start()
        logger.info(f"✅ Webhook đã được đăng ký thành công tới: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"❌ Lỗi khởi động Webhook: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await telegram_app.stop()
        await telegram_app.shutdown()
    except Exception:
        pass

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"❌ Lỗi xử lý webhook update: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/")
async def root():
    return {"status": "Bot is live and running!"}

# Đăng ký các Handler vào Telegram Application
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CommandHandler("addproduct", add_product_command))
telegram_app.add_handler(CommandHandler("addkey", add_key_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))
