import os
from fastapi import FastAPI, Request
import requests
from supabase import create_client, Client

app = FastAPI()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/")
async def webhook(request: Request):
    data = await request.json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        if text == "/start":
            send_message(chat_id, "Chào mừng bạn! Bấm /getkey để lấy key.")
        elif text == "/getkey":
            res = supabase.table("keys").select("*").eq("is_used", False).limit(1).execute()
            if res.data:
                key_info = res.data[0]
                key_code = key_info["key_code"]

                supabase.table("keys").update({"is_used": True, "buyer_id": str(chat_id)}).eq("id", key_info["id"]).execute()

                send_message(chat_id, f"Key của bạn là: `{key_code}`")
            else:
                send_message(chat_id, "Rất tiếc, kho key hiện tại đã hết!")

    return {"status": "ok"}

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)
