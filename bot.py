import telebot
import os
import time
from threading import Thread
from flask import Flask
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timedelta

# CONFIGURATION
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
MONGO_URL = os.getenv('MONGO_URL')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URL, tlsAllowInvalidCertificates=True)
db = client['multichannel_autopost_db']

channels_col = db['channels']
posts_col = db['posts']

# Database Indexing - ဒါက Channel အများကြီးရှိလာရင် speed အတွက် အရေးကြီးဆုံးပါ
posts_col.create_index([("channel_id", ASCENDING), ("is_posted", ASCENDING), ("msg_id", ASCENDING)])

app = Flask('')
@app.route('/')
def home(): return "Multi-Channel Bot is Live! 🚀"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# --- HELPERS ---
def save_post(channel_id, message_id):
    c_id = str(channel_id)
    if not posts_col.find_one({"channel_id": c_id, "msg_id": message_id}):
        posts_col.insert_one({
            "channel_id": c_id, 
            "msg_id": message_id, 
            "is_posted": False
        })

def get_next_post(channel_id):
    c_id = str(channel_id)
    # အဟောင်းဆုံး unposted post ကို ရှာမယ်
    post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
    
    if not post:
        # အားလုံး တင်ပြီးသွားရင် အစကပြန်စဖို့ Reset လုပ်မယ်
        posts_col.update_many({"channel_id": c_id}, {"$set": {"is_posted": False}})
        post = posts_col.find_one({"channel_id": c_id, "is_posted": False}, sort=[("msg_id", 1)])
    return post

# --- HANDLERS ---
@bot.message_handler(commands=['add_channel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        t_id = message.text.split()[1]
        channels_col.update_one({"channel_id": t_id}, {"$set": {"active": True, "peak_hours": [9, 12, 15, 18, 21], "batch_size": 1}}, upsert=True)
        bot.reply_to(message, f"✅ Channel {t_id} register လုပ်ပြီးပါပြီ။")
    except: bot.reply_to(message, "Usage: /add_channel [ID]")

@bot.channel_post_handler(content_types=['video', 'photo', 'document', 'text'])
def auto_fetch(message):
    # Register လုပ်ထားတဲ့ channel ဆိုရင် post အသစ်ကို auto သိမ်းမယ်
    if channels_col.find_one({"channel_id": str(message.chat.id), "active": True}):
        save_post(message.chat.id, message.message_id)

# --- ENGINE ---
def worker_loop():
    last_h = -1
    while True:
        now = datetime.utcnow() + timedelta(hours=6, minutes=30)
        curr_h, curr_m = now.hour, now.minute

        # မိနစ် 0 မှာ စတင်မယ် (ဒါမှမဟုတ် မိနစ် 0 ကို လွတ်သွားရင်လည်း အလုပ်လုပ်အောင် logic ပြင်ထားတယ်)
        if curr_h != last_h and curr_m == 0:
            active_channels = list(channels_col.find({"active": True}))
            
            for ch in active_channels:
                if curr_h in ch.get('peak_hours', []):
                    c_id = ch['channel_id']
                    batch = ch.get('batch_size', 1)
                    
                    for _ in range(batch):
                        post = get_next_post(c_id)
                        if post:
                            try:
                                bot.forward_message(c_id, c_id, post['msg_id'])
                                posts_col.update_one({"_id": post['_id']}, {"$set": {"is_posted": True}})
                                time.sleep(2) # Flood avoidance
                            except Exception as e:
                                print(f"Error for {c_id}: {e}")
            
            last_h = curr_h
        
        time.sleep(30)

if __name__ == "__main__":
    Thread(target=run_http).start()
    Thread(target=worker_loop, daemon=True).start()
    bot.infinity_polling()
