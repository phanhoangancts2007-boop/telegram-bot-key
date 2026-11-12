import os
import html
import logging
import datetime
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from supabase import create_client, Client

# Import SDK payOS
from payos import AsyncPayOS
from payos.types import CreatePaymentLinkRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==================== BIẾN MÔI TRƯỜNG ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_RAW.split(",") if id.strip().isdigit()]

# Biến môi trường payOS
PAYOS_CLIENT_ID = os.environ.get("PAYOS_CLIENT_ID", "")
PAYOS_API_KEY = os.environ.get("PAYOS_API_KEY", "")
PAYOS_CHECKSUM_KEY = os.environ.get("PAYOS_CHECKSUM_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Khởi tạo client payOS
payos_client = AsyncPayOS(
    client_id=PAYOS_CLIENT_ID,
    api_key=PAYOS_API_KEY,
    checksum_key=PAYOS_CHECKSUM_KEY
)

# ==================== HÀM CƠ BẢN ====================
def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS
def esc(s) -> str: return html.escape(str(s or ""))

def stock_of(product_id):
    res = supabase.table("keys").select("id", count="exact").eq("product_id", product_id).eq("is_sold", False).execute()
    return res.count or 0

def is_maintenance():
    try:
        res = supabase.table("settings").select("value").eq("key", "maintenance").execute()
        return res.data and res.data[0]["value"] == "true"
    except: return False

async def safe_edit(query, text, kb=None):
    try: await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower(): await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ==================== GIAO DIỆN ====================
BTN_SHOP = "🛒 Mua Key"
BTN_ADMIN = "🛠 Admin Panel"

def reply_menu(user_id):
    rows = [[BTN_SHOP]]
    if is_admin(user_id): rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def products_view():
    products = supabase.table("products").select("*").order("sort_order").execute().data or []
    if not products: return "🛒 <b>Danh sách sản phẩm</b>\n\nHiện chưa có sản phẩm nào.", None
    
    text, buttons = "🛒 <b>Danh sách sản phẩm</b>\n\n", []
    maint = is_maintenance()
    if maint: text += "⚠️ <b>SHOP ĐANG BẢO TRÌ, VUI LÒNG QUAY LẠI SAU!</b>\n\n"

    for p in products:
        stock = stock_of(p["id"])
        text += f"• <b>{esc(p['name'])}</b>\n💰 <code>{p['price']:,}đ</code> | {('✅ Còn ' + str(stock)) if stock > 0 else '❌ Hết hàng'}\n"
        if p.get('description'): text += f"<i>{esc(p['description'])}</i>\n"
        text += "\n"
        if stock > 0 and not maint: 
            buttons.append([InlineKeyboardButton(f"Mua {p['name']}", callback_data=f"select_qty:{p['id']}:1")])
    return text, (InlineKeyboardMarkup(buttons) if buttons else None)

def admin_panel_view():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"), InlineKeyboardButton("📦 Tồn kho", callback_data="admin_stock")],
        [InlineKeyboardButton("🎟 Quản lý Mã Giảm Giá", callback_data="admin_coupon_help")],
    ])
    return "🛠 <b>Admin Panel</b>\nGõ /help để xem toàn bộ lệnh.", kb

# ==================== LỆNH ADMIN CƠ BẢN ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supabase.table("bot_users").upsert({"user_id": update.effective_user.id, "full_name": update.effective_user.full_name}).execute()
    await update.message.reply_text(f"Xin chào {esc(update.effective_user.first_name)}!\nBấm nút bên dưới để mua hàng.", reply_markup=reply_menu(update.effective_user.id))

async def msg_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = products_view(); await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def msg_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        text, kb = admin_panel_view(); await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    text = (
        "⚙️ <b>DANH SÁCH LỆNH ADMIN</b>\n\n"
        "👉 <code>/addprod id | Tên | Giá | Mô tả</code>\n"
        "👉 <code>/setprice id Giá_mới</code>\n"
        "👉 <code>/setname id Tên_mới</code>\n"
        "👉 <code>/delprod id</code>\n"
        "👉 <code>/addkey id key1 key2...</code>\n"
        "👉 <code>/stock</code> (Xem tồn kho)\n"
        "👉 <code>/stats</code> (Xem doanh thu)\n"
        "👉 <code>/find Mã_Đơn</code> (Tìm đơn hàng)\n"
        "👉 <code>/addcoupon Mã | percent/fixed | Số_giảm</code>\n"
        "👉 <code>/maintenance on/off</code> (Bật/tắt bảo trì)\n"
        "👉 <code>/broadcast Nội_dung</code> (Gửi tất cả khách)\n"
        "👉 <code>/addmoney ID Số_tiền</code> (Cộng/trừ tiền khách)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_addprod(update, context):
    if not is_admin(update.effective_user.id): return
    raw = update.message.text.split(maxsplit=1)
    parts = [x.strip() for x in raw[1].split("|")] if len(raw) > 1 else []
    if len(parts) >= 3 and parts[2].replace(",", "").isdigit():
        supabase.table("products").upsert({"id": parts[0], "name": parts[1], "price": int(parts[2].replace(",", "")), "description": parts[3] if len(parts)>3 else ""}).execute()
        await update.message.reply_text(f"✅ Đã lưu SP {parts[0]}")
    else: await update.message.reply_text("Cú pháp: /addprod id | Tên | Giá | Mô tả")

async def cmd_setprice(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2 or not context.args[1].replace(",", "").isdigit(): return await update.message.reply_text("Cú pháp: /setprice id Giá")
    supabase.table("products").update({"price": int(context.args[1].replace(",", ""))}).eq("id", context.args[0]).execute()
    await update.message.reply_text(f"✅ Đã đổi giá {context.args[0]}")

async def cmd_setname(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3: return await update.message.reply_text("Cú pháp: /setname id Tên_mới")
    supabase.table("products").update({"name": parts[2]}).eq("id", parts[1]).execute()
    await update.message.reply_text(f"✅ Đã đổi tên {parts[1]}")

async def cmd_delprod(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: return await update.message.reply_text("Cú pháp: /delprod id")
    supabase.table("products").delete().eq("id", context.args[0]).execute()
    await update.message.reply_text(f"🗑 Đã xóa SP {context.args[0]} và toàn bộ key.")

async def cmd_addkey(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split()
    if len(parts) >= 3:
        keys = list(dict.fromkeys(parts[2:]))
        supabase.table("keys").insert([{"product_id": parts[1], "key_code": k, "is_sold": False} for k in keys]).execute()
        await update.message.reply_text(f"✅ Đã thêm {len(keys)} key cho {parts[1]}.")
    else: await update.message.reply_text("Cú pháp: /addkey id key1 key2...")

async def cmd_stock(update, context):
    if not is_admin(update.effective_user.id): return
    products = supabase.table("products").select("*").execute().data or []
    text = "📦 <b>Tồn kho:</b>\n\n"
    for p in products: text += f"• {esc(p['name'])}: <b>{stock_of(p['id'])}</b>\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_stats(update, context):
    if not is_admin(update.effective_user.id): return
    orders = supabase.table("orders").select("status, quantity, products(price)").execute().data or []
    completed = [o for o in orders if o["status"] == "completed"]
    pending = [o for o in orders if o["status"] == "pending"]
    revenue = sum(o["products"]["price"] * o["quantity"] for o in completed if o.get("products"))
    
    text = (
        "📊 <b>THỐNG KÊ SHOP</b>\n\n"
        f"💰 Tổng doanh thu: <b>{revenue:,}đ</b>\n"
        f"✅ Đơn hoàn thành: <b>{len(completed)}</b>\n"
        f"⏳ Đơn đang chờ: <b>{len(pending)}</b>\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_broadcast(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2: return await update.message.reply_text("Cú pháp: /broadcast nội dung")
    users = supabase.table("bot_users").select("user_id").execute().data or []
    ok = fail = 0
    for u in users:
        try: await context.bot.send_message(u["user_id"], parts[1]); ok += 1
        except: fail += 1
    await update.message.reply_text(f"📣 Thành công: {ok} | Lỗi: {fail}")

async def cmd_maintenance(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1 or context.args[0] not in ["on", "off"]: return await update.message.reply_text("Cú pháp: /maintenance on/off")
    val = "true" if context.args[0] == "on" else "false"
    supabase.table("settings").upsert({"key": "maintenance", "value": val}).execute()
    await update.message.reply_text(f"✅ Đã {'BẬT' if val=='true' else 'TẮT'} bảo trì.")

async def cmd_addcoupon(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=1)
    data = [x.strip() for x in parts[1].split("|")] if len(parts) > 1 else []
    if len(data) < 3 or data[1] not in ["percent", "fixed"]:
        return await update.message.reply_text("Cú pháp: /addcoupon Mã | percent/fixed | Số_giảm\nVD: /addcoupon TET | percent | 10")
    supabase.table("coupons").upsert({"code": data[0].upper(), "discount_type": data[1], "discount_value": int(data[2]), "is_active": True}).execute()
    await update.message.reply_text(f"✅ Đã tạo mã {data[0].upper()} giảm {data[2]} {'%' if data[1]=='percent' else 'đ'}")

async def cmd_find(update, context):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: return
    res = supabase.table("orders").select("*, products(name)").eq("id", int(context.args[0])).execute()
    if not res.data: return await update.message.reply_text("❌ Không tìm thấy đơn.")
    o = res.data[0]
    text = f"🔍 <b>Đơn #{o['id']}</b>\nKhách: <code>{o['user_id']}</code>\nSP: {o['products']['name']} (SL: {o['quantity']})\nTrạng thái: {o['status']}\nKey: <code>{o.get('key_code', 'Chưa có')}</code>"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_addmoney(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2 or not context.args[1].replace("-", "").isdigit():
        return await update.message.reply_text("Cú pháp: /addmoney ID Số_tiền\nVD: /addmoney 12345 50000")
    user_id = int(context.args[0]); amount = int(context.args[1])
    res = supabase.table("bot_users").select("balance").eq("user_id", user_id).execute()
    if not res.data: return await update.message.reply_text("❌ Không tìm thấy user.")
    old_bal = res.data[0].get("balance", 0) or 0
    new_bal = old_bal + amount
    supabase.table("bot_users").update({"balance": new_bal}).eq("user_id", user_id).execute()
    try: await context.bot.send_message(user_id, f"💰 Số dư của bạn đã được Admin cập nhật: <b>{amount:+,}đ</b>\nSố dư hiện tại: <b>{new_bal:,}đ</b>", parse_mode="HTML")
    except: pass
    await update.message.reply_text(f"✅ Đã cập nhật số dư cho {user_id}. Số dư mới: {new_bal:,}đ")

# ==================== LUỒNG MUA HÀNG ====================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, data, user = update.callback_query, update.callback_query.data, update.callback_query.from_user

    if data == "show_products":
        text, kb = products_view(); await safe_edit(query, text, kb); await query.answer()
        return

    elif data.startswith("select_qty:"):
        parts = data.split(":"); p_id, qty = parts[1], int(parts[2])
        res = supabase.table("products").select("*").eq("id", p_id).execute()
        if not res.data: return await query.answer("SP không tồn tại!", show_alert=True)
        max_stock = stock_of(p_id)
        if max_stock <= 0: return await query.answer("Hết hàng!", show_alert=True)
        p = res.data[0]
        kb_rows = [[InlineKeyboardButton(f"✅ {q}" if q == qty else str(q), callback_data=f"select_qty:{p_id}:{q}") for q in [1,2,3,5] if q <= max_stock]]
        kb_rows.append([InlineKeyboardButton(f"🛒 Xác nhận mua {qty} ({p['price']*qty:,}đ)", callback_data=f"buy:{p_id}:{qty}")])
        kb_rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="show_products")])
        await safe_edit(query, f"Sản phẩm: <b>{esc(p['name'])}</b>\nTồn: {max_stock}\nChọn SL: <b>{qty}</b>", InlineKeyboardMarkup(kb_rows))
        await query.answer()
        return

    elif data.startswith("buy:"):
        parts = data.split(":"); p_id, qty = parts[1], int(parts[2])
        res = supabase.table("products").select("*").eq("id", p_id).execute()
        if not res.data or stock_of(p_id) < qty: return await query.answer("Hết hàng!", show_alert=True)
        p = res.data[0]
        total = p["price"] * qty
        
        # Tạo đơn hàng trong database
        order = supabase.table("orders").insert({"user_id": user.id, "product_id": p_id, "quantity": qty, "status": "pending", "total_amount": total}).execute().data[0]
        o_id = order["id"]
        
        try:
            # Gọi payOS để tạo link thanh toán
            payment_request = CreatePaymentLinkRequest(
                order_code=o_id,
                amount=total,
                description=f"Thanh toan DH{o_id}",
                cancel_url="https://t.me/fo4cauthu_bot", 
                return_url="https://t.me/fo4cauthu_bot"  
            )
            payment_link = await payos_client.payment_requests.create(payment_request)
            
            # Gửi tin nhắn text chứa nút thanh toán và nút xác nhận thủ công
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Thanh toán ngay", url=payment_link.checkout_url)],
                [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"check_paid:{o_id}")],
                [InlineKeyboardButton("⬅️ Quay lại", callback_data="show_products")]
            ])
            
            await query.message.edit_text(
                f"✅ Đã tạo đơn hàng <b>#{o_id}</b>\n\n"
                f"SP: <b>{esc(p['name'])}</b> (SL: <b>{qty}</b>)\n"
                f"Tổng tiền: <b>{total:,}đ</b>\n\n"
                f"Vui lòng bấm nút bên dưới để thanh toán qua payOS.",
                reply_markup=kb,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Lỗi tạo link payOS: {e}")
            await query.message.edit_text(f"❌ Có lỗi xảy ra: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        
        await query.answer()
        return

    # ==================== XỬ LÝ KHI KHÁCH BẤM "TÔI ĐÃ CHUYỂN KHOẢN" ====================
    elif data.startswith("check_paid:"):
        o_id = int(data.split(":")[1])
        
        # Thông báo cho khách là đang kiểm tra
        await query.answer("Đang kiểm tra giao dịch với payOS...", show_alert=True)
        
        try:
            # Gọi API payOS để lấy thông tin thanh toán của đơn hàng này
            # Sử dụng phương thức getPaymentLinkInformation của SDK
            payment_info = await payos_client.getPaymentLinkInformation(order_id=o_id)
            
            # Kiểm tra trạng thái thanh toán (status thường là "PAID" nếu thành công)
            if payment_info.status == "PAID":
                # Nếu thành công, xử lý nhả key (tương tự logic trong webhook)
                o_res = supabase.table("orders").select("*, products(name)").eq("id", o_id).eq("status", "pending").execute()
                if o_res.data:
                    o = o_res.data[0]
                    req_qty = o.get("quantity", 1)
                    
                    # Lấy key từ kho
                    k_res = supabase.table("keys").select("id, key_code").eq("product_id", o["product_id"]).eq("is_sold", False).limit(req_qty).execute()
                    if k_res.data and len(k_res.data) >= req_qty:
                        keys = []
                        for k in k_res.data:
                            supabase.table("keys").update({"is_sold": True}).eq("id", k["id"]).eq("is_sold", False).execute()
                            keys.append(k["key_code"])
                        
                        all_keys_str = "\n".join(keys)
                        supabase.table("orders").update({"status": "completed", "key_code": all_keys_str}).eq("id", o_id).execute()
                        
                        # Gửi key cho khách
                        await context.bot.send_message(
                            o["user_id"], 
                            f"🎉 Thanh toán thành công! Đơn hàng #{o_id}\nKey của bạn:\n" + "\n".join([f"<code>{k}</code>" for k in keys]), 
                            parse_mode="HTML"
                        )
                        await query.message.edit_text(f"✅ Đơn hàng #{o_id} đã được xác nhận thanh toán và đã gửi key.", reply_markup=None)
                    else:
                        await query.message.edit_text(f"⚠️ Đơn hàng #{o_id} đã thanh toán nhưng kho tạm hết key. Vui lòng liên hệ Admin.", reply_markup=None)
                else:
                    await query.message.edit_text(f"✅ Đơn hàng #{o_id} đã được xử lý trước đó.", reply_markup=None)
            else:
                # Nếu chưa thanh toán
                await query.message.edit_text(
                    f"⏳ Hệ thống payOS báo đơn hàng <b>#{o_id}</b> chưa thanh toán thành công.\n"
                    f"Vui lòng kiểm tra lại giao dịch hoặc thử lại sau ít phút.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Thử lại", callback_data=f"check_paid:{o_id}")]])
                )
        except Exception as e:
            logger.error(f"Lỗi kiểm tra trạng thái thanh toán: {e}")
            await query.message.edit_text(f"❌ Có lỗi xảy ra khi kiểm tra giao dịch. Vui lòng thử lại sau.", reply_markup=None)

    elif data == "admin_panel":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        text, kb = admin_panel_view(); await safe_edit(query, text, kb); await query.answer()
    
    elif data == "admin_stats":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        await query.answer("Vui lòng gõ lệnh /stats để xem báo cáo chi tiết!", show_alert=True)

    elif data == "admin_coupon_help":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        await safe_edit(query, "🎟 <b>Tạo mã giảm giá:</b>\n<code>/addcoupon Mã | percent/fixed | Số_giảm</code>\n\nVD: <code>/addcoupon TET | percent | 10</code>", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]))
        await query.answer()

# ==================== WEBHOOK PAYOS ====================
@app.post("/payos-webhook")
async def payos_webhook(request: Request):
    logger.info("--- WEBHOOK ĐÃ ĐƯỢC GỌI ---")
    try:
        body = await request.body()
        
        # Đã thêm await ở đây để sửa lỗi 'coroutine' object has no attribute 'code'
        webhook_data = await payos_client.webhooks.verify(body)
        
        # Kiểm tra mã code thành công
        if webhook_data.code == "00":
            # Lấy order_code từ object data
            order_code = webhook_data.data.orderCode
            logger.info(f"Nhận thanh toán thành công cho đơn hàng: {order_code}")
            
            # Lấy thông tin đơn hàng
            o_res = supabase.table("orders").select("*, products(name)").eq("id", order_code).eq("status", "pending").execute()
            if not o_res.data:
                logger.warning(f"Không tìm thấy đơn {order_code} hoặc đã xử lý")
                return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})
            
            o = o_res.data[0]
            req_qty = o.get("quantity", 1)
            
            # Lấy key từ kho
            k_res = supabase.table("keys").select("id, key_code").eq("product_id", o["product_id"]).eq("is_sold", False).limit(req_qty).execute()
            if not k_res.data or len(k_res.data) < req_qty:
                logger.error(f"Không đủ key cho đơn hàng {order_code}!")
                return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})
            
            keys = []
            for k in k_res.data:
                supabase.table("keys").update({"is_sold": True}).eq("id", k["id"]).eq("is_sold", False).execute()
                keys.append(k["key_code"])
            
            all_keys_str = "\n".join(keys)
            supabase.table("orders").update({"status": "completed", "key_code": all_keys_str}).eq("id", order_code).execute()
            
            # Gửi key cho khách
            try:
                await telegram_app.bot.send_message(
                    o["user_id"], 
                    f"🎉 Thanh toán thành công! Đơn hàng #{order_code}\nKey của bạn:\n" + "\n".join([f"<code>{k}</code>" for k in keys]), 
                    parse_mode="HTML"
                )
                logger.info(f"Đã gửi key cho user {o['user_id']}")
            except Exception as e:
                logger.error(f"Không thể gửi tin nhắn Telegram: {e}")
        
        # Luôn trả về 200 OK để payOS biết là đã nhận thành công
        return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})
        
    except Exception as e:
        logger.error(f"Lỗi xử lý webhook: {e}")
        # Kể cả khi code có lỗi, vẫn phải trả về 200 để payOS không đánh dấu webhook bị lỗi
        return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})

# ==================== ĐĂNG KÝ & CHẠY ====================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("help", cmd_help))
telegram_app.add_handler(CommandHandler("addprod", cmd_addprod))
telegram_app.add_handler(CommandHandler("setprice", cmd_setprice))
telegram_app.add_handler(CommandHandler("setname", cmd_setname))
telegram_app.add_handler(CommandHandler("delprod", cmd_delprod))
telegram_app.add_handler(CommandHandler("addkey", cmd_addkey))
telegram_app.add_handler(CommandHandler("stock", cmd_stock))
telegram_app.add_handler(CommandHandler("stats", cmd_stats))
telegram_app.add_handler(CommandHandler("find", cmd_find))
telegram_app.add_handler(CommandHandler("addcoupon", cmd_addcoupon))
telegram_app.add_handler(CommandHandler("maintenance", cmd_maintenance))
telegram_app.add_handler(CommandHandler("broadcast", cmd_broadcast))
telegram_app.add_handler(CommandHandler("addmoney", cmd_addmoney))
telegram_app.add_handler(MessageHandler(filters.Text([BTN_SHOP]), msg_shop))
telegram_app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]), msg_admin))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))

@app.on_event("startup")
async def startup_event():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

@app.on_event("shutdown")
async def shutdown_event():
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.get("/")
def health(): return {"status": "ok"}
