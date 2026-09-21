import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DOWNLOAD_DIR = "downloads"
MAX_TELEGRAM_SIZE_MB = 49  # Telegram bot upload limit is 50MB, 1MB buffer kept

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive and running 24/7!")

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs in console


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Health server error: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me any YouTube video or playlist link, "
        "and I'll extract the audio (MP3) and send it back to you."
    )


class YTDLLogger:
    """Captures yt-dlp's internal warning/error messages so we can show the
    real reason for a failure instead of a generic message."""
    def __init__(self):
        self.messages = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.messages.append(f"WARNING: {msg}")

    def error(self, msg):
        self.messages.append(f"ERROR: {msg}")


def download_audio(url: str, chat_folder: str, logger: "YTDLLogger") -> list:
    """Downloads audio from a YouTube video/playlist and converts it to MP3.
    Returns the list of final MP3 file paths, in download order."""
    downloaded_files = []

    # Snapshot existing files so we can reliably detect newly downloaded MP3s
    before_files = set(os.listdir(chat_folder)) if os.path.exists(chat_folder) else set()

    def pp_hook(d):
        if d.get('status') == 'finished':
            info = d.get('info_dict', {})
            fp = info.get('filepath')
            if fp and fp.endswith('.mp3') and fp not in downloaded_files:
                downloaded_files.append(fp)

    output_template = os.path.join(chat_folder, "%(title)s [%(id)s].%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'postprocessor_hooks': [pp_hook],
        'noplaylist': False,   # if a playlist link is given, the whole playlist gets downloaded
        'ignoreerrors': True,  # if one video in the playlist fails, the rest still continue
        'quiet': True,
        'logger': logger,      # route all warnings/errors into our logger instead of hiding them
        # 'android' client is fast and works well on mobile residential IPs
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Fallback / safety check: collect any newly generated .mp3 files in chat_folder
    if os.path.exists(chat_folder):
        for f in os.listdir(chat_folder):
            if f.endswith('.mp3') and f not in before_files:
                full_path = os.path.join(chat_folder, f)
                if full_path not in downloaded_files and os.path.isfile(full_path):
                    downloaded_files.append(full_path)

    return downloaded_files


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "youtube.com" not in text and "youtu.be" not in text:
        await update.message.reply_text(
            "That doesn't look like a valid YouTube link. Please send a YouTube video or playlist link."
        )
        return

    status_msg = await update.message.reply_text("⏳ Download started, please wait...")

    chat_folder = os.path.join(DOWNLOAD_DIR, str(update.effective_chat.id))
    os.makedirs(chat_folder, exist_ok=True)

    logger = YTDLLogger()

    try:
        files = await asyncio.to_thread(download_audio, text, chat_folder, logger)
    except Exception as e:
        details = "\n".join(logger.messages[-3:])
        extra = f"\n\nDetails:\n{details}" if details else ""
        await status_msg.edit_text(f"❌ Download failed: {e}{extra}")
        return

    if not files:
        details = "\n".join(logger.messages[-3:]) if logger.messages else "No details captured."
        await status_msg.edit_text(f"❌ Couldn't download any audio.\n\nDetails:\n{details}")
        return

    await status_msg.edit_text(f"✅ Got {len(files)} track(s), sending now...")

    for filepath in files:
        try:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb > MAX_TELEGRAM_SIZE_MB:
                await update.message.reply_text(
                    f"⚠️ '{os.path.basename(filepath)}' is {size_mb:.1f}MB, "
                    f"over the 50MB limit, skipping it."
                )
            else:
                track_title = os.path.splitext(os.path.basename(filepath))[0]
                if track_title.endswith("]") and " [" in track_title:
                    clean_title = track_title.rsplit(" [", 1)[0]
                else:
                    clean_title = track_title

                with open(filepath, 'rb') as audio_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        title=clean_title,
                        filename=os.path.basename(filepath),
                        read_timeout=120,
                        write_timeout=120,
                    )
                await asyncio.sleep(1)  # small gap to avoid Telegram's flood limit
        except Exception as e:
            # previously this error was silently swallowed - now it's reported
            await update.message.reply_text(f"⚠️ Couldn't send '{os.path.basename(filepath)}': {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)  # delete right away to save laptop storage

    await status_msg.edit_text("🎉 Done! All tracks have been sent.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Error: BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start background web server for Render health checks and 24/7 uptime pings
    web_thread = threading.Thread(target=run_health_server, daemon=True)
    web_thread.start()

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
