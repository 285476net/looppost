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
channels_col = db['authorized_channels']
settings_col = db['settings']
posts_col = db['posts']
sent_col = db['sent_messages']

# --- WEB SERVER ---
@app.route('/')
def index(): return "Bot is strictly active!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# --- POSTING & CLEANING ---
def auto_forward_job(channel_id):
    setting = settings_col.find_one({"channel_id": channel_id})
    if not setting: return

    post_count = setting.get("post_count", 1)
    
    for _ in range(post_count):
        next_post = posts_col.find_one({"channel_id": channel_id, "posted": False}, sort=[("msg_id", 1)])
        
        if next_post:
            try:
                # Forward အစား copy_message ကို သုံးခြင်းက ပိုစိတ်ချရသည်
                sent_msg = bot.copy_message(channel_id, channel_id, next_post['msg_id'])
                posts_col.update_one({"_id": next_post["_id"]}, {"$set": {"posted": True}})
                sent_col.insert_one({"channel_id": channel_id, "msg_id": sent_msg.message_id})
                time.sleep(2)
            except Exception as e:
                print(f"Error in {channel_id}: {e}")
        else:
            # Cleanup logic
            sent_messages = list(sent_col.find({"channel_id": channel_id}))
            if not sent_messages:
                posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
                return

            for msg in sent_messages:
                try:
                    bot.delete_message(channel_id, msg['msg_id'])
                    time.sleep(1)
                except: pass
            
            sent_col.delete_many({"channel_id": channel_id})
            posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
            break

# --- ADMIN COMMANDS ---

# ၁။ Post အဟောင်းများကို ID Range ဖြင့် ဆွဲယူခြင်း (အရေးကြီးဆုံး အပိုင်း)
@bot.message_handler(commands=['fetch'])
def fetch_old_posts(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        # Format: /fetch [channel_id] [start_id] [end_id]
        args = message.text.split()
        target_cid = int(args[1])
        start_id = int(args[2])
        end_id = int(args[3])

        count = 0
        status_msg = bot.reply_to(message, f"⌛ Channel {target_cid} မှ Post များကို စစ်ဆေးနေသည်...")

        for msg_id in range(start_id, end_id + 1):
            try:
                # Message ရှိ၊ မရှိ copy စမ်းလုပ်ကြည့်ခြင်းဖြင့် စစ်ဆေးသည်
                # တကယ် copy မလုပ်ဘဲ data ပဲ ယူမှာမို့လို့ Chat ID အဖြစ် Admin ID ကို ခဏသုံးမယ်
                temp_msg = bot.forward_message(ADMIN_ID, target_cid, msg_id, disable_notification=True)
                
                # Forwarded မဟုတ်မှ သိမ်းမည်
                if not temp_msg.forward_from_chat or temp_msg.forward_from_chat.id == target_cid:
                    posts_col.update_one(
                        {"channel_id": target_cid, "msg_id": msg_id},
                        {"$set": {"posted": False}},
                        upsert=True
                    )
                    count += 1
                
                bot.delete_message(ADMIN_ID, temp_msg.message_id) # ယာယီ forward တာကို ပြန်ဖျက်သည်
                time.sleep(0.5) # Flood wait ရှောင်ရန်
            except:
                continue
        
        bot.edit_message_text(f"✅ လုပ်ဆောင်ချက် ပြီးဆုံးပါပြီ!\nPost အသစ် {count} ခုကို Database ထဲ ထည့်သွင်းပြီးပါပြီ။", message.chat.id, status_msg.message_id)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}\nFormat: /fetch -100xxx 1 500")

# ၂။ Channel ခွင့်ပြုခြင်း
@bot.message_handler(commands=['addchannel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        cid = int(message.text.split()[1])
        channels_col.update_one({"channel_id": cid}, {"$set": {"active": True}}, upsert=True)
        bot.reply_to(message, f"✅ Channel {cid} ကို ခွင့်ပြုလိုက်ပါပြီ။")
    except:
        bot.reply_to(message, "❌ Format: /addchannel -100xxxxxxxx")

# ၃။ Setting သတ်မှတ်ခြင်း
@bot.message_handler(commands=['set'])
def set_config(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        target_cid = int(args[1])
        count = int(args[2])
        hours = args[3]
        settings_col.update_one({"channel_id": target_cid}, {"$set": {"post_count": count, "hours": hours}}, upsert=True)
        setup_scheduler()
        bot.reply_to(message, "✅ Setting အောင်မြင်ပါသည်။")
    except:
        bot.reply_to(message, "❌ Format: /set [channel_id] [count] [hours]")

# ၄။ Post အသစ်တင်လျှင် သိမ်းခြင်း
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    if channels_col.find_one({"channel_id": message.chat.id}):
        # Bot ကိုယ်တိုင် copy/forward လုပ်တာ မဟုတ်မှ သိမ်းမယ်
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
    for s in settings_col.find():
        for hr in s['hours'].split(','):
            try:
                scheduler.add_job(auto_forward_job, CronTrigger(hour=int(hr), minute=0, timezone=tz), args=[s['channel_id']], id=f"{s['channel_id']}_{hr}")
            except: pass

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    setup_scheduler()
    scheduler.start()
    bot.infinity_polling()
