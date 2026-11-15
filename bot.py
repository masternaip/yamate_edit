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
import imageio_ffmpeg
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
MAX_BYTES = 45 * 1024 * 1024        # limit ng Telegram bot (50 MB) na may kaunting espasyo
HARD_LIMIT_BYTES = 150 * 1024 * 1024  # huwag mag-download ng higit dito kahit hindi alam ang laki
MAX_OPTIONS = 8
URL_RE = re.compile(r"https?://\S+")
SUPPORTED_RE = re.compile(r"(youtube\.com|youtu\.be|tiktok\.com)", re.I)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()  # may kasamang ffmpeg ang package na ito

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
        "• Mag-send ng YouTube o TikTok link, tapos pumili ng resolution na gusto mo\n"
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
def _base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "ffmpeg_location": FFMPEG_PATH,
    }
    # Opsyonal: kung may Secret File na cookies.txt sa Render, gamitin ito
    src = "/etc/secrets/cookies.txt"
    if os.path.exists(src):
        shutil.copyfile(src, "/tmp/cookies.txt")
        opts["cookiefile"] = "/tmp/cookies.txt"
    return opts


def _fsize(f, duration):
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return int(size)
    if f.get("tbr") and duration:
        return int(f["tbr"] * 1000 / 8 * duration)
    return None


def get_options(url):
    """Kunin ang lahat ng available na resolution ng link."""
    with yt_dlp.YoutubeDL(_base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    # Pinakamagandang audio-only (mas gusto ang m4a para madaling i-merge sa mp4)
    audio = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    audio.sort(key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), reverse=True)
    best_audio = audio[0] if audio else None
    audio_size = _fsize(best_audio, duration) if best_audio else 0

    # Pinakamagandang format kada (taas, fps)
    best = {}
    for f in formats:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        fps = f.get("fps") or 0
        key = (f["height"], fps > 30)
        score = ((f.get("vcodec") or "").startswith("avc1"), f.get("tbr") or 0)
        if key not in best or score > best[key][0]:
            best[key] = (score, f)

    options = []
    for (height, high_fps), (_, f) in sorted(best.items(), reverse=True):
        has_audio = f.get("acodec") not in (None, "none")
        vsize = _fsize(f, duration)
        if has_audio:
            fmt = f["format_id"]
            total = vsize
        else:
            fmt = f"{f['format_id']}+{best_audio['format_id'] if best_audio else 'bestaudio'}"
            total = (vsize + audio_size) if vsize is not None else None
        fps_text = f"{int(round(f.get('fps')))}" if high_fps else ""
        options.append({
            "label": f"{height}p{fps_text}",
            "fmt": fmt,
            "size": total,
        })

    options = options[:MAX_OPTIONS]
    if not options:
        options = [{"label": "Best na available", "fmt": "best", "size": None}]

    return {"title": info.get("title") or "Video", "options": options}


def download_video(url, fmt, tmpdir):
    opts = _base_opts()
    opts.update({
        "format": fmt,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "max_filesize": HARD_LIMIT_BYTES,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise RuntimeError("TOO_BIG")
    return max(files, key=os.path.getsize), info


def _size_text(size):
    if not size:
        return "laki: hindi alam"
    return f"~{size / (1024 * 1024):.1f} MB"


def _friendly_error(msg):
    low = msg.lower()
    if "sign in" in low or "not a bot" in low:
        return (
            "❌ Hinarang ng YouTube ang server (akala nito ay bot). "
            "Subukan ang TikTok link, o ulitin mamaya."
        )
    if "unexpected response" in low or "tiktok" in low:
        return (
            "❌ Hindi ma-access ng bot ang TikTok video na ito ngayon "
            "(maaaring hinaharang ng TikTok ang server). Subukan ulit mamaya o ibang link."
        )
    return f"❌ Hindi ma-download ang video.\n\nDetalye: {msg[:300]}"


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        return
    url = match.group(0)
    if not SUPPORTED_RE.search(url):
        await update.message.reply_text("⚠️ YouTube at TikTok links lang ang suportado.")
        return

    status = await update.message.reply_text("🔎 Hinahanap ang mga available na quality...")
    try:
        data = await asyncio.to_thread(get_options, url)
    except Exception as e:
        logger.warning("Nabigo ang paghahanap ng formats: %s", e)
        await status.edit_text(_friendly_error(str(e)))
        return

    link_id = uuid.uuid4().hex[:10]
    links = context.bot_data.setdefault("links", {})
    if len(links) > 200:
        links.clear()
    links[link_id] = {"url": url, "options": data["options"], "title": data["title"]}

    rows = []
    for i, opt in enumerate(data["options"]):
        warn = "⚠️ " if opt["size"] and opt["size"] > MAX_BYTES else ""
        rows.append([
            InlineKeyboardButton(
                f"{warn}{opt['label']}  ({_size_text(opt['size'])})",
                callback_data=f"dl:{link_id}:{i}",
            )
        ])

    await status.edit_text(
        f"🎬 {data['title'][:80]}\n\nPumili ng resolution:\n"
        f"(⚠️ = lalampas sa 45 MB na limit ng Telegram)",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, link_id, idx = query.data.split(":")
        idx = int(idx)
    except ValueError:
        return

    entry = context.bot_data.get("links", {}).get(link_id)
    if not entry or idx >= len(entry["options"]):
        await query.edit_message_text("⚠️ Luma na ang link na ito. Ipadala ulit ang link.")
        return

    opt = entry["options"][idx]
    if opt["size"] and opt["size"] > MAX_BYTES:
        await query.edit_message_text(
            f"⚠️ Masyadong malaki ang {opt['label']} ({_size_text(opt['size'])}) "
            f"para ipadala ng Telegram bot (limit: 45 MB). Pumili ng mas mababang resolution.",
            reply_markup=query.message.reply_markup,
        )
        return

    await query.edit_message_text(f"⏳ Dina-download ang {opt['label']}... Maghintay.")

    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        path, info = await asyncio.to_thread(download_video, entry["url"], opt["fmt"], tmpdir)

        if os.path.getsize(path) > MAX_BYTES:
            await query.edit_message_text(
                "⚠️ Lumampas sa 45 MB ang na-download na video, kaya hindi ito maipapadala "
                "ng Telegram bot. Pumili ng mas mababang resolution."
            )
            return

        caption = f"✅ {entry['title'][:80]}\nQuality: {opt['label']}"
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
        if "TOO_BIG" in str(e):
            await query.edit_message_text(
                "⚠️ Masyadong malaki ang video para ipadala ng Telegram bot. "
                "Pumili ng mas mababang resolution."
            )
        else:
            await query.edit_message_text(_friendly_error(str(e)))
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
