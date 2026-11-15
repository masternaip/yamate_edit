import os
import re
import uuid
import shutil
import hashlib
import asyncio
import threading
import logging
import tempfile

import yt_dlp
from flask import Flask, request
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # para hindi lumabas ang token sa logs
logging.getLogger("httpx2").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.getenv("RENDER_URL")  # hal. https://pangalan-mo.onrender.com (walang / sa dulo)

# Hiwalay na secret path para hindi na nasa URL ang mismong token
WEBHOOK_PATH = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

# Mga AI model na susubukan, nang isa-isa, hanggang may sumagot
MODELS = ["glm-5.3-flash", "qwen3.8-27b"]

# ---------- Mga setting ng downloader ----------
MAX_BYTES = 45 * 1024 * 1024  # ligtas na laki para sa 50 MB limit ng Telegram bot
URL_RE = re.compile(r"https?://\S+")
SUPPORTED_RE = re.compile(r"(youtube\.com|youtu\.be|tiktok\.com)", re.I)

# Walang ffmpeg sa Render, kaya single-file formats lang (may video at audio na)
FORMAT_HIGH = "best[ext=mp4][filesize<?45M]/best[filesize<?45M]/worst"
FORMAT_LOW = "worst[ext=mp4]/worst"

# --- KONEKSIYON SA VYCE AI (walang auto-retry para hindi mag-freeze) ---
vyce_client = AsyncOpenAI(
    api_key=os.getenv("VYCE_API_KEY"),
    base_url="https://vyceai.com/v1",
    max_retries=0,
)

app_flask = Flask(__name__)

# ---------- Isang permanenteng event loop sa background thread ----------
loop = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()

# ---------- Telegram app (webhook mode, walang polling updater) ----------
telegram_app = Application.builder().token(TOKEN).updater(None).build()


# ---------- Mga command ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 AI Video Editor Bot (Render)\n\n"
        "• Mag-send ng YouTube o TikTok link para i-download ang video "
        "(piliin ang pinakamataas o pinakamababang quality)\n"
        "• Mag-type ng /ai [prompt] para gumamit ng Vyce AI para sa captions/scripts!"
    )
    await update.message.reply_text(help_text)


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_prompt = " ".join(context.args)
    if not user_prompt:
        await update.message.reply_text(
            "⚠️ Maglagay ng prompt. Halimbawa: /ai Gumawa ng caption para sa sunset video"
        )
        return

    await update.message.reply_text("🤖 Nag-iisip ang Vyce AI... (maaaring abutin ng 30-60 segundo)")

    last_error = None
    for model in MODELS:
        try:
            response = await vyce_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": user_prompt}],
                timeout=60,
            )
            ai_reply = response.choices[0].message.content
            if not ai_reply:
                raise ValueError("Walang laman ang sagot ng AI")
            await update.message.reply_text(ai_reply)
            return
        except Exception as e:
            last_error = e
            logger.warning("Nabigo ang model %s: %s", model, e)

    await update.message.reply_text(
        f"❌ Hindi sumagot ang Vyce AI ngayon. Subukan ulit mamaya.\n\nDetalye: {str(last_error)[:300]}"
    )


# ---------- Downloader ----------
def _prepare_cookies():
    """Opsyonal: kung may Secret File na cookies.txt sa Render, gamitin ito."""
    src = "/etc/secrets/cookies.txt"
    if os.path.exists(src):
        dst = "/tmp/cookies.txt"
        shutil.copyfile(src, dst)
        return dst
    return None


def download_video(url, mode, tmpdir):
    opts = {
        "format": FORMAT_HIGH if mode == "hi" else FORMAT_LOW,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 30,
        "retries": 2,
    }
    cookies = _prepare_cookies()
    if cookies:
        opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise RuntimeError("TOO_BIG")
    path = max(files, key=os.path.getsize)
    return path, info


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return
    url = match.group(0)
    if not SUPPORTED_RE.search(url):
        await update.message.reply_text("⚠️ YouTube at TikTok links lang ang suportado.")
        return

    link_id = uuid.uuid4().hex[:10]
    links = context.bot_data.setdefault("links", {})
    if len(links) > 200:
        links.clear()
    links[link_id] = url

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬆️ Pinakamataas na quality", callback_data=f"dl:hi:{link_id}")],
            [InlineKeyboardButton("⬇️ Pinakamababang quality", callback_data=f"dl:lo:{link_id}")],
        ]
    )
    await update.message.reply_text("Pumili ng quality:", reply_markup=keyboard)


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, mode, link_id = query.data.split(":")
    except ValueError:
        return

    url = context.bot_data.get("links", {}).get(link_id)
    if not url:
        await query.edit_message_text("⚠️ Luma na ang link na ito. Ipadala ulit ang link.")
        return

    label = "pinakamataas" if mode == "hi" else "pinakamababa"
    await query.edit_message_text(f"⏳ Dina-download ({label} na quality)... Maghintay ng ilang segundo.")

    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        path, info = await asyncio.to_thread(download_video, url, mode, tmpdir)

        if os.path.getsize(path) > MAX_BYTES:
            await query.edit_message_text(
                "⚠️ Masyadong malaki ang video (higit 45 MB) para ipadala ng Telegram bot. "
                "Subukan ang pinakamababang quality."
            )
            return

        title = (info.get("title") or "Video")[:80]
        height = info.get("height")
        quality_text = f"{height}p" if height else "hindi alam"
        caption = f"✅ {title}\nQuality: {quality_text}"

        with open(path, "rb") as f:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=f,
                caption=caption,
                supports_streaming=True,
                read_timeout=180,
                write_timeout=180,
                connect_timeout=30,
            )
        await query.edit_message_text("✅ Tapos na!")
    except Exception as e:
        logger.warning("Nabigo ang download: %s", e)
        msg = str(e)
        if "TOO_BIG" in msg:
            reply = (
                "⚠️ Masyadong malaki ang video para ipadala ng Telegram bot (higit 45 MB). "
                "Subukan ang pinakamababang quality o mas maikling video."
            )
        elif "Sign in" in msg or "bot" in msg.lower():
            reply = (
                "❌ Hinarang ng YouTube ang server (akala nito ay bot). "
                "Subukan ang TikTok link, o ulitin mamaya."
            )
        else:
            reply = f"❌ Hindi ma-download ang video.\n\nDetalye: {msg[:300]}"
        await query.edit_message_text(reply)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error: %s", context.error, exc_info=context.error)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("ai", cmd_ai))
telegram_app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^dl:"))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
telegram_app.add_error_handler(on_error)


# ---------- I-initialize ang telegram app nang isang beses lang ----------
async def _startup():
    await telegram_app.initialize()
    await telegram_app.start()
    if RENDER_URL:
        await telegram_app.bot.set_webhook(
            f"{RENDER_URL}/{WEBHOOK_PATH}",
            drop_pending_updates=True,
        )
        logger.info("Webhook set: %s/<SECRET_PATH>", RENDER_URL)
    else:
        logger.error("RENDER_URL ay wala! Hindi na-set ang webhook.")


asyncio.run_coroutine_threadsafe(_startup(), loop).result(timeout=30)


def _log_future_result(fut):
    try:
        fut.result()
    except Exception:
        logger.exception("Nabigo ang pagproseso ng update")


# ---------- Flask routes ----------
@app_flask.route("/")
def index():
    return "Bot is running and active!", 200


@app_flask.route(f"/{WEBHOOK_PATH}", methods=["POST"])
def webhook():
    json_data = request.get_json(force=True, silent=True)
    if not json_data:
        return "bad request", 400

    logger.info("Update natanggap mula sa Telegram")
    update = Update.de_json(json_data, telegram_app.bot)
    fut = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
    fut.add_done_callback(_log_future_result)
    return "ok", 200


if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=PORT, threaded=True)
