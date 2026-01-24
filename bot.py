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

# --- CONFIGURATIONS (Environment Variables မှ ယူမည်) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_URL = os.environ.get("RENDER_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Yangon')

# MongoDB Setup
client = MongoClient(MONGO_URL)
db = client['multi_channel_autobot']
settings_col = db['settings']
posts_col = db['posts']
sent_col = db['sent_messages']

# --- WEB SERVER FOR KEEP-ALIVE ---
@app.route('/')
def index():
    return "Bot is running perfectly!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    while True:
        try:
            if RENDER_URL:
                requests.get(RENDER_URL)
            time.sleep(600) # ၁၀ မိနစ်တစ်ခါ Website ကို နိုးပေးသည်
        except:
            pass

# --- CORE LOGIC: POSTING & CLEANING ---

def auto_forward_job(channel_id):
    setting = settings_col.find_one({"channel_id": channel_id})
    if not setting:
        return

    post_count = setting.get("post_count", 1)
    
    for _ in range(post_count):
        # မတင်ရသေးသော post ကို ရှာသည်
        next_post = posts_col.find_one({"channel_id": channel_id, "posted": False}, sort=[("msg_id", 1)])
        
        if next_post:
            try:
                # Forward လုပ်သည်
                sent_msg = bot.forward_message(channel_id, channel_id, next_post['msg_id'])
                
                # DB မှာ status ပြောင်းသည်
                posts_col.update_one({"_id": next_post["_id"]}, {"$set": {"posted": True}})
                
                # နောက်မှပြန်ဖျက်ရန် ID သိမ်းသည်
                sent_col.insert_one({"channel_id": channel_id, "msg_id": sent_msg.message_id})
                time.sleep(2) # Telegram Flood limit ကာကွယ်ရန်
            except Exception as e:
                print(f"Error in {channel_id}: {e}")
        else:
            # တင်စရာ Post မရှိတော့လျှင် (Cycle ပြည့်လျှင်) အဟောင်းများဖျက်ပြီး Reset လုပ်သည်
            print(f"Cycle complete for {channel_id}. Starting Cleanup...")
            sent_messages = sent_col.find({"channel_id": channel_id})
            for msg in sent_messages:
                try:
                    bot.delete_message(channel_id, msg['msg_id'])
                    time.sleep(1.2) # ဖျက်နှုန်း အရမ်းမမြန်စေရန်
                except:
                    pass
            
            sent_col.delete_many({"channel_id": channel_id})
            posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
            print(f"Channel {channel_id} reset success.")
            break

# --- ADMIN COMMANDS ---

@bot.message_handler(commands=['set'])
def set_config(message):
    # Admin စစ်ဆေးခြင်း
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⚠️ သင်သည် Admin မဟုတ်ပါ။")
        return

    try:
        # Command format: /set 1 8,10,18
        args = message.text.split()
        count = int(args[1])
        hours = args[2] 
        channel_id = message.chat.id

        settings_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"post_count": count, "hours": hours}},
            upsert=True
        )
        
        setup_scheduler() # Scheduler ကို update လုပ်သည်
        bot.reply_to(message, f"✅ Settings Updated!\nChannel: {channel_id}\nDaily Hours: {hours}\nPost per cycle: {count}")
    except Exception:
        bot.reply_to(message, "❌ Format မှားယွင်းနေပါသည်။\nဥပမာ - `/set 1 8,10,18,22`", parse_mode="Markdown")

@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    # Admin က တင်လိုက်တဲ့ Original post များကို DB ထဲ သိမ်းသည်
    if not message.forward_from_chat:
        posts_col.update_one(
            {"channel_id": message.chat.id, "msg_id": message.message_id},
            {"$set": {"posted": False}},
            upsert=True
        )

# --- SCHEDULER SETUP ---
scheduler = BackgroundScheduler(timezone=tz)

def setup_scheduler():
    scheduler.remove_all_jobs()
    all_settings = settings_col.find()
    for setting in all_settings:
        channel_id = setting['channel_id']
        hours = setting['hours'].split(',')
        for hour in hours:
            try:
                scheduler.add_job(
                    auto_forward_job,
                    CronTrigger(hour=int(hour), minute=0, timezone=tz),
                    args=[channel_id],
                    id=f"{channel_id}_{hour}"
                )
            except:
                pass

# --- STARTUP ---
if __name__ == "__main__":
    # Flask & Keep-alive ကို Background မှာ run မည်
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    
    setup_scheduler()
    scheduler.start()
    
    print("Bot is polling and scheduler is active...")
    bot.infinity_polling()
