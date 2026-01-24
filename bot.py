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

# --- CONFIGURATIONS ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_URL = os.environ.get("RENDER_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Yangon')

# --- DB & MODEL SETUP ---
# Database connection ကို error handling နဲ့ သေသေချာချာ ချိတ်ဆက်ပါမယ်
try:
    client = MongoClient(MONGO_URL, tls=True, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)
    db = client['smart_multi_channel_bot']
    channels_col = db['authorized_channels']
    settings_col = db['settings']
    posts_col = db['posts']
    sent_col = db['sent_messages']
    
    # Query ပိုမြန်စေဖို့ Index တွေ သတ်မှတ်ထားပါမယ်
    posts_col.create_index([("channel_id", ASCENDING), ("msg_id", ASCENDING)])
    posts_col.create_index([("posted", ASCENDING)])
except Exception as e:
    print(f"❌ Database Connection Error: {e}")

# Global Bot Info (bot.infinity_polling စတင်ချိန်မှ ယူပါမယ်)
BOT_INFO = None

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
    
    # ရွေးထားတဲ့ အရေအတွက်အတိုင်း Post တင်မည်
    for _ in range(post_count):
        next_post = posts_col.find_one({"channel_id": channel_id, "posted": False}, sort=[("msg_id", 1)])
        
        if next_post:
            try:
                # copy_message သည် ပိုမိုစိတ်ချရပြီး message history ကို မပျက်စေပါ
                sent_msg = bot.copy_message(channel_id, channel_id, next_post['msg_id'])
                posts_col.update_one({"_id": next_post["_id"]}, {"$set": {"posted": True}})
                sent_col.insert_one({"channel_id": channel_id, "msg_id": sent_msg.message_id})
                time.sleep(2) # Telegram Flood limit ကို ရှောင်ရန်
            except Exception as e:
                print(f"Post Error in {channel_id}: {e}")
        else:
            # Post အသစ်မရှိတော့လျှင် အဟောင်းများကို ဖျက်ပြီး loop ပြန်စမည်
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

@bot.message_handler(commands=['fetch'])
def fetch_old_posts(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        if len(args) < 4:
            return bot.reply_to(message, "❌ Format: /fetch [channel_id] [start_id] [end_id]")
            
        target_cid = int(args[1])
        start_id = int(args[2])
        end_id = int(args[3])

        # Range အရမ်းများရင် Bot Crash ဖြစ်နိုင်လို့ ကန့်သတ်ပေးတာ ပိုကောင်းပါတယ်
        if (end_id - start_id) > 1000:
            return bot.reply_to(message, "⚠️ တစ်ခါ fetch ရင် message ၁၀၀၀ ထက် မပိုသင့်ပါ။")

        status_msg = bot.reply_to(message, f"⌛ Channel {target_cid} မှ Post များကို စတင်စစ်ဆေးနေသည်...")
        count = 0

        for msg_id in range(start_id, end_id + 1):
            try:
                # Message ရှိမရှိ စစ်ရန် Admin ဆီ ယာယီ forward ကြည့်ခြင်း
                temp_msg = bot.forward_message(ADMIN_ID, target_cid, msg_id, disable_notification=True)
                
                # Forwarded source မရှိမှ (သို့) ကိုယ့် channel ကိုယ်ပြန်တင်ထားတာမှ သိမ်းမည်
                if not temp_msg.forward_from_chat or temp_msg.forward_from_chat.id == target_cid:
                    posts_col.update_one(
                        {"channel_id": target_cid, "msg_id": msg_id},
                        {"$set": {"posted": False}},
                        upsert=True
                    )
                    count += 1
                
                bot.delete_message(ADMIN_ID, temp_msg.message_id)
                time.sleep(0.3) # Process ကို အနည်းငယ် ပိုမြန်စေရန် 0.3 သုံးထားပါသည်
            except:
                continue
        
        bot.edit_message_text(f"✅ လုပ်ဆောင်ချက် ပြီးဆုံးပါပြီ!\nPost အသစ် {count} ခုကို Database ထဲ သိမ်းဆည်းပြီးပါပြီ။", message.chat.id, status_msg.message_id)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['addchannel'])
def add_channel(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        cid = int(message.text.split()[1])
        channels_col.update_one({"channel_id": cid}, {"$set": {"active": True}}, upsert=True)
        bot.reply_to(message, f"✅ Channel {cid} ကို ခွင့်ပြုလိုက်ပါပြီ။")
    except:
        bot.reply_to(message, "❌ Format: /addchannel -100xxxxxxxx")

@bot.message_handler(commands=['set'])
def set_config(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        target_cid = int(args[1])
        count = int(args[2])
        hours = args[3] # format: "9,12,15,21"
        
        # Validation: နာရီ format မှန်မမှန် စစ်ဆေးခြင်း
        valid_hours = [h.strip() for h in hours.split(',') if h.strip().isdigit() and 0 <= int(h.strip()) <= 23]
        if not valid_hours:
            return bot.reply_to(message, "❌ နာရီ Format မှားနေပါသည်။ (ဥပမာ: 9,13,21)")

        settings_col.update_one(
            {"channel_id": target_cid}, 
            {"$set": {"post_count": count, "hours": ",".join(valid_hours)}}, 
            upsert=True
        )
        setup_scheduler() # Scheduler ကို update လုပ်မည်
        bot.reply_to(message, f"✅ Setting အောင်မြင်ပါသည်။\nChannel: {target_cid}\nတစ်ခါတင်မည့်အရေအတွက်: {count}\nနာရီများ: {','.join(valid_hours)}")
    except Exception as e:
        bot.reply_to(message, f"❌ Format: /set [channel_id] [count] [hours]\nError: {e}")

@bot.channel_post_handler(func=lambda message: True)
def handle_channel_post(message):
    # ၁။ ခွင့်ပြုထားတဲ့ Channel ဟုတ်၊ မဟုတ် စစ်မည်
    if not channels_col.find_one({"channel_id": message.chat.id}):
        return

    # ၂။ Bot ကိုယ်တိုင် တင်တာဆိုရင် Database ထဲ ထပ်မသိမ်းပါ
    if BOT_INFO and message.from_user and message.from_user.id == BOT_INFO.id:
        return
    
    # ၃။ Forward Filter (တခြား Channel မှ Forward လာတာတွေကို မသိမ်းပါ)
    if message.forward_from_chat and message.forward_from_chat.id != message.chat.id:
        return

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
        hours_str = s.get('hours', "")
        for hr in hours_str.split(','):
            if hr.strip().isdigit():
                try:
                    h_val = int(hr.strip())
                    scheduler.add_job(
                        auto_forward_job, 
                        CronTrigger(hour=h_val, minute=0, timezone=tz), 
                        args=[s['channel_id']], 
                        id=f"{s['channel_id']}_{h_val}"
                    )
                except: pass
    print("⏰ Scheduler synchronization complete.")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Flask ကို Thread နဲ့ Background မှာ ပတ်ထားမယ်
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Bot အချက်အလက်ကို စတင်ရယူမယ်
    try:
        BOT_INFO = bot.get_me()
        print(f"🤖 Bot @{BOT_INFO.username} is starting...")
    except Exception as e:
        print(f"❌ Failed to get bot info: {e}")

    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        
    # Bot ကို infinity loop ပတ်ထားမယ်
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
