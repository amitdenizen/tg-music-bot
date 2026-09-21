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


def download_audio_stream(url: str, chat_folder: str, logger: "YTDLLogger", queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    downloaded_files = set()

    def pp_hook(d):
        if d.get('status') == 'finished':
            info = d.get('info_dict', {})
            fp = info.get('filepath')
            if fp and fp.endswith('.mp3') and fp not in downloaded_files:
                downloaded_files.add(fp)
                # Send the filepath back to the main async thread securely
                loop.call_soon_threadsafe(queue.put_nowait, fp)

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
        'noplaylist': False,   # download whole playlist
        'ignoreerrors': True,  # skip failed videos in playlist
        'quiet': True,
        'logger': logger,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    finally:
        # Send a None marker to signal the download thread has completely finished
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "youtube.com" not in text and "youtu.be" not in text:
        await update.message.reply_text(
            "That doesn't look like a valid YouTube link. Please send a YouTube video or playlist link."
        )
        return

    status_msg = await update.message.reply_text("⏳ Download started, getting tracks...")

    chat_folder = os.path.join(DOWNLOAD_DIR, str(update.effective_chat.id))
    os.makedirs(chat_folder, exist_ok=True)

    logger = YTDLLogger()
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Start the downloading process in a background thread so it doesn't freeze the bot
    dl_task = asyncio.to_thread(download_audio_stream, text, chat_folder, logger, queue, loop)

    tracks_sent = 0
    while True:
        # Wait until yt-dlp finishes downloading one song and pushes it to the queue
        filepath = await queue.get()
        
        if filepath is None:
            # yt-dlp finished the entire playlist
            break

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
                tracks_sent += 1

                # Update the status message to show it's working through the playlist
                try:
                    await status_msg.edit_text(f"⏳ Downloading playlist... Sent {tracks_sent} track(s) so far.")
                except:
                    pass  # Ignore Telegram error if text is exactly the same
                
                await asyncio.sleep(1)  # small gap to avoid Telegram's flood limit
        except Exception as e:
            await update.message.reply_text(f"⚠️ Couldn't send '{os.path.basename(filepath)}': {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)  # delete immediately to save laptop/phone storage

    # Wait for the background thread to exit cleanly
    await dl_task

    if tracks_sent == 0:
        details = "\n".join(logger.messages[-3:]) if logger.messages else "No details captured."
        await status_msg.edit_text(f"❌ Couldn't download any audio.\n\nDetails:\n{details}")
    else:
        await status_msg.edit_text(f"🎉 Done! All {tracks_sent} track(s) have been sent.")


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
