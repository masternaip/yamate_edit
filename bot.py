import os
import subprocess
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 5000))
# Ang URL ng iyong Render web service pagkatapos i-deploy (halimbawa: https://yung-app-mo.onrender.com)
RENDER_URL = os.getenv("RENDER_URL") 

app_flask = Flask(__name__)

# I-setup ang Telegram Bot Application (Walang run_polling para sa webhook)
telegram_app = Application.builder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **Cloud Video Editor Bot (Render)**\n\n"
        "Mag-send ng video at gamitin ang mga commands:\n"
        "• `/trim [simula] [haba]` (Hal: `/trim 0 10`)\n"
        "• `/mute` (Tanggalin ang audio)\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

last_video_path = "user_input.mp4"

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_path
    video_file = await update.message.video.get_file()
    await video_file.download_to_drive(last_video_path)
    await update.message.reply_text("✅ Natanggap na ang video sa cloud! Pwede nang i-type ang command.")

async def cmd_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_path
    if not os.path.exists(last_video_path):
        await update.message.reply_text("⚠️ Mag-send muna ng video.")
        return
    args = context.args
    start_time = args[0] if len(args) > 0 else "0"
    duration = args[1] if len(args) > 1 else "10"
    output_path = "trimmed.mp4"
    
    subprocess.run(["ffmpeg", "-y", "-ss", start_time, "-i", last_video_path, "-t", duration, "-c", "copy", output_path], check=True)
    await update.message.reply_video(video=open(output_path, "rb"), caption="Eto na ang na-trim na video!")

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_path
    if not os.path.exists(last_video_path):
        await update.message.reply_text("⚠️ Mag-send muna ng video.")
        return
    output_path = "muted.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", last_video_path, "-an", "-vcodec", "copy", output_path], check=True)
    await update.message.reply_video(video=open(output_path, "rb"), caption="Eto na ang video na walang tunog!")

# Magdagdag ng handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("trim", cmd_trim))
telegram_app.add_handler(CommandHandler("mute", cmd_mute))
telegram_app.add_handler(MessageHandler(filters.VIDEO, handle_video))

@app_flask.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Sasalo ng updates mula sa Telegram at ipapasa sa bot application"""
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    # Patakbuhin ang async telegram app sa loob ng flask request
    import asyncio
    asyncio.run(telegram_app.process_update(update))
    return "ok", 200

@app_flask.route("/")
def index():
    return "Bot is active and running on Render!", 200

if __name__ == "__main__":
    # I-set ang webhook sa Telegram bago magsimula ang Flask server
    import requests
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/{TOKEN}"
        requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}")
    
    app_flask.run(host="0.0.0.0", port=PORT)
