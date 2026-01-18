import telebot
import os
import random
import time
from threading import Thread
from flask import Flask
from pymongo import MongoClient
from datetime import datetime

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
MONGO_URL = os.getenv('MONGO_URL')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URL, tlsAllowInvalidCertificates=True)
db = client['multichannel_autopost_db']

# Collections
channels_col = db['channels']  # ငှားထားသော Channel စာရင်း
posts_col = db['posts']        # Post IDs စာရင်း

# ==========================================
# WEB SERVER (KEEP ALIVE)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Multi-Channel AutoPost Bot is Running! 🚀"

def run_http():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

def keep_alive():
    t = Thread(target=run_http)
    t.start()

# ==========================================
# DATABASE HELPERS
# ==========================================

def add_new_post(channel_id, message_id):
    """Post အသစ်ကို DB ထဲထည့်သည် (Duplicate မဖြစ်အောင်စစ်သည်)"""
    if not posts_col.find_one({"channel_id": str(channel_id), "msg_id": message_id}):
        posts_col.insert_one({
            "channel_id": str(channel_id),
            "msg_id": message_id,
            "is_posted": False,
            "added_at": datetime.now()
        })

def get_random_unposted(channel_id):
    """မတင်ရသေးသော Post ထဲမှ Random တစ်ခုယူသည်"""
    unposted = list(posts_col.find({"channel_id": str(channel_id), "is_posted": False}))
    if not unposted:
        # အားလုံးတင်ပြီးသွားရင် Reset ပြန်လုပ်သည်
        posts_col.update_many({"channel_id": str(channel_id)}, {"$set": {"is_posted": False}})
        unposted = list(posts_col.find({"channel_id": str(channel_id), "is_posted": False}))
    
    return random.choice(unposted) if unposted else None

# ==========================================
# ADMIN COMMANDS (RENTAL SYSTEM)
# ==========================================

@bot.message_handler(commands=['add_channel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        # /add_channel -100xxxxx
        parts = message.text.split()
        target_id = parts[1]
        
        channels_col.update_one(
            {"channel_id": target_id},
            {"$set": {"active": True, "daily_limit": 10, "interval_min": 1}},
            upsert=True
        )
        bot.reply_to(message, f"✅ Channel `{target_id}` ကို စနစ်ထဲ ထည့်သွင်းပြီးပါပြီ။")
    except:
        bot.reply_to(message, "⚠️ Usage: `/add_channel [ChannelID]`")

@bot.message_handler(commands=['fetch'])
def fetch_posts(message):
    """Channel အဟောင်းထဲက post တွေကို scan ဖတ်ရန်"""
    if message.from_user.id != ADMIN_ID: return
    try:
        parts = message.text.split()
        channel_id = parts[1]
        start_id = int(parts[2])
        end_id = int(parts[3])
        
        count = 0
        for msg_id in range(start_id, end_id + 1):
            add_new_post(channel_id, msg_id)
            count += 1
            
        bot.reply_to(message, f"📥 Channel {channel_id} မှ Post {count} ခုကို စာရင်းသွင်းပြီးပါပြီ။")
    except:
        bot.reply_to(message, "⚠️ Usage: `/fetch [ChannelID] [StartID] [EndID]`")

# ==========================================
# AUTO FEEDER (NEW POSTS)
# ==========================================

@bot.channel_post_handler(content_types=['video', 'photo', 'document'])
def auto_save_new_post(message):
    """Channel ထဲမှာ Post အသစ်တင်တိုင်း DB ထဲ Auto မှတ်သည်"""
    channel_id = str(message.chat.id)
    # ခွင့်ပြုထားတဲ့ Channel ဟုတ်မဟုတ် စစ်သည်
    if channels_col.find_one({"channel_id": channel_id, "active": True}):
        add_new_post(channel_id, message.message_id)

# ==========================================
# CORE LOOP (AUTO POSTING)
# ==========================================

def auto_post_loop():
    while True:
        now = datetime.now()
        current_hour = now.hour
        
        # ၁။ အချိန်အပိုင်းအခြား (Time Slots) ပြင်ရန်
        # ဒီစာရင်းထဲက နာရီတွေရောက်မှ Bot က အလုပ်လုပ်ပါမယ်
        peak_hours = [8, 12, 18, 19, 20, 21, 22] 
        
        if current_hour in peak_hours:
            active_channels = channels_col.find({"active": True})
            for ch in active_channels:
                channel_id = ch['channel_id']
                
                # ၂။ တစ်ခါလုပ်လျှင် Post ဘယ်နှစ်ခု တင်မလဲ (Batch Size)
                # ဥပမာ - တစ်ခါတင်ရင် ၃ ပုဒ် ဆက်တိုက်တင်ချင်ရင်
                for _ in range(3): 
                    post = get_random_unposted(channel_id)
                    if post:
                        try:
                            bot.copy_message(channel_id, channel_id, post['msg_id'])
                            posts_col.update_one({"_id": post['_id']}, {"$set": {"is_posted": True}})
                            
                            # ၃။ တစ်ခုနဲ့တစ်ခုကြား အချိန်ဘယ်လောက်ခြားမလဲ (Interval)
                            # 60 = ၁ မိနစ်၊ 300 = ၅ မိနစ်၊ 900 = ၁၅ မိနစ်
                            time.sleep(60) 
                        except:
                            pass
            
            # တစ်သုတ် (Batch) တင်ပြီးရင် နောက်တစ်နာရီအထိ ခေတ္တနားမည်
            time.sleep(3600) 
        else:
            # Peak hour မဟုတ်ရင် ၁ နာရီတစ်ခါပဲ loop စစ်မည်
            time.sleep(3600)
# ==========================================
# START BOT
# ==========================================

if __name__ == "__main__":
    keep_alive() # Render အတွက်
    
    # Auto Post Loop ကို Thread တစ်ခုဖြင့် သီးသန့်ပတ်ထားမည်
    loop_thread = Thread(target=auto_post_loop)
    loop_thread.daemon = True
    loop_thread.start()
    
    print("🤖 Multi-Channel Bot Started...")

    bot.infinity_polling()
