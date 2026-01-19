import telebot
import os
import random
import time
import requests
from threading import Thread
from flask import Flask
from pymongo import MongoClient
# FIXED: timedelta ကို import ထဲမှာ ထည့်သွင်းထားသည်
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

# KEEP ALIVE SERVER
app = Flask('')
@app.route('/')
def home(): return "Bot is Fully Active with Multi-Schedule! 🚀"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

# ANTI-SLEEP (SELF-PING)
def self_ping():
    url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}.onrender.com"
    while True:
        try:
            requests.get(url)
            print(f"Pinged {url}")
        except: pass
        time.sleep(1200)

# DATABASE HELPERS
def add_new_post(channel_id, message_id):
    if not posts_col.find_one({"channel_id": str(channel_id), "msg_id": message_id}):
        posts_col.insert_one({"channel_id": str(channel_id), "msg_id": message_id, "is_posted": False})

def get_oldest_unposted(channel_id):
    oldest = posts_col.find_one({"channel_id": str(channel_id), "is_posted": False}, sort=[("msg_id", 1)])
    if not oldest:
        posts_col.update_many({"channel_id": str(channel_id)}, {"$set": {"is_posted": False}})
        oldest = posts_col.find_one({"channel_id": str(channel_id), "is_posted": False}, sort=[("msg_id", 1)])
    return oldest

# COMMANDS
@bot.message_handler(commands=['add_channel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = message.text.split()[1]
        channels_col.update_one({"channel_id": target_id}, {"$set": {"active": True}}, upsert=True)
        bot.reply_to(message, f"✅ Added {target_id}")
    except: bot.reply_to(message, "Usage: /add_channel [ID]")

@bot.message_handler(commands=['set_schedule'])
def set_schedule(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        parts = message.text.split()
        target_id, hours_str, batch = parts[1], parts[2], int(parts[3])
        hours = [int(h) for h in hours_str.split(',')]
        channels_col.update_one({"channel_id": target_id}, {"$set": {"peak_hours": hours, "batch_size": batch}})
        bot.reply_to(message, "✅ Schedule Set!")
    except: bot.reply_to(message, "Usage: /set_schedule [ID] 8,12,20 2")

@bot.message_handler(commands=['fetch'])
def fetch_posts(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        p = message.text.split()
        for m_id in range(int(p[2]), int(p[3]) + 1): add_new_post(p[1], m_id)
        bot.reply_to(message, "📥 Fetched!")
    except: bot.reply_to(message, "Usage: /fetch [ID] [Start] [End]")

@bot.channel_post_handler(content_types=['video', 'photo', 'document'])
def auto_save(message):
    if channels_col.find_one({"channel_id": str(message.chat.id), "active": True}):
        add_new_post(message.chat.id, message.message_id)

# NEW UPDATED LOOP (FIXED INDENTATION & LOGIC)
def auto_post_loop():
    while True:
        now = datetime.utcnow()
        mmt_now = now + timedelta(hours=6, minutes=30)
        current_hour = mmt_now.hour
        current_minute = mmt_now.minute

        if current_minute == 0:
            active_channels = channels_col.find({"active": True})
            for ch in active_channels:
                if current_hour in ch.get('peak_hours', []):
                    target_id = str(ch['channel_id'])
                    batch_size = ch.get('batch_size', 1)
                    
                    for _ in range(batch_size):
                        post = get_oldest_unposted(target_id) 
                        if post:
                            try:
                                bot.forward_message(chat_id=target_id, from_chat_id=target_id, message_id=post['msg_id'])
                                posts_col.update_one({"_id": post['_id']}, {"$set": {"is_posted": True}})
                                time.sleep(60) 
                            except Exception as e:
                                print(f"Error: {e}")
            
            # Batch အားလုံး ပို့ပြီးလျှင် အိပ်ခိုင်းမည်
            time.sleep(3300) 
        else:
            time.sleep(60)

if __name__ == "__main__":
    Thread(target=run_http).start()
    Thread(target=self_ping, daemon=True).start()
    Thread(target=auto_post_loop, daemon=True).start()
    bot.infinity_polling()
