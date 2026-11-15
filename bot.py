import os
import hashlib
import asyncio
import threading
import logging

from flask import Flask, request
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # para hindi lumabas ang token sa logs
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 5000))
RENDER_URL = os.getenv("RENDER_URL")  # hal. https://pangalan-mo.onrender.com (walang / sa dulo)

# Hiwalay na secret path para hindi na nasa URL ang mismong token
WEBHOOK_PATH = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

# --- KONEKSIYON SA VYCE AI (async para hindi harangan ang event loop) ---
vyce_client = AsyncOpenAI(
    api_key=os.getenv("VYCE_API_KEY"),
    base_url="https://vyceai.com/v1",
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 AI Video Editor Bot (Render)\n\n"
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

    await update.message.reply_text("🤖 Nag-iisip ang Vyce AI...")
    try:
        response = await vyce_client.chat.completions.create(
            model="qwen3.8-27b",  # Libre at mabilis. Palitan kung mawala sa Vyce Models page
            messages=[{"role": "user", "content": user_prompt}],
            timeout=30,
        )
        ai_reply = response.choices[0].message.content
        await update.message.reply_text(ai_reply)
    except Exception as e:
        logger.exception("AI command failed")
        await update.message.reply_text(f"❌ Vyce AI Error: {str(e)[:500]}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Handler error: %s", context.error, exc_info=context.error)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("ai", cmd_ai))
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
    app_flask.run(host="0.0.0.0", port=PORT)
