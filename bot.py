#!/usr/bin/env python3
import os
import sys
import shutil
import sqlite3
import logging
import asyncio
from functools import wraps
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------------------
# Logging configuration: Write logs to a file and keep the last 30 days.
# ------------------------------------------------------------------------------
from logging.handlers import TimedRotatingFileHandler

log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log_handler = TimedRotatingFileHandler("bot.log", when="D", interval=1, backupCount=30)
log_handler.setFormatter(log_formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# Also log to console (optional)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# ------------------------------------------------------------------------------
# Conversation States
# ------------------------------------------------------------------------------
(
    WAIT_FILENAME,  # 0: waiting for file name (if not provided)
    WAIT_CATEGORY,  # 1: waiting for category (movie or tv)
    WAIT_MOVIE_DIR,  # 2: waiting for movie directory name
    WAIT_TV_NEW_EXISTING,  # 3: waiting for TV new/existing selection
    WAIT_TV_NEW_NAME,  # 4: waiting for new TV series name
    WAIT_TV_NEW_SEASON_EPISODE,  # 5: waiting for season and episode input for new series (format: season,episode)
    WAIT_TV_EXISTING_SELECTION,  # 6: waiting for user to pick an existing TV series
    WAIT_TV_EXISTING_SEASON,  # 7: waiting for season selection for an existing series (list of seasons plus new option)
    WAIT_TV_EXISTING_EPISODE,  # 8: waiting for episode number (if an existing season was chosen)
) = range(9)

# ------------------------------------------------------------------------------
# Directories & Database File (absolute paths)
# ------------------------------------------------------------------------------
MOVIES_DIR = "/app/movies"
TV_DIR = "/app/tv"
DOWNLOADS_DIR = "/app/downloads"
DB_FILE = "/app/db/tv_database.db"
APPROVED_USERS_FILE = "/app/config/approved_users.txt"


# ------------------------------------------------------------------------------
# Database Functions
# ------------------------------------------------------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Table for TV series (each series has a unique id, name, and directory)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            directory TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Table for episodes; each row represents one episode belonging to a series.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tv_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(series_id) REFERENCES tv_series(id)
        )
        """
    )
    conn.commit()
    conn.close()
    logger.info("Database initialized.")


def add_tv_series(name: str, directory: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tv_series (name, directory) VALUES (?, ?)", (name, directory)
    )
    conn.commit()
    series_id = c.lastrowid
    conn.close()
    logger.info(f"Added new TV series: {name} with id {series_id}")
    return series_id


def add_tv_episode(
    series_id: int, season: int, episode: int, file_path: str = None
) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tv_episodes (series_id, season, episode, file_path) VALUES (?, ?, ?, ?)",
        (series_id, season, episode, file_path),
    )
    conn.commit()
    episode_id = c.lastrowid
    conn.close()
    logger.info(
        f"Added episode: Series ID {series_id}, Season {season}, Episode {episode}"
    )
    return episode_id


def get_tv_series_list() -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id, name, directory, created_at FROM tv_series ORDER BY created_at DESC"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_tv_seasons(series_id: int) -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT season FROM tv_episodes WHERE series_id = ? ORDER BY season",
        (series_id,),
    )
    seasons = [row[0] for row in c.fetchall()]
    conn.close()
    return seasons


# ------------------------------------------------------------------------------
# Approved Users and Decorator
# ------------------------------------------------------------------------------
approved_users = set()


def load_approved_users() -> set:
    users = set()
    if not os.path.exists(APPROVED_USERS_FILE):
        logger.warning(
            f"Approved users file not found: {APPROVED_USERS_FILE}. No user is approved."
        )
        return users
    with open(APPROVED_USERS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    users.add(int(line))
                except ValueError:
                    logger.warning(f"Invalid user id in approved users file: {line}")
    logger.info(f"Approved users loaded: {users}")
    return users


def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in approved_users:
            logger.warning(
                "Unauthorized access attempt from user: %s",
                user.id if user else "Unknown",
            )
            if update.message:
                await update.message.reply_text(
                    "You are not authorized to use this bot."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "You are not authorized.", show_alert=True
                )
            return ConversationHandler.END
        return await func(update, context)

    return wrapped


# ------------------------------------------------------------------------------
# Background Download Function (with Telethon fallback)
# ------------------------------------------------------------------------------
async def background_download(video, chat_id, message_id) -> str:
    # Determine the file extension.
    ext = None
    if hasattr(video, "file_name") and video.file_name:
        ext = os.path.splitext(video.file_name)[1]
    if not ext and hasattr(video, "mime_type") and video.mime_type:
        mime_to_ext = {
            "video/mp4": ".mp4",
            "video/x-matroska": ".mkv",
            "video/quicktime": ".mov",
        }
        ext = mime_to_ext.get(video.mime_type)
    if not ext:
        ext = ".mp4"
    temp_path = os.path.join(DOWNLOADS_DIR, f"temp_{video.file_id}{ext}")
    logger.info(f"Downloading media to temporary path: {temp_path}")
    try:
        file = await video.get_file()
        await file.download_to_drive(custom_path=temp_path)
        logger.info("Download via Bot API succeeded.")
        return temp_path
    except Exception as e:
        if "File is too big" in str(e):
            logger.info("File too big via Bot API; falling back to Telethon.")
            TELETHON_API_ID = os.environ.get("TELETHON_API_ID")
            TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH")
            BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not (TELETHON_API_ID and TELETHON_API_HASH and BOT_TOKEN):
                raise Exception("Telethon fallback not fully configured.")
            try:
                telethon_api_id = int(TELETHON_API_ID)
            except Exception as te:
                raise Exception("Invalid TELETHON_API_ID provided.")
            try:
                from telethon import TelegramClient
            except ImportError:
                raise Exception("Telethon library not installed.")
            session = os.environ.get("TELETHON_SESSION", "bot_telethon.session")
            client = TelegramClient(session, telethon_api_id, TELETHON_API_HASH)
            try:
                await client.start(bot_token=BOT_TOKEN)
            except Exception as te:
                raise Exception(f"Telethon client failed to start: {te}")
            try:
                msg = await client.get_messages(chat_id, ids=message_id)
            except Exception as te:
                await client.disconnect()
                raise Exception(f"Failed to retrieve message via Telethon: {te}")
            try:
                await client.download_file(msg, file=temp_path, part_size_kb=2048)
            except Exception as te:
                await client.disconnect()
                raise Exception(f"Telethon failed to download media: {te}")
            await client.disconnect()
            logger.info("Download via Telethon succeeded.")
            return temp_path
        else:
            raise e


# ------------------------------------------------------------------------------
# Helper: Rename file for TV episodes (format: SeriesName-SXXEXX.ext)
# ------------------------------------------------------------------------------
def format_tv_filename(
    series_name: str, season: int, episode: int, orig_path: str
) -> str:
    # Extract extension from orig_path
    _, ext = os.path.splitext(orig_path)
    return f"{series_name}-S{season:02d}E{episode:02d}{ext}"


# ------------------------------------------------------------------------------
# Conversation Handlers
# ------------------------------------------------------------------------------
@restricted
async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point when a video is received.
    Starts asynchronous download and prompts for category.
    """
    if not update.message or not update.message.video:
        return ConversationHandler.END
    video = update.message.video
    # Start download in background
    context.user_data["download_task"] = asyncio.create_task(
        background_download(video, update.message.chat.id, update.message.message_id)
    )
    # For logging purposes, log the original filename if available.
    orig_fname = video.file_name if hasattr(video, "file_name") else None
    logger.info(f"Video received. Original file name: {orig_fname}")
    if orig_fname:
        context.user_data["desired_filename"] = orig_fname
        keyboard = [
            [
                InlineKeyboardButton("Movie", callback_data="category_movie"),
                InlineKeyboardButton("TV", callback_data="category_tv"),
            ]
        ]
        await update.message.reply_text(
            f"Video received (file name: {orig_fname}). Please choose: Movie or TV?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return WAIT_CATEGORY
    else:
        await update.message.reply_text(
            "Video received. Please provide a file name (include extension):"
        )
        return WAIT_FILENAME


@restricted
async def filename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(
            "File name cannot be empty. Please provide a valid file name:"
        )
        return WAIT_FILENAME
    context.user_data["desired_filename"] = text
    logger.info(f"User provided desired file name: {text}")
    keyboard = [
        [
            InlineKeyboardButton("Movie", callback_data="category_movie"),
            InlineKeyboardButton("TV", callback_data="category_tv"),
        ]
    ]
    await update.message.reply_text(
        f"File name set to: {text}. Now choose: Movie or TV?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAIT_CATEGORY


@restricted
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Category selected: {data}")
    if data == "category_movie":
        await query.edit_message_text("Please provide the movie directory name:")
        return WAIT_MOVIE_DIR
    elif data == "category_tv":
        keyboard = [
            [
                InlineKeyboardButton("New Series", callback_data="tv_new"),
                InlineKeyboardButton("Existing Series", callback_data="tv_existing"),
            ]
        ]
        await query.edit_message_text(
            "Is this TV series new or existing?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return WAIT_TV_NEW_EXISTING
    else:
        await query.edit_message_text("Invalid selection. Operation aborted.")
        return ConversationHandler.END


@restricted
async def movie_dir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dir_name = update.message.text.strip()
    if not dir_name:
        await update.message.reply_text(
            "Directory name cannot be empty. Please try again:"
        )
        return WAIT_MOVIE_DIR
    movie_dir_path = os.path.join(MOVIES_DIR, dir_name)
    os.makedirs(movie_dir_path, exist_ok=True)
    logger.info(f"Movie directory set to: {movie_dir_path}")
    # Wait for download to finish.
    try:
        temp_path = await context.user_data["download_task"]
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return ConversationHandler.END
    desired_filename = context.user_data.get("desired_filename") or os.path.basename(
        temp_path
    )
    final_path = os.path.join(movie_dir_path, desired_filename)
    try:
        shutil.move(temp_path, final_path)
        await update.message.reply_text(f"Movie file moved to: {final_path}")
        logger.info(f"Movie file moved to: {final_path}")
    except Exception as e:
        await update.message.reply_text(f"Error moving file: {e}")
        logger.error(f"Error moving file: {e}")
    return ConversationHandler.END


# -------------------------------
# TV Series Handlers (New Series)
# -------------------------------
@restricted
async def tv_new_existing_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"TV new/existing selection: {data}")
    if data == "tv_new":
        await query.edit_message_text("Please provide the new TV series name:")
        return WAIT_TV_NEW_NAME
    elif data == "tv_existing":
        # List existing TV series from database
        series_list = get_tv_series_list()
        if not series_list:
            await query.edit_message_text(
                "No existing TV series found. Please provide a new TV series name:"
            )
            return WAIT_TV_NEW_NAME
        # Show one series at a time (pagination)
        series = series_list[0]
        text = f"TV Series: {series[1]}\nCreated At: {series[3]}"
        keyboard = [
            [InlineKeyboardButton("Select", callback_data=f"tv_select:{series[0]}")]
        ]
        if len(series_list) > 1:
            keyboard.append([InlineKeyboardButton("Next", callback_data="tv_next")])
        await query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        # Store the list in user_data for pagination
        context.user_data["tv_series_list"] = series_list
        context.user_data["tv_series_index"] = 0
        return WAIT_TV_EXISTING_SELECTION
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END


@restricted
async def tv_new_name_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # For new series, store series name and prompt for season and episode numbers.
    series_name = update.message.text.strip()
    if not series_name:
        await update.message.reply_text(
            "Series name cannot be empty. Please provide a valid name:"
        )
        return WAIT_TV_NEW_NAME
    context.user_data["tv_series_name"] = series_name
    logger.info(f"New TV series name: {series_name}")
    # Automatically set the directory for the series
    series_dir = os.path.join(TV_DIR, series_name)
    os.makedirs(series_dir, exist_ok=True)
    context.user_data["tv_series_directory"] = series_dir
    await update.message.reply_text(
        "Please provide season and episode numbers in the format: season,episode (e.g. 1,13):"
    )
    return WAIT_TV_NEW_SEASON_EPISODE


@restricted
async def tv_new_season_episode_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # Parse season and episode from the user's input.
    text = update.message.text.strip()
    try:
        season_str, episode_str = text.split(",")
        season = int(season_str.strip())
        episode = int(episode_str.strip())
    except Exception:
        await update.message.reply_text(
            "Invalid format. Please provide in the format: season,episode (e.g. 1,13):"
        )
        return WAIT_TV_NEW_SEASON_EPISODE
    # Create new series and add episode record.
    series_name = context.user_data["tv_series_name"]
    series_dir = context.user_data["tv_series_directory"]
    series_id = add_tv_series(series_name, series_dir)
    add_tv_episode(series_id, season, episode)
    # Wait for download to finish.
    try:
        temp_path = await context.user_data["download_task"]
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return ConversationHandler.END
    # Rename file according to format.
    new_fname = format_tv_filename(series_name, season, episode, temp_path)
    final_path = os.path.join(series_dir, new_fname)
    try:
        shutil.move(temp_path, final_path)
        await update.message.reply_text(f"TV episode file moved to: {final_path}")
        logger.info(f"TV episode file moved to: {final_path}")
    except Exception as e:
        await update.message.reply_text(f"Error moving file: {e}")
        logger.error(f"Error moving file: {e}")
    return ConversationHandler.END


# -------------------------------
# TV Series Handlers (Existing Series)
# -------------------------------
@restricted
async def tv_existing_selection_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # Handle pagination or selection from existing series list.
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "tv_next":
        # Paginate: show next series
        series_list = context.user_data.get("tv_series_list", [])
        index = context.user_data.get("tv_series_index", 0) + 1
        if index >= len(series_list):
            index = 0
        context.user_data["tv_series_index"] = index
        series = series_list[index]
        text = f"TV Series: {series[1]}\nCreated At: {series[3]}"
        keyboard = [
            [InlineKeyboardButton("Select", callback_data=f"tv_select:{series[0]}")]
        ]
        if len(series_list) > 1:
            keyboard.append([InlineKeyboardButton("Next", callback_data="tv_next")])
        await query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_TV_EXISTING_SELECTION
    elif data.startswith("tv_select:"):
        try:
            series_id = int(data.split(":")[1])
        except Exception:
            await query.edit_message_text("Invalid selection.")
            return ConversationHandler.END
        context.user_data["tv_series_id"] = series_id
        # Retrieve series name from database (or from the stored list)
        series_list = context.user_data.get("tv_series_list", [])
        series_name = None
        for s in series_list:
            if s[0] == series_id:
                series_name = s[1]
                break
        if not series_name:
            await query.edit_message_text("Series not found.")
            return ConversationHandler.END
        context.user_data["tv_series_name"] = series_name
        # Now query for existing seasons for this series.
        seasons = get_tv_seasons(series_id)
        logger.info(f"Existing seasons for series {series_name}: {seasons}")
        if seasons:
            # Build inline buttons for each season and an option for "New Season"
            keyboard = []
            for s in seasons:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"Season {s}", callback_data=f"tv_existing_season:{s}"
                        )
                    ]
                )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "New Season", callback_data="tv_existing_season:new"
                    )
                ]
            )
            await query.edit_message_text(
                "Please select an existing season or choose New Season:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return WAIT_TV_EXISTING_SEASON
        else:
            # No seasons yet, so prompt for new season and episode.
            await query.edit_message_text(
                "No seasons found. Please provide season and episode numbers (format: season,episode):"
            )
            return WAIT_TV_NEW_SEASON_EPISODE
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END


@restricted
async def tv_existing_season_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # This handler is triggered by the inline button selection for season in existing series.
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "tv_existing_season:new":
        await query.edit_message_text(
            "Please provide season and episode numbers for the new season (format: season,episode):"
        )
        return WAIT_TV_NEW_SEASON_EPISODE
    elif data.startswith("tv_existing_season:"):
        try:
            season = int(data.split(":")[1])
        except Exception:
            await query.edit_message_text("Invalid season selection.")
            return ConversationHandler.END
        context.user_data["tv_season"] = season
        await query.edit_message_text(
            f"Season {season} selected. Please provide the episode number:"
        )
        return WAIT_TV_EXISTING_EPISODE
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END


@restricted
async def tv_existing_episode_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    # This handler processes the episode number for an existing season.
    text = update.message.text.strip()
    try:
        episode = int(text)
    except Exception:
        await update.message.reply_text(
            "Invalid episode number. Please provide a numeric value:"
        )
        return WAIT_TV_EXISTING_EPISODE
    series_id = context.user_data.get("tv_series_id")
    series_name = context.user_data.get("tv_series_name")
    season = context.user_data.get("tv_season")
    if not (series_id and series_name and season):
        await update.message.reply_text("Internal error: missing series data.")
        return ConversationHandler.END
    # Insert new episode record.
    add_tv_episode(series_id, season, episode)
    # Wait for download to finish.
    try:
        temp_path = await context.user_data["download_task"]
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return ConversationHandler.END
    # Format new file name.
    new_fname = format_tv_filename(series_name, season, episode, temp_path)
    series_dir = os.path.join(TV_DIR, series_name)
    os.makedirs(series_dir, exist_ok=True)
    final_path = os.path.join(series_dir, new_fname)
    try:
        shutil.move(temp_path, final_path)
        await update.message.reply_text(f"TV episode file moved to: {final_path}")
        logger.info(f"TV episode file moved to: {final_path}")
    except Exception as e:
        await update.message.reply_text(f"Error moving file: {e}")
        logger.error(f"Error moving file: {e}")
    return ConversationHandler.END


# ------------------------------------------------------------------------------
# Cancel Handler
# ------------------------------------------------------------------------------
@restricted
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Operation cancelled.")
    elif update.callback_query:
        await update.callback_query.edit_message_text("Operation cancelled.")
    return ConversationHandler.END


# ------------------------------------------------------------------------------
# Main Function
# ------------------------------------------------------------------------------
def main() -> None:
    logging.info("Bot starting up...")
    os.makedirs(MOVIES_DIR, exist_ok=True)
    os.makedirs(TV_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    init_db()
    global approved_users
    approved_users = load_approved_users()
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        logging.error(
            "No Telegram bot token provided. Set TELEGRAM_BOT_TOKEN environment variable."
        )
        sys.exit(1)
    application = Application.builder().token(TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO, video_handler)],
        states={
            WAIT_FILENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, filename_handler)
            ],
            WAIT_CATEGORY: [
                CallbackQueryHandler(category_handler, pattern="^category_")
            ],
            WAIT_MOVIE_DIR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, movie_dir_handler)
            ],
            WAIT_TV_NEW_EXISTING: [
                CallbackQueryHandler(
                    tv_new_existing_handler, pattern="^tv_(new|existing)$"
                )
            ],
            WAIT_TV_NEW_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tv_new_name_handler)
            ],
            WAIT_TV_NEW_SEASON_EPISODE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, tv_new_season_episode_handler
                )
            ],
            WAIT_TV_EXISTING_SELECTION: [
                CallbackQueryHandler(
                    tv_existing_selection_handler, pattern="^(tv_next|tv_select:.*)"
                )
            ],
            WAIT_TV_EXISTING_SEASON: [
                CallbackQueryHandler(
                    tv_existing_season_handler, pattern="^tv_existing_season:"
                )
            ],
            WAIT_TV_EXISTING_EPISODE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, tv_existing_episode_handler
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("cancel", cancel_handler))
    logging.info("Bot started polling.")
    application.run_polling()


if __name__ == "__main__":
    main()
