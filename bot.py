import os
import subprocess
import requests
import asyncio
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 5000))
RENDER_URL = os.getenv("RENDER_URL")
VYCE_API_KEY = os.getenv("VYCE_API_KEY")

app_flask = Flask(__name__)

# I-buildu ang Telegram application
telegram_app = Application.builder().token(TOKEN).build()

@app_flask.route("/")
def index():
    return "Bot is running and active!", 200

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **AI Video Editor Bot (Render)**\n\n"
        "• Mag-send ng video para i-edit (gamitin ang `/trim` o `/mute`)\n"
        "• Mag-type ng `/ai [prompt]` para gumamit ng Vyce AI para sa captions/scripts!"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_prompt = " ".join(context.args)
    if not user_prompt:
        await update.message.reply_text("⚠️ Maglagay ng prompt. Halimbawa: `/ai Gumawa ng caption para sa sunset video`")
        return
        
    await update.message.reply_text("🤖 Nag-iisip ang Vyce AI...")
    try:
        headers = {
            "Authorization": f"Bearer {VYCE_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": user_prompt}]
        }
        
        response = requests.post("https://api.vyceai.com/v1/chat/completions", json=payload, headers=headers, timeout=30)
        
        if response.status_code == 200:
            res_data = response.json()
            ai_reply = res_data["choices"][0]["message"]["content"]
            await update.message.reply_text(ai_reply)
        else:
            await update.message.reply_text(f"❌ Vyce AI Error ({response.status_code}): {response.text}")
            
    except Exception as e:
        await update.message.reply_text(f"❌ May error sa koneksyon: {str(e)}")

# I-register ang handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("ai", cmd_ai))

# Global initialization flag para sa Telegram app
is_initialized = False

@app_flask.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    global is_initialized
    json_data = request.get_json(force=True)
    if not json_data:
        return "bad request", 400

    update = Update.de_json(json_data, telegram_app.bot)
    
    # Patakbuhin ang initialization at process_update sa loob ng isang ligtas na background task o thread
    import threading

    def run_in_background():
        global is_initialized
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def background_process():
            if not is_initialized:
                await telegram_app.initialize()
                await telegram_app.start()
                is_initialized = True
            await telegram_app.process_update(update)

        try:
            loop.run_until_complete(background_process())
        finally:
            loop.close()

    t = threading.Thread(target=run_in_background)
    t.start()

    return "ok", 200

if __name__ == "__main__":
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/{TOKEN}"
        requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}")
    app_flask.run(host="0.0.0.0", port=PORT)
