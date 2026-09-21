import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pytubefix import YouTube, Playlist
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
    """Downloads audio from a YouTube video/playlist using pytubefix.
    Returns the list of final audio file paths, in download order."""
    downloaded_files = []

    try:
        if 'playlist' in url.lower() or '&list=' in url.lower():
            pl = Playlist(url)
            videos = pl.videos
        else:
            videos = [YouTube(url)]
            
        for yt in videos:
            try:
                ys = yt.streams.get_audio_only()
                if ys:
                    file_path = ys.download(output_path=chat_folder)
                    downloaded_files.append(file_path)
                else:
                    logger.warning(f"No audio stream found for {yt.video_id}")
            except Exception as e:
                logger.error(f"Failed to download video: {e}")
                
    except Exception as e:
        logger.error(f"Failed to fetch metadata: {e}")
        
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
