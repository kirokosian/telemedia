#!/usr/bin/env python3
import os
import sys
import shutil
import sqlite3
import logging
import asyncio
from functools import wraps
from datetime import datetime

# ---------------------------------------------------------------------------
# Add sickbeard_mp4_automator to sys.path so it can be imported properly.
# ---------------------------------------------------------------------------
sickbeard_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "sickbeard_mp4_automator")
if sickbeard_path not in sys.path:
    sys.path.insert(0, sickbeard_path)

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
from logging.handlers import TimedRotatingFileHandler

# =============================================================================
# Ensure /app/config exists
# =============================================================================
os.makedirs("/app/config", exist_ok=True)

# =============================================================================
# Logging Configuration (log file under /app/config)
# =============================================================================
log_file_path = os.path.join("/app/config", "bot.log")
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_handler = TimedRotatingFileHandler(log_file_path, when="D", interval=1, backupCount=10)
log_handler.setFormatter(log_formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
# Also log to console (optional)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# =============================================================================
# Global Job Queue and Progress Tracking
# =============================================================================
JOB_QUEUE = asyncio.Queue()         # Jobs will be queued here.
ACTIVE_JOBS = set()                 # Set of job_ids that are actively processing.
PROGRESS_DICT = {}                  # Maps job_id -> progress percentage (0-100).

# =============================================================================
# Conversation States
# =============================================================================
(
    WAIT_FILENAME,                 # 0: waiting for file name (if not provided by video)
    WAIT_CATEGORY,                 # 1: waiting for category (movie or tv)
    WAIT_MOVIE_DIR,                # 2: waiting for movie directory name input
    WAIT_TV_NEW_EXISTING,          # 3: waiting for TV new/existing selection
    WAIT_TV_NEW_NAME,              # 4: waiting for new TV series name
    WAIT_TV_NEW_SEASON_EPISODE,    # 5: waiting for season,episode input for new TV series
    WAIT_TV_EXISTING_SELECTION,    # 6: waiting for user to pick an existing TV series
    WAIT_TV_EXISTING_SEASON,       # 7: waiting for season selection for an existing series
    WAIT_TV_EXISTING_EPISODE       # 8: waiting for episode number for an existing series
) = range(9)

# =============================================================================
# Directories & Database File
# =============================================================================
MOVIES_DIR = "/app/movies"
TV_DIR = "/app/tv"
DOWNLOADS_DIR = "/app/downloads"
DB_FILE = "/app/db/tv_database.db"
APPROVED_USERS_FILE = "/app/config/approved_users.txt"

# =============================================================================
# Telethon API Configuration 
# =============================================================================
telethon_lock = asyncio.Lock()

# =============================================================================
# Database Functions
# =============================================================================
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
    c.execute("INSERT INTO tv_series (name, directory) VALUES (?, ?)", (name, directory))
    conn.commit()
    series_id = c.lastrowid
    conn.close()
    logger.info(f"Added TV series '{name}' with id {series_id}")
    return series_id

def add_tv_episode(series_id: int, season: int, episode: int, file_path: str = None) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO tv_episodes (series_id, season, episode, file_path) VALUES (?, ?, ?, ?)",
              (series_id, season, episode, file_path))
    conn.commit()
    ep_id = c.lastrowid
    conn.close()
    logger.info(f"Added episode: Series ID {series_id}, Season {season}, Episode {episode}")
    return ep_id

def get_tv_series_list() -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, directory, created_at FROM tv_series ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_tv_seasons(series_id: int) -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT season FROM tv_episodes WHERE series_id = ? ORDER BY season", (series_id,))
    seasons = [row[0] for row in c.fetchall()]
    conn.close()
    return seasons

# =============================================================================
# Approved Users and Decorator
# =============================================================================
approved_users = set()
def load_approved_users() -> set:
    users = set()
    if not os.path.exists(APPROVED_USERS_FILE):
        logger.warning(f"Approved users file not found: {APPROVED_USERS_FILE}. No user is approved.")
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
            logger.warning("Unauthorized access attempt from user: %s", user.id if user else "Unknown")
            if update.message:
                await update.message.reply_text("You are not authorized to use this bot.")
            elif update.callback_query:
                await update.callback_query.answer("You are not authorized.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context)
    return wrapped

# =============================================================================
# Helper: Format TV Episode File Name
# =============================================================================
def format_tv_filename(series_name: str, season: int, episode: int, orig_filename: str) -> str:
    _, ext = os.path.splitext(orig_filename)
    return f"{series_name}-S{season:02d}E{episode:02d}{ext}"

# =============================================================================
# Job Processing Function (with progress callback, notification, and conversion)
# =============================================================================
async def process_job(job: dict, context: ContextTypes.DEFAULT_TYPE):
    """
    Process a queued job:
      - Downloads the file (using Bot API; falls back to Telethon if needed) with progress tracking.
      - Moves/renames the file to its final destination.
      - If the final file is in MKV format, convert it to MP4 using sickbeard_mp4_automator.
      - Notifies the user when the job is complete.
    """
    chat_id = job['chat_id']
    message_id = job['message_id']
    file_id = job['file_id']

    # Determine file extension (default to .mp4)
    ext = None
    if job.get('original_filename'):
        ext = os.path.splitext(job['original_filename'])[1]
    if not ext and job.get('mime_type'):
        mime_to_ext = {"video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov"}
        ext = mime_to_ext.get(job['mime_type'], ".mp4")
    temp_path = os.path.join(DOWNLOADS_DIR, f"temp_{file_id}_{job['job_id']}{ext}")
    logger.info(f"[Job {job['job_id']}] Downloading file to {temp_path}")

    # Initialize progress
    PROGRESS_DICT[job['job_id']] = 0

    try:
        file_obj = await context.bot.get_file(file_id)
        # Bot API download branch (no progress callback available)
        await file_obj.download_to_drive(custom_path=temp_path)
        PROGRESS_DICT[job['job_id']] = 100
        logger.info(f"[Job {job['job_id']}] Download via Bot API succeeded.")
    except Exception as e:
        if "File is too big" in str(e):
            logger.info(f"[Job {job['job_id']}] File too big via Bot API; falling back to Telethon.")
            TELETHON_API_ID = os.environ.get("TELETHON_API_ID")
            TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH")
            BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not (TELETHON_API_ID and TELETHON_API_HASH and BOT_TOKEN):
                raise Exception("Telethon fallback not fully configured.")
            try:
                telethon_api_id = int(TELETHON_API_ID)
            except Exception:
                raise Exception("Invalid TELETHON_API_ID provided.")
            try:
                from telethon import TelegramClient
            except ImportError:
                raise Exception("Telethon library not installed.")
            
            # Use a unique session file per job to allow concurrency:
            session = f"bot_telethon_{job['job_id']}.session"
            # Remove the session file if it already exists
            if os.path.exists(session):
                os.remove(session)
            
            client = TelegramClient(session, telethon_api_id, TELETHON_API_HASH)
            try:
                await client.start(bot_token=BOT_TOKEN)
            except Exception as te:
                raise Exception(f"Telethon client failed to start: {te}")
            try:
                msg = await client.get_messages(chat_id, ids=message_id)
            except Exception as te:
                await client.disconnect()
                raise Exception(f"Telethon failed to retrieve message: {te}")
            def progress_callback(current, total):
                if total:
                    percentage = int(current / total * 100)
                else:
                    percentage = 0
                PROGRESS_DICT[job['job_id']] = percentage
                        # Attempt to determine the total file size from the message:
            total_size = None
            if hasattr(msg, 'size') and msg.size:
                total_size = msg.size
            elif hasattr(msg, 'document') and msg.document and hasattr(msg.document, 'size'):
                total_size = msg.document.size

            def progress_callback(current, total):
                if total:
                    percentage = int(current / total * 100)
                else:
                    percentage = 0
                PROGRESS_DICT[job['job_id']] = percentage

            try:
                await client.download_file(
                    msg,
                    file=temp_path,
                    part_size_kb=2048,
                    file_size=total_size,
                    progress_callback=progress_callback
                )
            except Exception as te:
                await client.disconnect()
                raise Exception(f"Telethon failed to download media: {te}")
            await client.disconnect()
            logger.info(f"[Job {job['job_id']}] Download via Telethon succeeded.")
        else:
            raise e

    # Determine destination path based on category
    if job['category'] == 'movie':
        dest_dir = os.path.join(MOVIES_DIR, job['movie_directory'])
        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, job['desired_filename'])
    elif job['category'] == 'tv':
        series_name = job['tv_series_name']
        dest_dir = os.path.join(TV_DIR, series_name)
        os.makedirs(dest_dir, exist_ok=True)
        final_fname = format_tv_filename(series_name, job['season'], job['episode'], job['desired_filename'])
        final_path = os.path.join(dest_dir, final_fname)
    else:
        raise Exception("Invalid job category.")
    
    # Check if the temporary file exists before moving it.
    if os.path.exists(temp_path):
        try:
            shutil.move(temp_path, final_path)
            logger.info(f"[Job {job['job_id']}] File moved to {final_path}")
        except Exception as e:
            raise Exception(f"Error moving file: {e}")
    else:
        # If the file doesn't exist, log an error and notify the user.
        error_msg = f"Temporary file {temp_path} not found for job {job['job_id']}."
        logger.error(error_msg)
        raise Exception(error_msg)

    # --- Convert MKV to MP4 if needed ---
    if final_path.lower().endswith(".mkv"):
        logger.info(f"[Job {job['job_id']}] MKV file detected; starting conversion using SMA MediaProcessor.")
        try:
            from sickbeard_mp4_automator.resources.readsettings import ReadSettings
            from sickbeard_mp4_automator.resources.mediaprocessor import MediaProcessor

            settings = ReadSettings(logger=logger)
            mp = MediaProcessor(settings, logger=logger)
            info = mp.isValidSource(final_path)
            if not info:
                logger.error(f"[Job {job['job_id']}] File {final_path} is not a valid source for conversion.")
            else:
                output = mp.process(final_path, True, info=info)
                if output and 'output' in output:
                    converted_file = output['output']
                    logger.info(f"[Job {job['job_id']}] Conversion successful: {converted_file}")
                    final_path = converted_file
                else:
                    logger.error(f"[Job {job['job_id']}] Conversion failed, no output received.")
        except Exception as conv_e:
            logger.error(f"[Job {job['job_id']}] Conversion error: {conv_e}")

    # Notify user that the job is complete
    await context.bot.send_message(job['chat_id'], f"Download job {job['job_id']} completed. File is available at: {final_path}")

async def worker():
    while True:
        job = await JOB_QUEUE.get()
        ACTIVE_JOBS.add(job['job_id'])
        try:
            await process_job(job, worker_context)
        except Exception as e:
            logger.error(f"Error processing job {job['job_id']}: {e}")
            await worker_context.bot.send_message(job['chat_id'], f"Job {job['job_id']} failed: {e}")
        ACTIVE_JOBS.remove(job['job_id'])
        JOB_QUEUE.task_done()

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = "Active jobs:\n"
    for job_id in ACTIVE_JOBS:
        progress = PROGRESS_DICT.get(job_id, "N/A")
        status_message += f"Job {job_id}: {progress}% downloaded\n"
    status_message += f"\nJobs in queue: {JOB_QUEUE.qsize()}"
    await update.message.reply_text(status_message)

async def status_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await status_handler(update, context)

async def queue_job(job: dict, context: ContextTypes.DEFAULT_TYPE):
    await JOB_QUEUE.put(job)
    logger.info(f"Job {job['job_id']} queued. Queue size: {JOB_QUEUE.qsize()}")

# =============================================================================
# Conversation Handlers – Capturing Metadata
# =============================================================================
@restricted
async def video_entry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.video:
        return ConversationHandler.END
    video = update.message.video
    job = {
        'job_id': int(datetime.utcnow().timestamp()),
        'chat_id': update.message.chat.id,
        'message_id': update.message.message_id,
        'file_id': video.file_id,
        'original_filename': video.file_name if hasattr(video, "file_name") else None,
        'mime_type': video.mime_type if hasattr(video, "mime_type") else None,
    }
    context.user_data["job"] = job
    logger.info(f"Video received. Job {job['job_id']} created with original filename: {job.get('original_filename')}")
    if job.get('original_filename'):
        job['desired_filename'] = job['original_filename']
        keyboard = [
            [InlineKeyboardButton("Movie", callback_data="category_movie"),
             InlineKeyboardButton("TV", callback_data="category_tv")]
        ]
        await update.message.reply_text(
            f"Video received (file: {job['original_filename']}). Please choose category: Movie or TV?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WAIT_CATEGORY
    else:
        await update.message.reply_text("Video received. Please provide a file name (include extension):")
        return WAIT_FILENAME

@restricted
async def filename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("File name cannot be empty. Please provide a valid file name:")
        return WAIT_FILENAME
    context.user_data["job"]['desired_filename'] = text
    logger.info(f"Job {context.user_data['job']['job_id']} desired filename set to: {text}")
    keyboard = [
        [InlineKeyboardButton("Movie", callback_data="category_movie"),
         InlineKeyboardButton("TV", callback_data="category_tv")]
    ]
    await update.message.reply_text("File name set. Please choose category: Movie or TV?",
                                    reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_CATEGORY

@restricted
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    job = context.user_data["job"]
    if data == "category_movie":
        job['category'] = 'movie'
        await query.edit_message_text("Please provide the movie directory name:")
        return WAIT_MOVIE_DIR
    elif data == "category_tv":
        job['category'] = 'tv'
        keyboard = [
            [InlineKeyboardButton("New Series", callback_data="tv_new"),
             InlineKeyboardButton("Existing Series", callback_data="tv_existing")]
        ]
        await query.edit_message_text("Is this TV series new or existing?", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_TV_NEW_EXISTING
    else:
        await query.edit_message_text("Invalid category selection.")
        return ConversationHandler.END

@restricted
async def movie_dir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    dir_name = update.message.text.strip()
    if not dir_name:
        await update.message.reply_text("Directory name cannot be empty. Please try again:")
        return WAIT_MOVIE_DIR
    context.user_data["job"]['movie_directory'] = dir_name
    logger.info(f"Job {context.user_data['job']['job_id']} movie directory set to: {dir_name}")
    await queue_job(context.user_data["job"], context)
    await update.message.reply_text("Movie job queued for processing.")
    return ConversationHandler.END

@restricted
async def tv_new_existing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    job = context.user_data["job"]
    if data == "tv_new":
        await query.edit_message_text("Please provide the new TV series name:")
        return WAIT_TV_NEW_NAME
    elif data == "tv_existing":
        series_list = get_tv_series_list()
        if not series_list:
            await query.edit_message_text("No existing TV series found. Please provide a new TV series name:")
            return WAIT_TV_NEW_NAME
        job['existing_series_list'] = series_list
        job['existing_series_index'] = 0
        series = series_list[0]
        text = f"TV Series: {series[1]}\nCreated At: {series[3]}"
        keyboard = [[InlineKeyboardButton("Select", callback_data=f"tv_select:{series[0]}")]]
        if len(series_list) > 1:
            keyboard.append([InlineKeyboardButton("Next", callback_data="tv_next")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_TV_EXISTING_SELECTION
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END

@restricted
async def tv_new_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    series_name = update.message.text.strip()
    if not series_name:
        await update.message.reply_text("Series name cannot be empty. Please provide a valid name:")
        return WAIT_TV_NEW_NAME
    job = context.user_data["job"]
    job['tv_series_name'] = series_name
    job['tv_series_directory'] = os.path.join(TV_DIR, series_name)
    os.makedirs(job['tv_series_directory'], exist_ok=True)
    logger.info(f"Job {job['job_id']} new TV series name: {series_name}")
    await update.message.reply_text("Please provide season and episode numbers in the format: season,episode (e.g., 1,13):")
    return WAIT_TV_NEW_SEASON_EPISODE

@restricted
async def tv_new_season_episode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        season_str, episode_str = text.split(",")
        season = int(season_str.strip())
        episode = int(episode_str.strip())
    except Exception:
        await update.message.reply_text("Invalid format. Use: season,episode (e.g., 1,13):")
        return WAIT_TV_NEW_SEASON_EPISODE
    job = context.user_data["job"]
    job['season'] = season
    job['episode'] = episode
    logger.info(f"Job {job['job_id']} TV new series season: {season}, episode: {episode}")
    series_id = add_tv_series(job['tv_series_name'], job['tv_series_directory'])
    add_tv_episode(series_id, season, episode)
    await queue_job(job, context)
    await update.message.reply_text("TV new series job queued for processing.")
    return ConversationHandler.END

@restricted
async def tv_existing_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    job = context.user_data["job"]
    if data == "tv_next":
        series_list = job.get('existing_series_list', [])
        index = job.get('existing_series_index', 0) + 1
        if index >= len(series_list):
            index = 0
        job['existing_series_index'] = index
        series = series_list[index]
        text = f"TV Series: {series[1]}\nCreated At: {series[3]}"
        keyboard = [[InlineKeyboardButton("Select", callback_data=f"tv_select:{series[0]}")]]
        if len(series_list) > 1:
            keyboard.append([InlineKeyboardButton("Next", callback_data="tv_next")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return WAIT_TV_EXISTING_SELECTION
    elif data.startswith("tv_select:"):
        try:
            series_id = int(data.split(":")[1])
        except Exception:
            await query.edit_message_text("Invalid selection.")
            return ConversationHandler.END
        job['tv_series_id'] = series_id
        series_list = job.get('existing_series_list', [])
        for s in series_list:
            if s[0] == series_id:
                job['tv_series_name'] = s[1]
                job['tv_series_directory'] = s[2]
                break
        seasons = get_tv_seasons(series_id)
        logger.info(f"Existing seasons for series {job['tv_series_name']}: {seasons}")
        if seasons:
            keyboard = []
            for s in seasons:
                keyboard.append([InlineKeyboardButton(f"Season {s}", callback_data=f"tv_existing_season:{s}")])
            keyboard.append([InlineKeyboardButton("New Season", callback_data="tv_existing_season:new")])
            await query.edit_message_text("Select an existing season or choose New Season:", reply_markup=InlineKeyboardMarkup(keyboard))
            return WAIT_TV_EXISTING_SEASON
        else:
            await query.edit_message_text("No seasons found. Provide season and episode numbers (format: season,episode):")
            return WAIT_TV_NEW_SEASON_EPISODE
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END

@restricted
async def tv_existing_season_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    job = context.user_data["job"]
    if data == "tv_existing_season:new":
        await query.edit_message_text("Provide season and episode numbers for the new season (format: season,episode):")
        return WAIT_TV_NEW_SEASON_EPISODE
    elif data.startswith("tv_existing_season:"):
        try:
            season = int(data.split(":")[1])
        except Exception:
            await query.edit_message_text("Invalid season selection.")
            return ConversationHandler.END
        job['season'] = season
        await query.edit_message_text(f"Season {season} selected. Provide the episode number:")
        return WAIT_TV_EXISTING_EPISODE
    else:
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END

@restricted
async def tv_existing_episode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        episode = int(text)
    except Exception:
        await update.message.reply_text("Invalid episode number. Provide a numeric value:")
        return WAIT_TV_EXISTING_EPISODE
    job = context.user_data["job"]
    job['episode'] = episode
    series_id = job.get('tv_series_id')
    if series_id:
        add_tv_episode(series_id, job['season'], episode)
    else:
        logger.error("No series_id in job for existing TV series.")
    await queue_job(job, context)
    await update.message.reply_text("TV existing series job queued for processing.")
    return ConversationHandler.END

@restricted
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Operation cancelled.")
    elif update.callback_query:
        await update.callback_query.edit_message_text("Operation cancelled.")
    return ConversationHandler.END

async def start_worker_tasks(context: ContextTypes.DEFAULT_TYPE):
    global worker_context
    worker_context = context  # Save context for use by worker tasks.
    for i in range(3):
        asyncio.create_task(worker())
    logger.info("Started 3 worker tasks for concurrent downloads.")
# =============================================================================
# Main Function
# =============================================================================
def main() -> None:
    logger.info("Bot starting up...")
    os.makedirs(MOVIES_DIR, exist_ok=True)
    os.makedirs(TV_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    init_db()
    global approved_users
    approved_users = load_approved_users()
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        logger.error("No Telegram bot token provided. Set TELEGRAM_BOT_TOKEN environment variable.")
        sys.exit(1)
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.VIDEO, video_entry_handler)],
        states={
            WAIT_FILENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, filename_handler)],
            WAIT_CATEGORY: [CallbackQueryHandler(category_handler, pattern="^category_")],
            WAIT_MOVIE_DIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, movie_dir_handler)],
            WAIT_TV_NEW_EXISTING: [CallbackQueryHandler(tv_new_existing_handler, pattern="^tv_(new|existing)$")],
            WAIT_TV_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tv_new_name_handler)],
            WAIT_TV_NEW_SEASON_EPISODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tv_new_season_episode_handler)],
            WAIT_TV_EXISTING_SELECTION: [CallbackQueryHandler(tv_existing_selection_handler, pattern="^(tv_next|tv_select:.*)")],
            WAIT_TV_EXISTING_SEASON: [CallbackQueryHandler(tv_existing_season_handler, pattern="^tv_existing_season:")],
            WAIT_TV_EXISTING_EPISODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tv_existing_episode_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(CommandHandler("status", status_command_handler))
    logger.info("Bot started polling.")

    # Manually schedule the worker tasks before starting polling.
    loop = asyncio.get_event_loop()
    loop.create_task(start_worker_tasks(application))
    
    # Start polling without on_startup:
    application.run_polling()

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()