import telebot
import os
import time
from threading import Thread
from flask import Flask
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timedelta

# --- CONFIGURATION ---
# ပတ်ဝန်းကျင် Variable များမှ ဖတ်ယူခြင်း
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0)) # သင့် ID ကို integer အနေနဲ့ ထည့်ပါ
MONGO_URL = os.getenv('MONGO_URL')

bot = telebot.TeleBot(BOT_TOKEN)

try:
    client = MongoClient(MONGO_URL, tlsAllowInvalidCertificates=True)
    db = client['multichannel_autopost_db']
    channels_col = db['channels']
    posts_col = db['posts']
    # Speed အတွက် Index ပေးခြင်း
    posts_col.create_index([("channel_id", ASCENDING), ("is_posted", ASCENDING), ("msg_id", ASCENDING)])
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")

# --- FLASK WEB SERVER (Keep Alive) ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running... 🚀"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# --- HELPERS ---
def save_post(channel_id, message_id):
    c_id = str(channel_id)
    # Post ရှိမရှိ အရင်စစ်ပြီးမှ အသစ်သွင်းမယ်
    if not posts_col.find_one({"channel_id": c_id, "msg_id": message_id}):
        posts_col.insert_one({
            "channel_id": c_id, 
            "msg_id": message_id, 
            "is_posted": False,
            "created_at": datetime.utcnow()
        })
        print(f"📥 Saved: Channel {c_id} | Msg {message_id}")

def get_next_post(channel_id):
    c_id = str(channel_id)
    # အဟောင်းဆုံး unposted post ကို ရှာမယ်
    post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
    
    if not post:
        # အားလုံး တင်ပြီးသွားရင် အစကပြန်စဖို့ Reset လုပ်မယ်
        count = posts_col.count_documents({"channel_id": c_id})
        if count > 0:
            posts_col.update_many({"channel_id": c_id}, {"$set": {"is_posted": False}})
            post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
            print(f"🔄 Loop Reset for Channel: {c_id}")
    return post

# --- HANDLERS ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Bot is active! Use /add_channel [ID] to register.")

@bot.message_handler(commands=['add_channel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ You are not authorized.")
    
    try:
        t_id = message.text.split()[1]
        # Channel ID သည် -100 နှင့် စရမည်
        channels_col.update_one(
            {"channel_id": str(t_id)}, 
            {"$set": {"active": True, "peak_hours": [9, 12, 15, 18, 21], "batch_size": 1}}, 
            upsert=True
        )
        bot.reply_to(message, f"✅ Channel {t_id} register လုပ်ပြီးပါပြီ။")
        print(f"➕ New Channel Added: {t_id}")
    except Exception as e: 
        bot.reply_to(message, "Usage: /add_channel -100123456789")

@bot.channel_post_handler(content_types=['video', 'photo', 'document', 'text'])
def auto_fetch(message):
    # Bot ကိုယ်တိုင် ပြန်ပို့တာကို မသိမ်းအောင် စစ်မယ် (Loop မဖြစ်အောင်)
    if message.forward_from_chat or message.forward_from:
        return

    c_id = str(message.chat.id)
    # Register လုပ်ထားတဲ့ channel ဟုတ်မဟုတ် database မှာ စစ်မယ်
    if channels_col.find_one({"channel_id": c_id, "active": True}):
        save_post(c_id, message.message_id)

# --- ENGINE (BACKGROUND WORKER) ---
def worker_loop():
    last_h = -1
    print("🤖 Worker Loop Started...")
    while True:
        # မြန်မာစံတော်ချိန် ချိန်ညှိခြင်း (UTC+6:30)
        now = datetime.utcnow() + timedelta(hours=6, minutes=30)
        curr_h, curr_m = now.hour, now.minute

        # မိနစ် 0 မှာ အလုပ်လုပ်မယ်၊ တစ်နာရီ တစ်ကြိမ်ပဲ လုပ်မယ်
        if curr_h != last_h and curr_m == 0:
            active_channels = list(channels_col.find({"active": True}))
            
            for ch in active_channels:
                if curr_h in ch.get('peak_hours', []):
                    c_id = ch['channel_id']
                    batch = ch.get('batch_size', 1)
                    
                    print(f"⏰ Peak Hour Reached for {c_id}. Sending {batch} post(s)...")
                    
                    for _ in range(batch):
                        post = get_next_post(c_id)
                        if post:
                            try:
                                # မူရင်း Channel ထဲကိုပဲ Forward ပြန်လုပ်မယ်
                                bot.forward_message(c_id, c_id, post['msg_id'])
                                posts_col.update_one({"_id": post['_id']}, {"$set": {"is_posted": True}})
                                time.sleep(3) # Telegram Flood limit မမိအောင်
                            except Exception as e:
                                print(f"❌ Error forwarding for {c_id}: {e}")
            
            last_h = curr_h
        
        time.sleep(30) # ၃၀ စက္ကန့်တစ်ခါ loop ပတ်ပြီး အချိန်စစ်မယ်

if __name__ == "__main__":
    # Background Thread များ စတင်ခြင်း
    Thread(target=run_http).start()
    Thread(target=worker_loop, daemon=True).start()
    
    print("🚀 Bot is Polling...")
    bot.infinity_polling()
