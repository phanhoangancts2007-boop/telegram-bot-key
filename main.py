import os
import html
import logging
from io import BytesIO
import datetime
import asyncio

import qrcode

from fastapi import FastAPI
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==================== BIẾN MÔI TRƯỜNG ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_RAW.split(",") if id.strip().isdigit()]

# Tài khoản mặc định (nếu không ai nhận đơn)
BANK_NAME = os.environ.get("BANK_NAME", "MB Bank")
ACCOUNT_NUMBER = os.environ.get("ACCOUNT_NUMBER", "0352836546")
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "Phan Hoang An")
BANK_BIN = os.environ.get("BANK_BIN", "970422")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Bộ nhớ tạm lưu các đơn đang chờ Admin nhận
pending_orders = {}

# ==================== HÀM CƠ BẢN ====================
def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS
def esc(s) -> str: return html.escape(str(s or ""))

def _tlv(tag, val): return f"{tag}{len(val):02d}{val}"
def _crc16(data):
    crc = 0xFFFF
    for b in data.encode():
        crc ^= b << 8
        for _ in range(8): crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        crc &= 0xFFFF
    return f"{crc:04X}"

def vietqr_payload(amount, content, bank_bin, acc_num):
    bank = _tlv("00", bank_bin) + _tlv("01", acc_num)
    merchant = _tlv("00", "A000000727") + _tlv("01", bank) + _tlv("02", "QRIBFTTA")
    p = _tlv("00", "01") + _tlv("01", "12") + _tlv("38", merchant) + _tlv("53", "704") + _tlv("54", str(amount)) + _tlv("58", "VN") + _tlv("62", _tlv("08", content)) + "6304"
    return p + _crc16(p)

def make_qr_png(amount, content, bank_bin, acc_num):
    img = qrcode.make(vietqr_payload(amount, content, bank_bin, acc_num), border=2, box_size=8)
    bio = BytesIO(); img.save(bio, format="PNG"); bio.seek(0); bio.name = "qr.png"
    return bio

def stock_of(product_id):
    res = supabase.table("keys").select("id", count="exact").eq("product_id", product_id).eq("is_sold", False).execute()
    return res.count or 0

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
    for p in products:
        stock = stock_of(p["id"])
        text += f"• <b>{esc(p['name'])}</b>\n💰 <code>{p['price']:,}đ</code> | {('✅ Còn ' + str(stock)) if stock > 0 else '❌ Hết hàng'}\n\n"
        if stock > 0: buttons.append([InlineKeyboardButton(f"Mua {p['name']}", callback_data=f"select_qty:{p['id']}:1")])
    return text, (InlineKeyboardMarkup(buttons) if buttons else None)

def admin_panel_view():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Thêm Key", callback_data="admin_addkey")],
        [InlineKeyboardButton("📦 Tồn kho", callback_data="admin_stock")],
        [InlineKeyboardButton("📋 Đơn chờ", callback_data="admin_pending")],
        [InlineKeyboardButton("🏦 Cài Bank", callback_data="admin_setbank")],
    ])
    return "🛠 <b>Admin Panel</b>", kb

# ==================== LỆNH USER ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supabase.table("bot_users").upsert({"user_id": update.effective_user.id, "full_name": update.effective_user.full_name}).execute()
    await update.message.reply_text(f"Xin chào {esc(update.effective_user.first_name)}!", reply_markup=reply_menu(update.effective_user.id))

async def msg_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = products_view(); await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def msg_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        text, kb = admin_panel_view(); await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def cmd_setbank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    raw = update.message.text.split(maxsplit=1)
    parts = [x.strip() for x in raw[1].split("|")] if len(raw) > 1 else []
    if len(parts) < 4: return await update.message.reply_text("Cú pháp: /setbank Tên Bank | STK | Tên | BIN")
    supabase.table("admin_status").upsert({"user_id": update.effective_user.id, "bank_name": parts[0], "account_number": parts[1], "account_name": parts[2], "bank_bin": parts[3]}).execute()
    await update.message.reply_text("✅ Đã lưu Bank cá nhân!")

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

    # --- KHÁCH BẤM MUA ---
    elif data.startswith("buy:"):
        parts = data.split(":"); p_id, qty = parts[1], int(parts[2])
        res = supabase.table("products").select("*").eq("id", p_id).execute()
        if not res.data or stock_of(p_id) < qty: return await query.answer("Hết hàng!", show_alert=True)
        p = res.data[0]
        total = p["price"] * qty
        
        # Tạo đơn hàng
        order = supabase.table("orders").insert({"user_id": user.id, "product_id": p_id, "quantity": qty, "status": "pending"}).execute().data[0]
        o_id = order["id"]
        
        # Lưu tạm
        pending_orders[o_id] = {
            "user_id": user.id, "product_name": p["name"], 
            "quantity": qty, "total": total, "chosen_bank": None
        }

        # Gửi thông báo cho TẤT CẢ admin
        admin_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🏦 NHẬN ĐƠN NÀY (Mã #{o_id})", callback_data=f"claim:{o_id}")]])
        msg_text = f"🔔 <b>KHÁCH ĐANG CHỜ THANH TOÁN (Mã #{o_id})</b>\n\nSP: {esc(p['name'])} (SL: {qty})\nTổng: {total:,}đ\n\nAi bấm nút bên dưới để nhận đơn (Khách sẽ nhận QR của bạn). Hết 15s hệ thống lấy Bank mặc định!"
        
        for ad in ADMIN_IDS:
            if ad != 0:
                try: await context.bot.send_message(ad, msg_text, reply_markup=admin_kb, parse_mode="HTML")
                except: pass

        # Báo khách chờ 15s
        await query.message.edit_text(f"⏳ Đang kết nối với Admin. Vui lòng chờ tối đa 15 giây...", reply_markup=None)
        await query.answer()

        # Hẹn 15s sau check lại
        async def timeout_handler():
            await asyncio.sleep(15)
            if pending_orders.get(o_id) and pending_orders[o_id]["chosen_bank"] is None:
                await generate_and_send_qr(context, user.id, o_id, "Mặc định (Admin không phản hồi)")
        
        asyncio.create_task(timeout_handler())
        return

    # --- ADMIN BẤM NHẬN ĐƠN ---
    elif data.startswith("claim:"):
        o_id = int(data.split(":")[1])
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        
        if o_id not in pending_orders: return await query.answer("Đơn đã được xử lý hoặc quá hạn!", show_alert=True)
        order_data = pending_orders[o_id]
        if order_data["chosen_bank"] is not None: return await query.answer("Đã có Admin khác nhận đơn này rồi!", show_alert=True)

        # Lấy bank của Admin
        ad_info = supabase.table("admin_status").select("*").eq("user_id", user.id).execute().data
        if not ad_info or not ad_info[0].get("bank_name"): return await query.answer("Bạn chưa cài Bank! Gõ /setbank trước.", show_alert=True)

        # Chốt bank
        order_data["chosen_bank"] = ad_info[0]
        await query.message.edit_text(f"✅ Bạn đã nhận đơn #{o_id}!\nKhách đang được gửi QR thanh toán tới Bank của bạn.", reply_markup=None)
        
        # Gửi QR cho khách
        await generate_and_send_qr(context, order_data["user_id"], o_id, f"Bank của Admin {user.first_name}")
        await query.answer("Nhận đơn thành công!")

    # --- CÁC NÚT KHÁC ---
    elif data == "admin_panel":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        text, kb = admin_panel_view(); await safe_edit(query, text, kb); await query.answer()
    
    elif data == "admin_setbank":
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        await safe_edit(query, "🏦 Gõ: <code>/setbank Tên Bank | STK | Tên | BIN</code>", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="admin_panel")]]))
        await query.answer()

    elif data.startswith("paid:"):
        o_id = int(data.split(":")[1])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Duyệt #{o_id}", callback_data=f"approve:{o_id}"), InlineKeyboardButton("❌ Hủy", callback_data=f"reject:{o_id}")]])
        for ad in ADMIN_IDS:
            if ad != 0:
                try: await context.bot.send_message(ad, f"📢 <b>KHÁCH BÁO ĐÃ CHUYỂN KHOẢN</b>\nMã đơn: #{o_id}", reply_markup=kb, parse_mode="HTML")
                except: pass
        try: await query.message.edit_reply_markup(reply_markup=None)
        except: pass
        await query.message.reply_text("✅ Đã ghi nhận. Chờ Admin duyệt nhé!")
        await query.answer()

    elif data.startswith("approve:"):
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        o_id = int(data.split(":")[1])
        o_res = supabase.table("orders").select("*, products(name)").eq("id", o_id).eq("status", "pending").execute()
        if not o_res.data: return await query.answer("Đơn không tồn tại/đã xử lý", show_alert=True)
        o = o_res.data[0]
        req_qty = o.get("quantity", 1)
        k_res = supabase.table("keys").select("id, key_code").eq("product_id", o["product_id"]).eq("is_sold", False).limit(req_qty).execute()
        if not k_res.data or len(k_res.data) < req_qty: return await query.answer("Không đủ key!", show_alert=True)
        
        keys = []
        for k in k_res.data:
            if supabase.table("keys").update({"is_sold": True}).eq("id", k["id"]).eq("is_sold", False).execute().data: keys.append(k["key_code"])
        
        supabase.table("orders").update({"status": "completed", "key_code": "\n".join(keys)}).eq("id", o_id).execute()
        
        try: await context.bot.send_message(o["user_id"], f"🎉 Đơn #{o_id} duyệt!\nKey:\n" + "\n".join([f"<code>{k}</code>" for k in keys]), parse_mode="HTML")
        except: pass
        
        await query.message.edit_text(f"✅ Đã duyệt #{o_id}", reply_markup=None)
        await query.answer("Đã duyệt!")

    elif data.startswith("reject:"):
        if not is_admin(user.id): return await query.answer("Không có quyền!", show_alert=True)
        o_id = int(data.split(":")[1])
        res = supabase.table("orders").update({"status": "cancelled"}).eq("id", o_id).eq("status", "pending").execute()
        if res.data:
            try: await context.bot.send_message(res.data[0]["user_id"], f"❌ Đơn #{o_id} đã hủy.")
            except: pass
        await query.message.edit_text(f"❌ Đã hủy #{o_id}", reply_markup=None)
        await query.answer("Đã hủy!")

# ==================== HÀM TẠO & GỬI QR ====================
async def generate_and_send_qr(context: ContextTypes.DEFAULT_TYPE, customer_id, o_id, source_note):
    data = pending_orders.get(o_id)
    if not data: return

    bank_info = data["chosen_bank"]
    if bank_info:
        b_name, b_acc, b_acc_name, b_bin = bank_info["bank_name"], bank_info["account_number"], bank_info["account_name"], bank_info["bank_bin"]
    else:
        b_name, b_acc, b_acc_name, b_bin = BANK_NAME, ACCOUNT_NUMBER, ACCOUNT_NAME, BANK_BIN

    content = f"DH{o_id}"
    caption = (
        f"✅ <b>ĐƠN HÀNG #{o_id}</b>\n\n"
        f"SP: <b>{esc(data['product_name'])}</b> (SL: <b>{data['quantity']}</b>)\n"
        f"Tổng tiền: <b>{data['total']:,}đ</b>\n\n"
        f"💳 <b>Chuyển khoản tới:</b>\n"
        f"Ngân hàng: {esc(b_name)}\n"
        f"STK: <code>{esc(b_acc)}</code>\n"
        f"Chủ TK: {esc(b_acc_name)}\n"
        f"Nội dung: <code>{content}</code>\n\n"
        f"<i>({esc(source_note)})</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tôi đã chuyển khoản", callback_data=f"paid:{o_id}")],
        [InlineKeyboardButton("⬅️ Quay lại", callback_data="show_products")]
    ])
    
    try:
        await context.bot.send_photo(chat_id=customer_id, photo=make_qr_png(data["total"], content, b_bin, b_acc), caption=caption, reply_markup=kb, parse_mode="HTML")
    except:
        await context.bot.send_message(chat_id=customer_id, text=caption, reply_markup=kb, parse_mode="HTML")
    
    if o_id in pending_orders: del pending_orders[o_id]

# ==================== CÁC LỆNH ADMIN PHỤ ====================
async def cmd_addprod(update, context):
    raw = update.message.text.split(maxsplit=1)
    parts = [x.strip() for x in raw[1].split("|")] if len(raw) > 1 else []
    if len(parts) >= 3 and parts[2].replace(",", "").isdigit():
        supabase.table("products").upsert({"id": parts[0], "name": parts[1], "price": int(parts[2].replace(",", "")), "description": parts[3] if len(parts)>3 else ""}).execute()
        await update.message.reply_text(f"✅ Đã lưu SP {parts[0]}")

async def cmd_addkey(update, context):
    parts = update.message.text.split()
    if len(parts) >= 3:
        supabase.table("keys").insert([{"product_id": parts[1], "key_code": k, "is_sold": False} for k in list(dict.fromkeys(parts[2:]))]).execute()
        await update.message.reply_text(f"✅ Đã thêm {len(parts)-2} key.")

async def cmd_stock(update, context):
    products = supabase.table("products").select("*").execute().data or []
    text = "📦 Tồn kho:\n\n"
    for p in products: text += f"• {esc(p['name'])}: {stock_of(p['id'])}\n"
    await update.message.reply_text(text, parse_mode="HTML")

# ==================== ĐĂNG KÝ & CHẠY ====================
telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("setbank", cmd_setbank))
telegram_app.add_handler(CommandHandler("addprod", cmd_addprod))
telegram_app.add_handler(CommandHandler("addkey", cmd_addkey))
telegram_app.add_handler(CommandHandler("stock", cmd_stock))
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
