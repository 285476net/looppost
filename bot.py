import telebot
import os
import time
import threading
import requests
import pytz
from flask import Flask
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# --- CONFIGURATIONS ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_URL = os.environ.get("RENDER_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Yangon')

# MongoDB Setup
client = MongoClient(MONGO_URL, tls=True, tlsAllowInvalidCertificates=True)
db = client['smart_multi_channel_bot']
channels_col = db['authorized_channels'] # ခွင့်ပြုထားတဲ့ channel စာရင်း
settings_col = db['settings']            # အချိန်နဲ့ post အရေအတွက် setting
posts_col = db['posts']                  # Original posts database
sent_col = db['sent_messages']           # ဖျက်ဖို့ msg id များ

# --- WEB SERVER & KEEP ALIVE ---
@app.route('/')
def index(): return "Bot is strictly active!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    while True:
        try:
            if RENDER_URL: requests.get(RENDER_URL)
            time.sleep(600)
        except: pass

# --- CORE LOGIC: POSTING & CLEANING ---

def auto_forward_job(channel_id):
    setting = settings_col.find_one({"channel_id": channel_id})
    if not setting: return

    post_count = setting.get("post_count", 1)
    
    for _ in range(post_count):
        next_post = posts_col.find_one({"channel_id": channel_id, "posted": False}, sort=[("msg_id", 1)])
        
        if next_post:
            try:
                sent_msg = bot.forward_message(channel_id, channel_id, next_post['msg_id'])
                posts_col.update_one({"_id": next_post["_id"]}, {"$set": {"posted": True}})
                sent_col.insert_one({"channel_id": channel_id, "msg_id": sent_msg.message_id})
                time.sleep(2)
            except Exception as e:
                print(f"Error in {channel_id}: {e}")
        else:
            # Cycle ပြီးရင် Cleanup လုပ်မယ်
            sent_messages = sent_col.find({"channel_id": channel_id})
            for msg in sent_messages:
                try:
                    bot.delete_message(channel_id, msg['msg_id'])
                    time.sleep(1.2)
                except: pass
            
            sent_col.delete_many({"channel_id": channel_id})
            posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
            break

# --- ADMIN COMMANDS ---

# ၁။ Channel အသစ်ထည့်ခြင်း (Private chat မှာ ID ပေးပြီး ခိုင်းရမယ်)
@bot.message_handler(commands=['addchannel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        cid = int(message.text.split()[1])
        channels_col.update_one({"channel_id": cid}, {"$set": {"active": True}}, upsert=True)
        bot.reply_to(message, f"✅ Channel {cid} ကို ခွင့်ပြုလိုက်ပါပြီ။")
    except:
        bot.reply_to(message, "❌ Format: /addchannel -100xxxxxxxx")

# ၂။ Channel Setting သတ်မှတ်ခြင်း (Channel ထဲမှာဖြစ်စေ၊ Private Chat မှာဖြစ်စေ ခိုင်းနိုင်တယ်)
@bot.message_handler(commands=['set'])
def set_config(message):
    if message.from_user.id != ADMIN_ID: return
    
    try:
        args = message.text.split()
        # Format: /set [channel_id] [count] [hours]
        # ဥပမာ: /set -100123 1 8,12,18
        target_cid = int(args[1])
        count = int(args[2])
        hours = args[3]

        if not channels_col.find_one({"channel_id": target_cid}):
            bot.reply_to(message, "⚠️ ဒီ Channel ကို အရင် Add လုပ်ပေးပါ။")
            return

        settings_col.update_one(
            {"channel_id": target_cid},
            {"$set": {"post_count": count, "hours": hours}},
            upsert=True
        )
        setup_scheduler()
        bot.reply_to(message, f"✅ Updated: Channel {target_cid}\nHours: {hours}\nPer Time: {count}")
    except:
        bot.reply_to(message, "❌ Format: /set [channel_id] [count] [hours]\nဥပမာ: /set -100xxx 1 8,12,20")

# ၃။ လက်ရှိ Active ဖြစ်နေတဲ့ Channel များ ကြည့်ရန်
@bot.message_handler(commands=['list'])
def list_channels(message):
    if message.from_user.id != ADMIN_ID: return
    active_list = channels_col.find()
    msg = "📋 **Authorized Channels:**\n"
    for c in active_list:
        msg += f"• `{c['channel_id']}`\n"
    bot.reply_to(message, msg, parse_mode="Markdown")

# ၄။ Post သိမ်းဆည်းခြင်း (ခွင့်ပြုထားသော channel မှသာ သိမ်းမည်)
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    if channels_col.find_one({"channel_id": message.chat.id}):
        if not message.forward_from_chat:
            posts_col.update_one(
                {"channel_id": message.chat.id, "msg_id": message.message_id},
                {"$set": {"posted": False}},
                upsert=True
            )

# --- SCHEDULER ---
scheduler = BackgroundScheduler(timezone=tz)

def setup_scheduler():
    scheduler.remove_all_jobs()
    all_settings = settings_col.find()
    for setting in all_settings:
        cid = setting['channel_id']
        hours = setting['hours'].split(',')
        for hr in hours:
            try:
                scheduler.add_job(
                    auto_forward_job,
                    CronTrigger(hour=int(hr), minute=0, timezone=tz),
                    args=[cid],
                    id=f"{cid}_{hr}"
                )
            except: pass

# --- START ---
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    setup_scheduler()
    scheduler.start()
    print("Bot is ready for Multi-Channel Management!")
    bot.infinity_polling()
