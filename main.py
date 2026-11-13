import os
import html
import logging
import datetime
import asyncio
import json
import hmac
import hashlib
import httpx

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

# ==================== HÀM XỬ LÝ ĐƠN HÀNG (2 CHIỀU) ====================
async def process_order_success(order_code: int):
    # 1. Khóa đơn hàng bằng trạng thái processing
    update_res = supabase.table("orders").update({"status": "processing"}).eq("id", order_code).eq("status", "pending").execute()
    if not update_res.data: return False
    
    o = update_res.data[0]
    req_qty = o.get("quantity", 1)
    user_id = o["user_id"]
    
    p_res = supabase.table("products").select("name").eq("id", o["product_id"]).execute()
    p_name = p_res.data[0]["name"] if p_res.data else "Sản phẩm"
    
    u_res = supabase.table("bot_users").select("full_name").eq("user_id", user_id).execute()
    u_name = u_res.data[0]["full_name"] if u_res.data else "Không rõ tên"

    # 2. Lấy key
    k_res = supabase.table("keys").select("id, key_code").eq("product_id", o["product_id"]).eq("is_sold", False).limit(req_qty).execute()
    
    if not k_res.data or len(k_res.data) < req_qty:
        logger.error(f"Không đủ key cho đơn hàng {order_code}!")
        supabase.table("orders").update({"status": "pending"}).eq("id", order_code).execute()
        return False
    
    keys = []
    for k in k_res.data:
        supabase.table("keys").update({"is_sold": True}).eq("id", k["id"]).eq("is_sold", False).execute()
        keys.append(k["key_code"])
    
    keys_formatted = "\n".join([f"<code>{esc(k)}</code>" for k in keys])
    all_keys_str = "\n".join(keys)
    
    # 3. Cập nhật database thành công
    supabase.table("orders").update({"status": "completed", "key_code": all_keys_str}).eq("id", order_code).execute()
    
    # 4. Gửi key cho khách
    try:
        await telegram_app.bot.send_message(
            user_id, 
            f"🎉 Thanh toán thành công! Đơn hàng #{order_code}\nKey của bạn:\n{keys_formatted}", 
            parse_mode="HTML"
        )
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Không thể gửi Telegram cho khách: {e}")

    # 5. Gửi log cho Admin
    admin_log_msg = (
        f"✅ <b>ĐƠN HÀNG THANH TOÁN #{order_code}</b>\n\n"
        f"👤 Khách hàng: <b>{esc(u_name)}</b>\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💬 <a href='tg://user?id={user_id}'>Bấm vào đây để nhắn tin cho khách</a>\n"
        f"🎮 Sản phẩm: {esc(p_name)} (SL: {req_qty})\n"
        f"🔑 Key đã giao:\n{keys_formatted}"
    )
    for ad in ADMIN_IDS:
        if ad != 0:
            try: 
                await telegram_app.bot.send_message(ad, admin_log_msg, parse_mode="HTML")
                await asyncio.sleep(0.5)
            except Exception as e: 
                logger.error(f"Không thể gửi log cho admin {ad}: {e}")
    return True

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
    if maint: text += "⚠️ <b>SHOP ĐANG BẢO TRÌ!</b>\n\n"

    for p in products:
        stock = stock_of(p["id"])
        text += f"• <b>{esc(p['name'])}</b>\n💰 <code>{p['price']:,}đ</code> | {('✅ Còn ' + str(stock)) if stock > 0 else '❌ Hết hàng'}\n\n"
        if stock > 0 and not maint: 
            buttons.append([InlineKeyboardButton(f"Mua {p['name']}", callback_data=f"select_qty:{p['id']}:1")])
    return text, (InlineKeyboardMarkup(buttons) if buttons else None)

def admin_panel_view():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Thống kê", callback_data="admin_stats"), InlineKeyboardButton("📦 Tồn kho", callback_data="admin_stock")],
        [InlineKeyboardButton("🎟 Mã Giảm Giá", callback_data="admin_coupon_help")],
    ])
    return "🛠 <b>Admin Panel</b>\nGõ /help để xem toàn bộ lệnh.", kb

# ==================== LỆNH ADMIN ====================
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
        "<b>📦 QUẢN LÝ SẢN PHẨM & KHO</b>\n"
        "👉 <code>/addprod id | Tên | Giá | Mô tả</code>\n"
        "👉 <code>/setprice id Giá</code> | <code>/setname id Tên</code>\n"
        "👉 <code>/delprod id</code>\n"
        "👉 <code>/addkey id key1 key2...</code> (Tự chống trùng)\n"
        "👉 <code>/delkey key_code</code> (Xóa 1 key lỗi)\n"
        "👉 <code>/stock</code> (Xem tồn kho)\n\n"
        "<b>🧾 QUẢN LÝ ĐƠN HÀNG & KHÁCH</b>\n"
        "👉 <code>/sold</code> (Xem 10 đơn gần nhất)\n"
        "👉 <code>/find Mã_Đơn</code> (Xem chi tiết & nhắn tin)\n"
        "👉 <code>/check ID_Khách</code> (Xem lịch sử chi tiêu)\n"
        "👉 <code>/addmoney ID Số_tiền</code> (Cộng/trừ tiền)\n\n"
        "<b>⚙️ HỆ THỐNG</b>\n"
        "👉 <code>/stats</code> (Doanh thu tổng)\n"
        "👉 <code>/top</code> (Top sản phẩm bán chạy)\n"
        "👉 <code>/addcoupon Mã | percent/fixed | Số_giảm</code>\n"
        "👉 <code>/maintenance on/off</code> (Bảo trì)\n"
        "👉 <code>/broadcast Nội_dung</code> (Gửi tất cả khách)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# --- SẢN PHẨM & KHO ---
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
        p_id = parts[1]
        raw_keys = list(dict.fromkeys(parts[2:]))
        existing = supabase.table("keys").select("key_code").eq("product_id", p_id).execute().data
        existing_codes = {k["key_code"] for k in existing} if existing else set()
        new_keys = [k for k in raw_keys if k not in existing_codes]
        
        if not new_keys: return await update.message.reply_text("⚠️ Tất cả key này đã tồn tại trong kho!")
        supabase.table("keys").insert([{"product_id": p_id, "key_code": k, "is_sold": False} for k in new_keys]).execute()
        
        msg = f"✅ Đã thêm {len(new_keys)} key mới cho SP {p_id}."
        if len(raw_keys) > len(new_keys): msg += f"\n⚠️ Đã bỏ qua {len(raw_keys) - len(new_keys)} key bị trùng."
        await update.message.reply_text(msg)
    else: await update.message.reply_text("Cú pháp: /addkey id key1 key2...")

async def cmd_delkey(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: return await update.message.reply_text("Cú pháp: /delkey Mã_Key")
    key = context.args[0]
    res = supabase.table("keys").delete().eq("key_code", key).execute()
    if res.data:
        await update.message.reply_text(f"🗑 Đã xóa key <code>{esc(key)}</code> khỏi kho.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Không tìm thấy key này.")

async def cmd_stock(update, context):
    if not is_admin(update.effective_user.id): return
    products = supabase.table("products").select("*").execute().data or []
    text = "📦 <b>Tồn kho:</b>\n\n"
    for p in products: text += f"• {esc(p['name'])}: <b>{stock_of(p['id'])}</b>\n"
    await update.message.reply_text(text, parse_mode="HTML")

# --- ĐƠN HÀNG & KHÁCH ---
async def cmd_sold(update, context):
    if not is_admin(update.effective_user.id): return
    res = supabase.table("orders").select("*, products(name)").eq("status", "completed").order("created_at", desc=True).limit(10).execute()
    if not res.data: return await update.message.reply_text("Chưa có đơn hàng nào được bán.")
        
    text = "🧾 <b>10 ĐƠN HÀNG GẦN NHẤT</b>\n\n"
    for o in res.data:
        u_res = supabase.table("bot_users").select("full_name").eq("user_id", o["user_id"]).execute()
        u_name = u_res.data[0]["full_name"] if u_res.data else "N/A"
        keys_list = o.get('key_code', '').split('\n')
        keys_preview = f"<code>{esc(keys_list[0])}</code>" if keys_list else "Không có"
        if len(keys_list) > 1: keys_preview += f" (+{len(keys_list)-1} key)"
        
        text += f"<b>Đơn #{o['id']}</b> - {esc(u_name)}\n└ SP: {esc(o['products']['name'])}\n└ Key: {keys_preview}\n\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_find(update, context):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: return
    try: o_id = int(context.args[0])
    except: return await update.message.reply_text("Mã đơn phải là số.")
        
    res = supabase.table("orders").select("*, products(name)").eq("id", o_id).execute()
    if not res.data: return await update.message.reply_text("❌ Không thấy đơn.")
    o = res.data[0]
    
    u_res = supabase.table("bot_users").select("full_name").eq("user_id", o["user_id"]).execute()
    u_name = u_res.data[0]["full_name"] if u_res.data else "N/A"
    
    text = (
        f"🔍 <b>THÔNG TIN ĐƠN HÀNG #{o['id']}</b>\n\n"
        f"👤 Khách: <b>{esc(u_name)}</b>\n"
        f"🆔 ID: <code>{o['user_id']}</code>\n"
        f"🎮 SP: {esc(o['products']['name'])} (SL: {o['quantity']})\n"
        f"💰 Tổng: {o.get('total_amount', 0):,}đ\n"
        f"📅 Ngày: {o['created_at'][:16]}\n"
        f"📌 Trạng thái: <b>{o['status']}</b>\n\n"
    )
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Nhắn tin cho khách", url=f"tg://user?id={o['user_id']}")]])
    
    if o.get('key_code'):
        keys_split = o['key_code'].split('\n')
        text += f"🔑 Key đã giao:\n" + "\n".join([f"<code>{esc(k)}</code>" for k in keys_split])
        if len(keys_split) > 5:
            text = text.replace("🔑 Key đã giao:\n", "🔑 Key đã giao (5 key đầu):\n")
            text = "\n".join(text.split('\n')[:14]) + "\n<i>... (Còn nữa)</i>"
    
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def cmd_check(update, context):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: return
    try: user_id = int(context.args[0])
    except: return await update.message.reply_text("ID phải là số.")
    
    u_res = supabase.table("bot_users").select("*").eq("user_id", user_id).execute()
    if not u_res.data: return await update.message.reply_text("❌ Không tìm thấy user này.")
    u = u_res.data[0]
    
    o_res = supabase.table("orders").select("total_amount").eq("user_id", user_id).eq("status", "completed").execute()
    total_spent = sum(o.get("total_amount", 0) for o in o_res.data) if o_res.data else 0
    total_orders = len(o_res.data) if o_res.data else 0
    
    text = (
        f"👤 <b>THÔNG TIN KHÁCH HÀNG</b>\n\n"
        f"Tên: <b>{esc(u.get('full_name', 'N/A'))}</b>\n"
        f"ID: <code>{u['user_id']}</code>\n"
        f"💰 Số dư: <b>{u.get('balance', 0):,}đ</b>\n"
        f"🧾 Tổng đơn đã mua: <b>{total_orders}</b>\n"
        f"💸 Tổng chi tiêu: <b>{total_spent:,}đ</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Nhắn tin", url=f"tg://user?id={user_id}")]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def cmd_addmoney(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2 or not context.args[1].replace("-", "").isdigit(): return
    user_id = int(context.args[0]); amount = int(context.args[1])
    res = supabase.table("bot_users").select("balance").eq("user_id", user_id).execute()
    if not res.data: return
    new_bal = (res.data[0].get("balance", 0) or 0) + amount
    supabase.table("bot_users").update({"balance": new_bal}).eq("user_id", user_id).execute()
    await update.message.reply_text(f"✅ Số dư mới của {user_id}: {new_bal:,}đ")

# --- HỆ THỐNG ---
async def cmd_stats(update, context):
    if not is_admin(update.effective_user.id): return
    orders = supabase.table("orders").select("status, quantity, total_amount").execute().data or []
    completed = [o for o in orders if o["status"] == "completed"]
    pending = [o for o in orders if o["status"] == "pending"]
    revenue = sum(o.get("total_amount", 0) for o in completed)
    
    text = f"📊 <b>THỐNG KÊ TỔNG QUAN</b>\n\n💰 Doanh thu: <b>{revenue:,}đ</b>\n✅ Đơn hoàn thành: <b>{len(completed)}</b>\n⏳ Đơn đang chờ: <b>{len(pending)}</b>"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_top(update, context):
    if not is_admin(update.effective_user.id): return
    res = supabase.table("orders").select("product_id, quantity, products(name)").eq("status", "completed").execute()
    if not res.data: return await update.message.reply_text("Chưa có dữ liệu bán hàng.")
    
    counts = {}
    for o in res.data:
        p_name = o["products"]["name"]
        counts[p_name] = counts.get(p_name, 0) + o["quantity"]
    
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
    text = "🏆 <b>TOP 5 SẢN PHẨM BÁN CHẠY</b>\n\n"
    for i, (name, qty) in enumerate(sorted_counts, 1):
        text += f"{i}. <b>{esc(name)}</b>: {qty} key\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_addcoupon(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=1)
    data = [x.strip() for x in parts[1].split("|")] if len(parts) > 1 else []
    if len(data) < 3: return
    supabase.table("coupons").upsert({"code": data[0].upper(), "discount_type": data[1], "discount_value": int(data[2]), "is_active": True}).execute()
    await update.message.reply_text(f"✅ Tạo mã {data[0]} thành công")

async def cmd_maintenance(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1 or context.args[0] not in ["on", "off"]: return
    val = "true" if context.args[0] == "on" else "false"
    supabase.table("settings").upsert({"key": "maintenance", "value": val}).execute()
    await update.message.reply_text(f"✅ Bảo trì: {val}")

async def cmd_broadcast(update, context):
    if not is_admin(update.effective_user.id): return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2: return
    users = supabase.table("bot_users").select("user_id").execute().data or []
    ok = 0
    for u in users:
        try: await context.bot.send_message(u["user_id"], parts[1]); ok += 1
        except: pass
    await update.message.reply_text(f"📣 Đã gửi: {ok}")

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
        
        order = supabase.table("orders").insert({"user_id": user.id, "product_id": p_id, "quantity": qty, "status": "pending", "total_amount": total}).execute().data[0]
        o_id = order["id"]
        
        try:
            payment_request = CreatePaymentLinkRequest(
                order_code=o_id,
                amount=total,
                description=f"Thanh toan DH{o_id}",
                cancel_url="https://t.me/fo4cauthu_bot", 
                return_url="https://t.me/fo4cauthu_bot"  
            )
            payment_link = await payos_client.payment_requests.create(payment_request)
            
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

    elif data.startswith("check_paid:"):
        o_id = int(data.split(":")[1])
        await query.answer("Đang kiểm tra giao dịch với payOS...", show_alert=True)
        
        try:
            async with httpx.AsyncClient() as client:
                headers = {"x-client-id": PAYOS_CLIENT_ID, "x-api-key": PAYOS_API_KEY}
                res = await client.get(f"https://api-merchant.payos.vn/v2/payment-requests/{o_id}", headers=headers)
                res_data = res.json()
                
                if res_data.get("code") == "00" and res_data.get("data", {}).get("status") == "PAID":
                    success = await process_order_success(o_id)
                    if success:
                        await query.message.edit_text(f"✅ Đơn hàng #{o_id} đã được xác nhận thanh toán và đã gửi key.", reply_markup=None)
                    else:
                        await query.message.edit_text(f"⚠️ Đã thanh toán nhưng đang hết key hoặc đơn đang được xử lý. Vui lòng báo Admin!", reply_markup=None)
                else:
                    await query.message.edit_text(
                        f"⏳ Hệ thống payOS báo đơn hàng <b>#{o_id}</b> chưa thanh toán thành công.\nVui lòng chuyển khoản thành công rồi thử lại.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Thử lại", callback_data=f"check_paid:{o_id}")]])
                    )
        except Exception as e:
            logger.error(f"Lỗi kiểm tra thanh toán: {e}")
            await query.message.edit_text(f"❌ Lỗi kết nối kiểm tra. Vui lòng thử lại sau.", reply_markup=None)

    elif data == "admin_panel":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        text, kb = admin_panel_view(); await safe_edit(query, text, kb); await query.answer()
    
    elif data == "admin_stats":
        if not is_admin(user.id): return
        await query.answer("Vui lòng gõ /stats!", show_alert=True)

    elif data == "admin_coupon_help":
        if not is_admin(user.id): return
        await safe_edit(query, "🎟 <code>/addcoupon Mã | percent | Số_giảm</code>", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]))
        await query.answer()

# ==================== WEBHOOK PAYOS ====================
@app.post("/payos-webhook")
async def payos_webhook(request: Request):
    logger.info("--- WEBHOOK ĐÃ ĐƯỢC GỌI ---")
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        data = json.loads(body_str)
        
        signature = data.get("signature")
        if signature:
            data_to_check = {k: v for k, v in data.items() if k not in ["signature", "code", "desc"]}
            sorted_data = sorted(data_to_check.items())
            query_str = "&".join(f"{k}={v}" for k, v in sorted_data)
            expected_sig = hmac.new(PAYOS_CHECKSUM_KEY.encode(), query_str.encode(), hashlib.sha256).hexdigest()
            if expected_sig != signature:
                logger.error("Sai chữ ký Webhook!")
                return JSONResponse(status_code=200, content={"code": "00", "desc": "success"}) 
        
        if data.get("code") == "00":
            order_code = data.get("data", {}).get("orderCode")
            logger.info(f"Nhận thanh toán thành công cho đơn hàng: {order_code}")
            await process_order_success(order_code)
        
        return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})
    except Exception as e:
        logger.error(f"Lỗi xử lý webhook: {e}")
        return JSONResponse(status_code=200, content={"code": "00", "desc": "success"})

# ==================== ĐĂNG KÝ & CHẠY ====================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("help", cmd_help))
telegram_app.add_handler(CommandHandler("addprod", cmd_addprod))
telegram_app.add_handler(CommandHandler("setprice", cmd_setprice))
telegram_app.add_handler(CommandHandler("setname", cmd_setname))
telegram_app.add_handler(CommandHandler("delprod", cmd_delprod))
telegram_app.add_handler(CommandHandler("addkey", cmd_addkey))
telegram_app.add_handler(CommandHandler("delkey", cmd_delkey)) # Lệnh mới
telegram_app.add_handler(CommandHandler("stock", cmd_stock))
telegram_app.add_handler(CommandHandler("stats", cmd_stats))
telegram_app.add_handler(CommandHandler("top", cmd_top)) # Lệnh mới
telegram_app.add_handler(CommandHandler("sold", cmd_sold))
telegram_app.add_handler(CommandHandler("find", cmd_find))
telegram_app.add_handler(CommandHandler("check", cmd_check)) # Lệnh mới
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
