"""SoundCloud Telegram bot - entry point and settings.

Edit the settings below (or set the same names as environment variables on Railway).
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys

# =========================== SETTINGS ===========================
BOT_TOKEN = "8872604133:AAEW5KslZlPHCZbgFE-H3Hh4PEEnuSZaZgc"   # from @BotFather (env var BOT_TOKEN overrides this)

MAX_CONCURRENT_JOBS = 3                  # downloads running at the same time (the rest wait in a queue)
MAX_JOBS_PER_USER = 2                    # active downloads allowed per user
MAX_PLAYLIST_TRACKS = 25                 # max tracks per playlist request in private chats
MAX_PLAYLIST_TRACKS_GROUP = 10           # smaller limit in groups (Telegram rate limits)
MAX_FILE_MB = 49                         # Telegram Bot API upload limit is 50 MB
BOT_API_URL = ""                         # optional self-hosted Bot API server (then set MAX_FILE_MB up to ~1900)
AUTO_UPDATE_YTDLP = True                 # upgrade yt-dlp to the latest version on every start
LOG_LEVEL = "INFO"
# ================================================================


def update_ytdlp() -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet",
             "--disable-pip-version-check", "yt-dlp[default]"],
            timeout=180, check=False,
        )
    except Exception as exc:  # the bot still works with the version baked into the image
        logging.getLogger("main").warning("yt-dlp update skipped: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("BOT_TOKEN") or BOT_TOKEN
    if not token or token.startswith("PUT_"):
        sys.exit("BOT_TOKEN is not set: put your token in main.py or in the BOT_TOKEN variable.")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg was not found in PATH.")

    if AUTO_UPDATE_YTDLP:
        update_ytdlp()

    from bot import Settings, run  # imported after the update so the new yt-dlp is used

    asyncio.run(run(Settings(
        token=token,
        max_concurrent_jobs=MAX_CONCURRENT_JOBS,
        max_jobs_per_user=MAX_JOBS_PER_USER,
        max_file_mb=MAX_FILE_MB,
        bot_api_url=BOT_API_URL,
        max_playlist_tracks=MAX_PLAYLIST_TRACKS,
        max_playlist_tracks_group=MAX_PLAYLIST_TRACKS_GROUP,
    )))


if __name__ == "__main__":
    main()
