import telebot
import os
import time
import threading
import requests
import pytz
from flask import Flask
from pymongo import MongoClient, ASCENDING
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telebot.apihelper import ApiTelegramException

# --- CONFIGURATIONS ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_URL = os.environ.get("RENDER_URL")

if not all([BOT_TOKEN, MONGO_URL, ADMIN_ID_RAW]):
    print("❌ Critical Error: Missing Environment Variables!")
    exit(1)

ADMIN_ID = int(ADMIN_ID_RAW)
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Yangon')

# --- DB & MODEL SETUP ---
def get_database():
    try:
        client = MongoClient(MONGO_URL, tls=True, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)
        client.admin.command('ping') # တကယ် ချိတ်မိလား စမ်းသပ်တာပါ
        return client['smart_multi_channel_bot']
    except Exception as e:
        print(f"❌ Database Connection Error: {e}")
        return None

db = get_database()
if db is None:
    exit(1) # DB ချိတ်မရရင် Bot ဆက် run လို့မရပါ
    
channels_col = db['authorized_channels']
settings_col = db['settings']
posts_col = db['posts']
sent_col = db['sent_messages']
    
posts_col.create_index([("channel_id", ASCENDING), ("msg_id", ASCENDING)])
posts_col.create_index([("posted", ASCENDING)])

# --- DECORATOR ---
def admin_only(func):
    def wrapper(message):
        if message.from_user.id == ADMIN_ID:
            return func(message)
    return wrapper

# --- WEB SERVER ---
@app.route('/')
def index(): 
    return "Bot is active and running smoothly!"

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
                # Copy message ဖြင့် တင်သည်
                sent_msg = bot.copy_message(channel_id, channel_id, next_post['msg_id'])
                posts_col.update_one({"_id": next_post["_id"]}, {"$set": {"posted": True}})
                sent_col.insert_one({"channel_id": channel_id, "msg_id": sent_msg.message_id})
                time.sleep(2)
            except ApiTelegramException as e:
                if e.error_code == 429:
                    wait_time = e.result_json['parameters']['retry_after']
                    time.sleep(wait_time)
                else: print(f"❌ API Error: {e}")
            except Exception as e:
                print(f"❌ Error: {e}")
                
        else:
            time.sleep(5) 
            sent_messages = list(sent_col.find({"channel_id": channel_id}))
            if not sent_messages:
                posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
                return

            # Bot တင်ထားသမျှ အကုန်ပြန်ဖျက်သည်
            for msg in sent_messages:
                try:
                    bot.delete_message(channel_id, msg['msg_id'])
                    time.sleep(1) # Delete တစ်ခုနဲ့တစ်ခုကြား flood မမိအောင် စောင့်ခြင်း
                except: pass
            
            # Database ရှင်းထုတ်ပြီး loop ပြန်စရန် ပြင်ဆင်သည်
            sent_col.delete_many({"channel_id": channel_id})
            posts_col.update_many({"channel_id": channel_id}, {"$set": {"posted": False}})
            print(f"🔄 Loop Restarted for Channel: {channel_id}")
            break
            
# --- ADMIN COMMANDS ---

@bot.message_handler(commands=['fetch'])
@admin_only
def fetch_old_posts(message):
    try:
        args = message.text.split()
        if len(args) < 4: return bot.reply_to(message, "❌ Format: /fetch [channel_id] [start_id] [end_id]")
        target_cid, start_id, end_id = int(args[1]), int(args[2]), int(args[3])

        status_msg = bot.reply_to(message, f"⌛ Processing {target_cid}...")
        count = 0

        for msg_id in range(start_id, end_id + 1):
            try:
                temp_msg = bot.forward_message(ADMIN_ID, target_cid, msg_id)
                posts_col.update_one({"channel_id": target_cid, "msg_id": msg_id}, {"$set": {"posted": False}}, upsert=True)
                count += 1
                bot.delete_message(ADMIN_ID, temp_msg.message_id)
                time.sleep(0.3)
            except: continue
        bot.edit_message_text(f"✅ Success! Saved {count} posts.", message.chat.id, status_msg.message_id)
    except Exception as e: bot.reply_to(message, f"❌ Error: {e}")
  
@bot.message_handler(commands=['addchannel'])
@admin_only
def add_channel(message):
    try:
        cid = int(message.text.split()[1])
        channels_col.update_one({"channel_id": cid}, {"$set": {"active": True}}, upsert=True)
        bot.reply_to(message, f"✅ Authorized: {cid}")
    except: bot.reply_to(message, "❌ Use: /addchannel -100xxx")

@bot.message_handler(commands=['set'])
@admin_only
def set_config(message):
    try:
        args = message.text.split()
        if len(args) < 4:
            return bot.reply_to(message, "❌ Format: /set [channel_id] [count] [hours]")
            
        target_cid = int(args[1])
        count = int(args[2])
        hours = args[3]
        
        valid_hours = [h.strip() for h in hours.split(',') if h.strip().isdigit() and 0 <= int(h.strip()) <= 23]
        if not valid_hours:
            return bot.reply_to(message, "❌ နာရီ Format မှားနေပါသည်။ (ဥပမာ: 9,13,21)")

        settings_col.update_one(
            {"channel_id": target_cid}, 
            {"$set": {"post_count": count, "hours": ",".join(valid_hours)}}, 
            upsert=True
        )
        setup_scheduler() # Setting ပြောင်းပြီးတာနဲ့ scheduler ကို update တန်းလုပ်ပေးမယ်
        bot.reply_to(message, f"✅ Setting အောင်မြင်ပါသည်။\nChannel: {target_cid}\nတစ်ခါတင်မည့်အရေအတွက်: {count}\nနာရီများ: {','.join(valid_hours)}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['list'])
@admin_only
def list_channels(message):
    active_channels = list(channels_col.find({"active": True}))
    if not active_channels: return bot.reply_to(message, "⚠️ No active channels.")
    res = "📊 **Status:**\n\n"
    for ch in active_channels:
        cid = ch['channel_id']
        rem = posts_col.count_documents({"channel_id": cid, "posted": False})
        res += f"🆔 `{cid}` | ⏳ Rem: {rem}\n"
    bot.send_message(message.chat.id, res, parse_mode="Markdown")

@bot.channel_post_handler(func=lambda message: True, content_types=['text', 'photo', 'video', 'document'])
def handle_channel_post(message):
    if not channels_col.find_one({"channel_id": message.chat.id}): return
    if sent_col.find_one({"channel_id": message.chat.id, "msg_id": message.message_id}): return
    if message.forward_from_chat and message.forward_from_chat.id != message.chat.id: return
    
    posts_col.update_one(
        {"channel_id": message.chat.id, "msg_id": message.message_id},
        {"$set": {"posted": False}},
        upsert=True
    )

# Keep-alive function
def keep_alive_ping():
    if RENDER_URL:
        try: requests.get(RENDER_URL)
        except: pass

# --- SCHEDULER ---
scheduler = BackgroundScheduler(timezone=tz)

def setup_scheduler():
    scheduler.remove_all_jobs()
    if RENDER_URL: scheduler.add_job(keep_alive_ping, 'interval', minutes=5)
    for s in settings_col.find():
        for hr in s.get('hours', "").split(','):
            if hr.strip().isdigit():
                try:
                    h_val = int(hr.strip())
                    scheduler.add_job(auto_forward_job, CronTrigger(hour=h_val, minute=0, timezone=tz), args=[s['channel_id']])
                except: pass
    print("⏰ Scheduler Ready.")

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    setup_scheduler()
    if not scheduler.running: scheduler.start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
