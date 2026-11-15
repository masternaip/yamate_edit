import os
import subprocess
from flask import Flask, request
from openai import OpenAI  # Ginagamit para sa Vyce AI API
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 5000))
RENDER_URL = os.getenv("RENDER_URL")

# --- KONEKSIYON SA VYCE AI ---
vyce_client = OpenAI(
    api_key=os.getenv("VYCE_API_KEY"),
    base_url="https://api.vyceai.com/v1"  # Siguraduhing ito ang tamang base URL sa docs nila
)

app_flask = Flask(__name__)
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

# Command para tawagin ang Vyce AI (halimbawa: /ai gawan mo ako ng caption para sa TikTok)
async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_prompt = " ".join(context.args)
    if not user_prompt:
        await update.message.reply_text("⚠️ Maglagay ng prompt. Halimbawa: `/ai Gumawa ng caption para sa sunset video`")
        return
        
    await update.message.reply_text("🤖 Nag-iisip ang Vyce AI...")
    try:
        response = vyce_client.chat.completions.create(
            model="gpt-3.5-turbo",  # Palitan ito ng tamang model name na nakalagay sa Vyce AI dashboard mo
            messages=[{"role": "user", "content": user_prompt}]
        )
        ai_reply = response.choices[0].message.content
        await update.message.reply_text(ai_reply)
    except Exception as e:
        await update.message.reply_text(f"❌ May error sa Vyce AI: {str(e)}")

# (Dito nakalagay ang iba pang video/trim handlers mo...)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("ai", cmd_ai))

@app_flask.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Sasalo ng updates mula sa Telegram at ipapasa sa bot application"""
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    import asyncio
    
    async def process():
        # I-initialize ang telegram application kung hindi pa naka-initialize
        if not telegram_app.running:
            await telegram_app.initialize()
            await telegram_app.start()
        await telegram_app.process_update(update)
        
    asyncio.run(process())
    return "ok", 200 

if __name__ == "__main__":
    import requests
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/{TOKEN}"
        requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={webhook_url}")
    app_flask.run(host="0.0.0.0", port=PORT)
