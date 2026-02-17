import os
import re
import datetime
import pytz
import threading
import certifi
from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from io import BytesIO
from pymongo import MongoClient
from bson.objectid import ObjectId

# --- CONFIGURATION ---
TOKEN = os.environ.get("BOT_TOKEN") 
OWNER_ID = os.environ.get("OWNER_ID") 
MONGO_URI = os.environ.get("MONGO_URI")

IST = pytz.timezone('Asia/Kolkata')

# --- MONGODB CONNECTION ---
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
# This name 'trading_bot' separates it from your MovieBox bot
db = client['trading_bot']
logs_col = db['logs']           
reminders_col = db['reminders'] 

# --- KEEP ALIVE SERVER ---
app = Flask(__name__)
@app.route('/')
def home(): return "Trading Bot (MongoDB) Online!"
def run_http(): app.run(host='0.0.0.0', port=10000)
def keep_alive():
    t = threading.Thread(target=run_http)
    t.start()

# --- HELPER FUNCTIONS ---

def extract_tags(text):
    if not text: return ""
    return ", ".join(re.findall(r"#\w+", text))

def save_log(content, tags, custom_date=None):
    if custom_date:
        timestamp = f"{custom_date} {datetime.datetime.now(IST).strftime('%H:%M:%S')}"
    else:
        timestamp = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    
    doc = {
        "timestamp": timestamp,
        "tags": tags,
        "content": content
    }
    logs_col.insert_one(doc)

def get_logs(tag_filter=None):
    if tag_filter:
        query = {"tags": {"$regex": tag_filter}}
        return list(logs_col.find(query).sort("timestamp", -1))
    else:
        return list(logs_col.find().sort("timestamp", -1))

def clear_logs_for_restore():
    logs_col.delete_many({})

def format_logs_for_export(logs):
    if not logs:
        return "No entries found."

    # -------- GROUP BY DATE --------
    grouped = {}
    for doc in logs:
        timestamp_str = doc['timestamp']
        content = doc['content']
        date_part = timestamp_str.split(' ')[0]

        if date_part not in grouped:
            grouped[date_part] = []

        grouped[date_part].append(content)

    # -------- SORT DAYS (NEWEST FIRST) --------
    sorted_dates = sorted(grouped.keys(), reverse=True)

    output = []

    # -------- PRINT EACH DAY --------
    for date in sorted_dates:
        output.append(f"\n=== 📅 {date} ===\n")

        # Reverse messages INSIDE day (oldest → newest)
        day_messages = list(reversed(grouped[date]))

        for msg in day_messages:
            output.append(f"{msg}\n\n")

    return "".join(output)


# --- SECURITY CHECK ---
async def check_auth(update: Update):
    if not OWNER_ID: return True 
    if update.effective_user.id != int(OWNER_ID): return False
    return True

# --- STATS ENGINE ---
def calculate_stats():
    all_logs = logs_col.find({}, {"content": 1})
    
    total_profit = 0
    total_loss = 0
    wins = 0
    losses = 0

    for doc in all_logs:
        content = doc['content']
        match = re.search(r"💰 P&L: ([+-]?\d+)", content)
        if match:
            amount = int(match.group(1))
            if amount > 0:
                total_profit += amount
                wins += 1
            else:
                total_loss += amount
                losses += 1

    net_pnl = total_profit + total_loss
    total_trades = wins + losses
    win_rate = int((wins / total_trades) * 100) if total_trades > 0 else 0

    return total_trades, wins, losses, win_rate, net_pnl, total_profit, total_loss

# --- PERSISTENT REMINDER SYSTEM ---

async def restore_reminders(app):
    reminders = list(reminders_col.find())
    print(f"🔄 Restoring {len(reminders)} reminders from MongoDB...")
    
    for r in reminders:
        chat_id = r['chat_id']
        r_type = r['type']
        msg = r['msg']
        args = r['args'] 
        
        try:
            if r_type == 'daily':
                h, m = args[0], args[1]
                app.job_queue.run_daily(send_reminder_job, datetime.time(h, m, tzinfo=IST), chat_id=chat_id, data=msg, name=str(r['_id']))
            elif r_type == 'weekly':
                day_num, h, m = args[0], args[1], args[2]
                app.job_queue.run_daily(send_reminder_job, datetime.time(h, m, tzinfo=IST), days=(day_num,), chat_id=chat_id, data=msg, name=str(r['_id']))
            elif r_type == 'monthly':
                d, h, m = args[0], args[1], args[2]
                app.job_queue.run_monthly(send_reminder_job, datetime.time(h, m, tzinfo=IST), day=d, chat_id=chat_id, data=msg, name=str(r['_id']))
            elif r_type == 'yearly':
                target_ts = args[0]
                target = datetime.datetime.fromtimestamp(target_ts, IST)
                app.job_queue.run_repeating(send_reminder_job, interval=31536000, first=target, chat_id=chat_id, data=msg, name=str(r['_id']))
            elif r_type == 'once':
                target_ts = args[0]
                target = datetime.datetime.fromtimestamp(target_ts, IST)
                if target > datetime.datetime.now(IST):
                    app.job_queue.run_once(send_reminder_job, target, chat_id=chat_id, data=msg, name=str(r['_id']))
                else:
                    reminders_col.delete_one({'_id': r['_id']})
        except Exception as e:
            print(f"Failed to restore reminder: {e}")

async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"🔔 **ALERT:**\n{job.data}", parse_mode="Markdown")

# --- LIST & DELETE (User Friendly) ---

async def list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    
    # Check Active Memory for accurate "Next Run Time"
    jobs = context.job_queue.jobs()
    
    if not jobs: 
        await update.message.reply_text("No active alerts.")
        return
        
    msg = "**⏰ Active Cloud Alerts:**\n"
    for i, job in enumerate(jobs):
        next_run = "Running..."
        if job.next_t:
            next_run = job.next_t.astimezone(IST).strftime("%d-%m %H:%M")
        msg += f"ID: `{i}` | {next_run} | {job.data}\n"
        
    msg += "\n`/kill <ID>` to delete."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def delete_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    if not context.args: return
    
    try:
        simple_id = int(context.args[0])
        jobs = context.job_queue.jobs()
        
        if simple_id < 0 or simple_id >= len(jobs):
            await update.message.reply_text("❌ Invalid ID.")
            return

        target_job = jobs[simple_id]
        mongo_id = target_job.name 
        
        # Delete from Cloud & Memory
        reminders_col.delete_one({'_id': ObjectId(mongo_id)})
        target_job.schedule_removal()
            
        await update.message.reply_text(f"🗑️ Deleted: {target_job.data}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# --- COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    keyboard = [
        [KeyboardButton("📊 Journal"), KeyboardButton("📦 Full Backup")],
        [KeyboardButton("⏰ Reminders"), KeyboardButton("❓ Help")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("**Trading Bot Ready.**\nLogs & Reminders are safe in MongoDB.", reply_markup=reply_markup, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    msg = (
        "**📈 COMMANDS:**\n\n"
        "**1. Log Trade:**\n`/pnl +5000 Nifty Call`\n\n"
        "**2. Reminders:**\n"
        "`/reminder daily 09 15 Market Open`\n"
        "`/reminder week mon 10 00 Weekly Meet`\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: `/pnl +5000 Reason`")
        return
    try:
        amount = int(args[0])
        reason = " ".join(args[1:])
        log_entry = f"💰 P&L: {amount} | {reason}"
        tags = "#profit" if amount > 0 else "#loss"
        save_log(log_entry, tags)
        await update.message.reply_text("✅ Logged.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ First value must be a number.")

# --- THE JOURNAL REPORT (PROFESSIONAL BOX STYLE) ---
async def journal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    total, wins, losses, win_rate, net, gross_profit, gross_loss = calculate_stats()
    
    trades = list(logs_col.find({"content": {"$regex": "💰 P&L:"}}).sort("timestamp", 1))

    report = "========================================\n"
    report += "         🏛️ MASTER TRADING JOURNAL       \n"
    report += "========================================\n\n"
    report += "--- 📜 TRADE LIST ---\n"
    
    if not trades:
        report += "No trades recorded yet.\n"
    else:
        for doc in trades:
            short_time = doc['timestamp'][:16]
            content = doc['content']
            report += f"[{short_time}] {content}\n"
    
    report += "\n"
    report += "========================================\n"
    report += "         📊 PERFORMANCE STATS           \n"
    report += "========================================\n"
    report += f"Total Trades  : {total}\n"
    report += f"Win Rate      : {win_rate}%\n"
    report += f"Gross Profit  : ₹{gross_profit}\n"
    report += f"Gross Loss    : ₹{gross_loss}\n"
    report += f"----------------------------------------\n"
    report += f"NET P&L       : ₹{net}\n"
    report += "========================================\n"

    file_bytes = BytesIO(report.encode('utf-8'))
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    file_bytes.name = f"Report_{today}.txt"
    await update.message.reply_document(document=file_bytes, caption=f"📊 Report: Net P&L ₹{net}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    text = update.message.text
    if text == "📦 Full Backup" or text == "📦 Backup": await backup(update, context)
    elif text == "📊 Journal": await journal_command(update, context)
    elif text == "⏰ Reminders": await list_jobs(update, context)
    elif text == "❓ Help": await help_command(update, context)
    else: save_log(text, extract_tags(text))

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    caption = update.message.caption or ""
    log_content = ""
    if update.message.photo: log_content = f"[📸 CHART] {caption}"
    elif update.message.document: 
        if not update.message.document.file_name.endswith('.txt'):
            log_content = f"[📄 FILE] {caption}"
    if log_content: save_log(log_content.strip(), extract_tags(caption))

async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    logs = get_logs()
    file_content = format_logs_for_export(logs)
    file_bytes = BytesIO(file_content.encode('utf-8'))
    today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
    file_bytes.name = f"Backup_{today}.txt"
    await update.message.reply_document(document=file_bytes, caption="📦 Cloud Data Backup")

async def handle_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return

    document = update.message.document
    if not (document.mime_type == "text/plain" or document.file_name.endswith('.txt')):
        return

    file = await document.get_file()
    file_bytes = await file.download_as_bytearray()
    content = file_bytes.decode('utf-8')

    clear_logs_for_restore()

    lines = content.split('\n')

    current_date = None
    second_counter = 1  # keeps order inside same day

    for line in lines:
        line = line.rstrip()

        # detect date header
        date_match = re.search(r"===\s*📅\s*(\d{4}-\d{2}-\d{2})\s*===", line)
        if date_match:
            current_date = date_match.group(1)
            second_counter = 1
            continue

        if not line.strip():
            continue

        # create increasing timestamp
        fake_time = f"{current_date} 00:00:{second_counter:02d}"

        doc = {
            "timestamp": fake_time,
            "tags": extract_tags(line),
            "content": line
        }

        logs_col.insert_one(doc)

        second_counter += 1

    await update.message.reply_text("♻️ Database restored with correct order.")


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update): return
    chat_id = update.effective_message.chat_id
    args = context.args
    if not args: await help_command(update, context); return
    first = args[0].lower()
    
    try:
        r_type = ""
        r_args = []
        msg = ""
        
        # 1. DAILY
        if first == 'daily':
            h, m = int(args[1]), int(args[2])
            msg = " ".join(args[3:])
            r_type = 'daily'
            r_args = [h, m]
            
            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_daily(send_reminder_job, datetime.time(h, m, tzinfo=IST), chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Daily Alert Saved.")

        # 2. WEEKLY
        elif first == 'week':
            day_map = {'mon':0, 'tue':1, 'wed':2, 'thu':3, 'fri':4, 'sat':5, 'sun':6}
            day_str = args[1][:3].lower()
            h, m = int(args[2]), int(args[3])
            msg = " ".join(args[4:])
            r_type = 'weekly'
            r_args = [day_map[day_str], h, m]
            
            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_daily(send_reminder_job, datetime.time(h, m, tzinfo=IST), days=(day_map[day_str],), chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Weekly Alert Saved.")

        # 3. MONTHLY
        elif first == 'month':
            d, h, m = int(args[1]), int(args[2]), int(args[3])
            msg = " ".join(args[4:])
            r_type = 'monthly'
            r_args = [d, h, m]
            
            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_monthly(send_reminder_job, datetime.time(h, m, tzinfo=IST), day=d, chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Monthly Alert Saved.")

        # 4. YEARLY
        elif first == 'year':
            d, month, h, m = int(args[1]), int(args[2]), int(args[3]), int(args[4])
            msg = " ".join(args[5:])
            r_type = 'yearly'
            
            now = datetime.datetime.now(IST)
            target = now.replace(month=month, day=d, hour=h, minute=m, second=0)
            if target < now: target = target.replace(year=now.year + 1)
            r_args = [target.timestamp()] 
            
            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_repeating(send_reminder_job, interval=31536000, first=target, chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Yearly Alert Saved.")

        # 5. DATE (Specific)
        elif len(args) >= 4:
            d, month, h, m = int(args[0]), int(args[1]), int(args[2]), int(args[3])
            msg = " ".join(args[4:])
            r_type = 'once'
            
            now = datetime.datetime.now(IST)
            target = now.replace(month=month, day=d, hour=h, minute=m, second=0)
            if target < now: target = target.replace(year=now.year + 1)
            r_args = [target.timestamp()]
            
            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_once(send_reminder_job, target, chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Date Alert Saved.")

        # 6. TODAY (Quick)
        elif len(args) >= 2:
            h, m = int(args[0]), int(args[1])
            msg = " ".join(args[2:])
            r_type = 'once'
            
            now = datetime.datetime.now(IST)
            target = now.replace(hour=h, minute=m, second=0)
            if target < now: target += datetime.timedelta(days=1)
            r_args = [target.timestamp()]

            res = reminders_col.insert_one({'chat_id': chat_id, 'type': r_type, 'args': r_args, 'msg': msg})
            context.job_queue.run_once(send_reminder_job, target, chat_id=chat_id, data=msg, name=str(res.inserted_id))
            await update.message.reply_text(f"✅ Today Alert Saved.")

    except Exception as e:
        print(e)
        await update.message.reply_text("❌ Format Error.")

async def post_init(application: ApplicationBuilder):
    await restore_reminders(application)

if __name__ == '__main__':
    keep_alive()
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("backup", backup))
    application.add_handler(CommandHandler("journal", journal_command))
    application.add_handler(CommandHandler("pnl", pnl_command))
    application.add_handler(CommandHandler("reminder", set_reminder))
    application.add_handler(CommandHandler("jobs", list_jobs))
    application.add_handler(CommandHandler("kill", delete_job))
    application.add_handler(CommandHandler("help", help_command))
    
    application.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_restore))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))

    print("🤖 TRADING BOT (FINAL CLOUD EDITION) RUNNING...")
    application.run_polling()