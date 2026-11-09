import os, sqlite3, datetime, logging, asyncio
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# Render (Python 3.14) Event Loop Fix
try:
    asyncio.set_event_loop(asyncio.new_event_loop())
except Exception:
    pass

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN")
AI_API_KEY  = os.getenv("AI_API_KEY")
CHANNEL     = os.getenv("CHANNEL", "@YourChannel")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "10"))
MODEL       = os.getenv("MODEL", "openai/gpt-oss-120b")
DB          = "database.db"

last_code = {}   # user_id -> code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("codebot")

# ---------- DB ----------
def db():
    return sqlite3.connect(DB)

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            queries_today INTEGER DEFAULT 0,
            last_date TEXT,
            total INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, mode TEXT, code TEXT,
            response TEXT, ts TEXT)""")

def today():
    return datetime.date.today().isoformat()

def get_user(uid):
    with db() as conn:
        row = conn.execute(
            "SELECT user_id, queries_today, last_date, total FROM users WHERE user_id=?",
            (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users(user_id,queries_today,last_date,total) VALUES(?,0,?,0)",
                         (uid, today()))
            return (uid, 0, today(), 0)
        if row[2] != today():
            conn.execute("UPDATE users SET queries_today=0, last_date=? WHERE user_id=?",
                         (today(), uid))
            return (uid, 0, today(), row[3])
        return row

def inc_user(uid):
    with db() as conn:
        conn.execute("UPDATE users SET queries_today=queries_today+1, total=total+1, last_date=? WHERE user_id=?",
                     (today(), uid))

def save_history(uid, mode, code, resp):
    with db() as conn:
        conn.execute("INSERT INTO history(user_id,mode,code,response,ts) VALUES(?,?,?,?,?)",
                     (uid, mode, code[:1500], resp[:4000], datetime.datetime.now().isoformat()))

# ---------- AI ----------
PROMPTS = {
    "explain": (
        "Tum ek senior developer ho. Neeche diye code ko Hindi/Hinglish me simple explain karo. "
        "Format: 1) Ye code kya karta hai (2-3 line) 2) Important parts line-by-line 3) Language kaunsa hai. "
        "Short rakho, faltu baat mat likho."
    ),
    "fix": (
        "Tum ek senior debugger ho. Neeche diye code ya error me bug dhundo. "
        "Format: 1) Bug kya hai 2) Fixed code 3) 1 line reason. Hindi/Hinglish me."
    ),
    "improve": (
        "Tum ek senior developer ho. Neeche diye code ko improve karo: performance, readability, best practices. "
        "Format: 1) Improved code 2) Kya change kiya (3-4 points). Hindi/Hinglish me."
    ),
}

def ask_ai(mode, code):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PROMPTS[mode]},
            {"role": "user", "content": code},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    
    with httpx.Client() as client:
        response = client.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

# ---------- UI ----------
def kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Explain", callback_data="explain"),
            InlineKeyboardButton("Fix Bug", callback_data="fix"),
            InlineKeyboardButton("Improve", callback_data="improve"),
        ],
        [InlineKeyboardButton("Channel Join", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
    ])

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)
    await update.message.reply_text(
        "Code Explainer Bot\n\n"
        "Code ya error paste karo — Hindi me explanation, bug fix, improvement.\n\n"
        "Commands:\n"
        "/explain - code samjho\n"
        "/fix - bug/error fix\n"
        "/improve - better code\n"
        "/limit - bachi queries\n"
        "/help - guide\n\n"
        f"Channel: {CHANNEL}\nDaily free limit: {DAILY_LIMIT}"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Code ya error message paste karo, phir buttons se mode chuno.\n"
        "Ya direct: /explain <code> | /fix <error> | /improve <code>\n\n"
        "/limit - aaj bachi queries"
    )

async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, q, _, t = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"Aaj bachi queries: {max(0, DAILY_LIMIT - q)}/{DAILY_LIMIT}\nTotal queries: {t}"
    )

async def run_mode(chat_id, context, uid, mode, code):
    _, q, _, _ = get_user(uid)
    if q >= DAILY_LIMIT:
        await context.bot.send_message(
            chat_id, f"Aaj ki limit khatam ({DAILY_LIMIT}). Kal wapas aao.\nChannel: {CHANNEL}")
        return

    m = await context.bot.send_message(chat_id, "Processing...")
    try:
        resp = ask_ai(mode, code)
    except httpx.HTTPStatusError as e:
        log.exception("AI HTTP error")
        await m.edit_text(f"AI error: {e.response.status_code}. API key ya model check karo.")
        return
    except Exception as e:
        log.exception("AI error")
        await m.edit_text(f"Error: {str(e)[:200]}")
        return

    inc_user(uid)
    save_history(uid, mode, code, resp)
    _, q, _, _ = get_user(uid)
    out = resp + f"\n\nBachi queries: {max(0, DAILY_LIMIT - q)}/{DAILY_LIMIT}"

    if len(out) <= 4000:
        await m.edit_text(out)
    else:
        await m.edit_text(out[:4000] + "\n...(truncated)")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""
    if text.startswith("/"):
        return
    if len(text.strip()) < 5:
        await update.message.reply_text("Thoda bada code ya error paste karo.")
        return
    last_code[uid] = text
    get_user(uid)
    await update.message.reply_text("Kaunsa mode? Neeche button dabao.", reply_markup=kb())

async def _mode_cmd(update, context, mode):
    uid = update.effective_user.id
    code = " ".join(context.args) if context.args else last_code.get(uid)
    if not code:
        await update.message.reply_text("Pehle code paste karo, ya command ke baad code likho.")
        return
    await run_mode(update.effective_chat.id, context, uid, mode, code)

async def explain_cmd(update, context): await _mode_cmd(update, context, "explain")
async def fix_cmd(update, context):     await _mode_cmd(update, context, "fix")
async def improve_cmd(update, context): await _mode_cmd(update, context, "improve")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    code = last_code.get(uid)
    if not code:
        await q.message.reply_text("Pehle code paste karo.")
        return
    await run_mode(q.message.chat_id, context, uid, q.data, code)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as conn:
        u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        d = conn.execute("SELECT COUNT(*) FROM history WHERE ts LIKE ?", (today() + "%",)).fetchone()[0]
    await update.message.reply_text(f"Users: {u}\nTotal queries: {t}\nAaj: {d}")

async def error_handler(update, context):
    log.exception("Handler error", exc_info=context.error)

# ---------- MAIN ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(CommandHandler("explain", explain_cmd))
    app.add_handler(CommandHandler("fix", fix_cmd))
    app.add_handler(CommandHandler("improve", improve_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
