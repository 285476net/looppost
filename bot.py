import telebot
import os
import time
from threading import Thread
from flask import Flask
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timedelta

# CONFIGURATION
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID')) # Bot ပိုင်ရှင် (Super Admin)
MONGO_URL = os.getenv('MONGO_URL')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URL, tlsAllowInvalidCertificates=True)
db = client['pro_autopost_db']

channels_col = db['channels'] # {channel_id, owner_id, active, peak_hours, batch_size, expiry_date}
posts_col = db['posts']

# DB Indexing
posts_col.create_index([("channel_id", ASCENDING), ("is_posted", ASCENDING), ("msg_id", ASCENDING)])

app = Flask('')
@app.route('/')
def home(): return "Pro Autopost Engine is Running! 🚀"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# --- UTILS ---
def is_admin(user_id):
    return user_id == ADMIN_ID

def get_channel_owner(channel_id):
    ch = channels_col.find_one({"channel_id": str(channel_id)})
    return int(ch['owner_id']) if ch else None

# --- CORE FUNCTIONS ---
def get_next_post(channel_id):
    c_id = str(channel_id)
    post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
    if not post:
        posts_col.update_many({"channel_id": c_id}, {"$set": {"is_posted": False}})
        post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
    return post

# --- ADMIN COMMANDS (For You) ---
@bot.message_handler(commands=['add_owner'])
def add_owner(message):
    if not is_admin(message.from_user.id): return
    try:
        # /add_owner [ChannelID] [OwnerID] [Days]
        p = message.text.split()
        c_id, o_id, days = p[1], p[2], int(p[3])
        expiry = datetime.utcnow() + timedelta(days=days)
        
        channels_col.update_one(
            {"channel_id": c_id},
            {"$set": {
                "owner_id": int(o_id),
                "active": True,
                "peak_hours": [9, 13, 17, 21],
                "batch_size": 1,
                "expiry_date": expiry
            }}, upsert=True
        )
        bot.reply_to(message, f"✅ Added!\nOwner: {o_id}\nExpiry: {expiry.date()}")
    except: bot.reply_to(message, "Usage: /add_owner [ChannelID] [OwnerID] [Days]")

# --- OWNER COMMANDS (For Customers) ---
@bot.message_handler(commands=['setup'])
def setup_channel(message):
    try:
        p = message.text.split()
        c_id, hours_str, batch = p[1], p[2], int(p[3])
        
        if get_channel_owner(c_id) != message.from_user.id:
            return bot.reply_to(message, "❌ သင်သည် ဤ Channel ၏ ပိုင်ရှင်မဟုတ်ပါ။")
        
        hours = [int(h) for h in hours_str.split(',')]
        channels_col.update_one({"channel_id": c_id}, {"$set": {"peak_hours": hours, "batch_size": batch}})
        bot.reply_to(message, "✅ Setup အောင်မြင်ပါသည်။")
    except: bot.reply_to(message, "Usage: /setup [ChannelID] 9,12,15 1")

@bot.message_handler(commands=['status'])
def check_status(message):
    owner_channels = channels_col.find({"owner_id": message.from_user.id})
    res = "Your Channels:\n"
    for ch in owner_channels:
        res += f"ID: `{ch['channel_id']}`\nExpiry: {ch['expiry_date'].date()}\nActive: {ch['active']}\n---\n"
    bot.reply_to(message, res, parse_mode="Markdown")

@bot.message_handler(commands=['fetch'])
def fetch_posts(message):
    try:
        p = message.text.split()
        c_id, start, end = p[1], int(p[2]), int(p[3])
        if get_channel_owner(c_id) != message.from_user.id: return
        
        for m_id in range(start, end + 1):
            if not posts_col.find_one({"channel_id": c_id, "msg_id": m_id}):
                posts_col.insert_one({"channel_id": c_id, "msg_id": m_id, "is_posted": False})
        bot.reply_to(message, "📥 Post များသိမ်းဆည်းပြီးပါပြီ။")
    except: bot.reply_to(message, "Usage: /fetch [ChannelID] [Start] [End]")

# --- AUTO FETCH & ENGINE ---
@bot.channel_post_handler(content_types=['video', 'photo', 'document', 'text'])
def auto_save(message):
    ch = channels_col.find_one({"channel_id": str(message.chat.id), "active": True})
    if ch:
        if not posts_col.find_one({"channel_id": str(message.chat.id), "msg_id": message.message_id}):
            posts_col.insert_one({"channel_id": str(message.chat.id), "msg_id": message.message_id, "is_posted": False})

def worker_loop():
    while True:
        now = datetime.utcnow()
        mmt_now = now + timedelta(hours=6, minutes=30)
        curr_h, curr_m = mmt_now.hour, mmt_now.minute

        if curr_m == 0:
            active_channels = list(channels_col.find({"active": True}))
            for ch in active_channels:
                # သက်တမ်းစစ်မည်
                if now > ch['expiry_date']:
                    channels_col.update_one({"_id": ch['_id']}, {"$set": {"active": False}})
                    continue
                
                if curr_h in ch.get('peak_hours', []):
                    c_id = ch['channel_id']
                    for _ in range(ch.get('batch_size', 1)):
                        post = get_next_post(c_id)
                        if post:
                            try:
                                bot.forward_message(c_id, c_id, post['msg_id'])
                                posts_col.update_one({"_id": post['_id']}, {"$set": {"is_posted": True}})
                                time.sleep(2)
                            except: pass
            time.sleep(60)
        time.sleep(30)

if __name__ == "__main__":
    Thread(target=run_http).start()
    Thread(target=worker_loop, daemon=True).start()
    bot.infinity_polling()
