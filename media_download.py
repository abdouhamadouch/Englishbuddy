# media_download.py
# FixMyEnglish - Media Downloader

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# SETTINGS
# ============================================================

WAIT_SECONDS = 60

# Maximum duration: 40 minutes
MAX_DURATION = 40 * 60

# Maximum file size: 50 MB
MAX_FILE_SIZE = 50 * 1024 * 1024

DOWNLOAD_ROOT = Path(
    os.getenv(
        "MEDIA_DOWNLOAD_DIR",
        os.path.join(
            tempfile.gettempdir(),
            "fixmyenglish_downloads",
        ),
    )
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# PENDING REQUESTS
# ============================================================

# One waiting request per user per chat.
#
# key:
#     (chat_id, user_id)
#
# value:
#     {
#         "task": asyncio.Task
#     }

PENDING_DOWNLOADS = {}


# ============================================================
# URL
# ============================================================

URL_RE = re.compile(
    r"https?://[^\s<>\"]+",
    re.IGNORECASE,
)


def extract_url(text: str | None) -> str | None:
    if not text:
        return None

    match = URL_RE.search(text)

    if not match:
        return None

    return match.group(0).rstrip(
        ".,!?;:)]}"
    )


def get_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


# ============================================================
# REQUEST KEY
# ============================================================

def request_key(update: Update):
    if (
        not update.effective_chat
        or not update.effective_user
    ):
        return None

    return (
        update.effective_chat.id,
        update.effective_user.id,
    )


# ============================================================
# CANCEL WAITING REQUEST
# ============================================================

def cancel_pending_request(update: Update) -> bool:
    key = request_key(update)

    if key is None:
        return False

    data = PENDING_DOWNLOADS.pop(
        key,
        None,
    )

    if not data:
        return False

    task = data.get("task")

    if task and not task.done():
        task.cancel()

    return True


# ============================================================
# TEMP DIRECTORY
# ============================================================

def create_download_dir() -> Path:
    return Path(
        tempfile.mkdtemp(
            prefix="fixmyenglish_",
            dir=str(DOWNLOAD_ROOT),
        )
    )


def cleanup_directory(directory: Path):
    try:
        if directory.exists():
            shutil.rmtree(
                directory,
                ignore_errors=True,
            )
    except Exception:
        pass


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    directory: Path,
    mode: str,
):
    output_template = str(
        directory
        / "%(title).80s [%(id)s].%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "noplaylist": True,

        "quiet": True,
        "no_warnings": True,

        # Extra protection against huge files.
        "max_filesize": MAX_FILE_SIZE,

        "writethumbnail": False,
        "writeinfojson": False,
        "writedescription": False,
        "writeautomaticsub": False,
        "writesubtitles": False,
    }

    if mode == "audio":

        options.update(
            {
                "format": "bestaudio/best",

                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    }
                ],
            }
        )

    else:

        options.update(
            {
                "format": (
                    "bestvideo[ext=mp4]+"
                    "bestaudio[ext=m4a]/"
                    "best[ext=mp4]/"
                    "best"
                ),
                "merge_output_format": "mp4",
            }
        )

    return options


# ============================================================
# CHECK MEDIA
# ============================================================

def check_media_info(info: dict):

    # --------------------------------------------------------
    # Playlist
    # --------------------------------------------------------

    if info.get("_type") == "playlist":
        raise ValueError(
            "Playlists are not supported.\n"
            "Please send one video link."
        )

    # --------------------------------------------------------
    # Duration
    # --------------------------------------------------------

    duration = info.get("duration")

    if duration is not None:

        try:
            duration = float(duration)
        except Exception:
            duration = None

    if (
        duration is not None
        and duration > MAX_DURATION
    ):
        raise ValueError(
            "This media is longer than 40 minutes."
        )


# ============================================================
# FIND DOWNLOADED FILE
# ============================================================

def find_downloaded_file(
    directory: Path,
) -> Path | None:

    if not directory.exists():
        return None

    files = [
        p
        for p in directory.rglob("*")
        if p.is_file()
    ]

    if not files:
        return None

    preferred_extensions = {
        ".mp4",
        ".m4a",
        ".mp3",
        ".webm",
        ".mov",
        ".mkv",
        ".aac",
        ".opus",
    }

    preferred = [
        p
        for p in files
        if p.suffix.lower()
        in preferred_extensions
    ]

    if preferred:
        return max(
            preferred,
            key=lambda p: p.stat().st_mtime,
        )

    return max(
        files,
        key=lambda p: p.stat().st_mtime,
    )


# ============================================================
# DOWNLOAD
# ============================================================

def download_media_sync(
    url: str,
    mode: str,
):

    directory = create_download_dir()

    try:

        options = build_ydl_options(
            directory,
            mode,
        )

        with yt_dlp.YoutubeDL(options) as ydl:

            # First inspect the media.
            info = ydl.extract_info(
                url,
                download=False,
            )

            if not info:
                raise ValueError(
                    "No media was found at this link."
                )

            # Check duration before downloading.
            check_media_info(info)

            title = (
                info.get("title")
                or "Downloaded media"
            )

            # Download.
            ydl.download([url])

        file_path = find_downloaded_file(
            directory
        )

        if not file_path:
            raise ValueError(
                "The download finished, "
                "but no media file was found."
            )

        # ----------------------------------------------------
        # FINAL SIZE CHECK
        # ----------------------------------------------------

        file_size = file_path.stat().st_size

        if file_size > MAX_FILE_SIZE:

            cleanup_directory(
                directory
            )

            raise ValueError(
                "The downloaded file is larger "
                "than 50 MB."
            )

        return {
            "directory": directory,
            "file": file_path,
            "title": title,
            "mode": mode,
        }

    except Exception:

        cleanup_directory(
            directory
        )

        raise


# ============================================================
# BUTTONS
# ============================================================

def download_buttons():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Video",
                    callback_data="md_video",
                ),
                InlineKeyboardButton(
                    "Audio",
                    callback_data="md_audio",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Cancel",
                    callback_data="md_cancel",
                ),
            ],
        ]
    )


# ============================================================
# WAITING MESSAGE
# ============================================================

def waiting_text():

    return (
        "Send the media link within 60 seconds.\n\n"
        "You can also reply to a message containing "
        "a link and write: تحميل\n\n"
        "Type إلغاء to cancel."
    )


# ============================================================
# SEND FILE
# ============================================================

async def send_downloaded_file(
    update: Update,
    result: dict,
):

    message = update.effective_message

    if not message:
        return

    file_path = result["file"]
    title = result["title"]
    mode = result["mode"]

    file_handle = None

    try:

        file_handle = file_path.open(
            "rb"
        )

        if mode == "audio":

            await message.reply_audio(
                audio=file_handle,
                title=title[:64],
                caption=(
                    "Downloaded audio\n\n"
                    f"{title[:200]}"
                ),
            )

        else:

            await message.reply_video(
                video=file_handle,
                supports_streaming=True,
                caption=(
                    "Downloaded video\n\n"
                    f"{title[:200]}"
                ),
            )

    finally:

        if file_handle:
            file_handle.close()

        cleanup_directory(
            result["directory"]
        )


# ============================================================
# PERFORM DOWNLOAD
# ============================================================

async def perform_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    mode: str,
):

    message = update.effective_message

    if not message:
        return

    domain = get_domain(url)

    status_message = await message.reply_text(
        "Downloading...\n\n"
        f"Source: {domain or 'unknown'}"
    )

    try:

        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        result = await asyncio.to_thread(
            download_media_sync,
            url,
            mode,
        )

        try:
            await status_message.delete()
        except Exception:
            pass

        await send_downloaded_file(
            update,
            result,
        )

    except ValueError as exc:

        try:
            await status_message.edit_text(
                str(exc)
            )
        except Exception:
            await message.reply_text(
                str(exc)
            )

    except Exception as exc:

        print(
            "MEDIA DOWNLOAD ERROR:",
            repr(exc),
        )

        try:

            await status_message.edit_text(
                "Download failed.\n\n"
                "The link may be private, "
                "unsupported, unavailable, "
                "or require access that the "
                "downloader cannot use."
            )

        except Exception:

            await message.reply_text(
                "Download failed."
            )


# ============================================================
# START WAITING
# ============================================================

async def start_download_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message:
        return

    key = request_key(update)

    if key is None:
        return

    # Cancel previous request.
    cancel_pending_request(
        update
    )

    await message.reply_text(
        waiting_text()
    )

    async def expiration_task():

        try:

            await asyncio.sleep(
                WAIT_SECONDS
            )

            current = PENDING_DOWNLOADS.get(
                key
            )

            if current:

                PENDING_DOWNLOADS.pop(
                    key,
                    None,
                )

                try:

                    await message.reply_text(
                        "Download request expired.\n"
                        "Please use تحميل again."
                    )

                except Exception:
                    pass

        except asyncio.CancelledError:
            return

    task = asyncio.create_task(
        expiration_task()
    )

    PENDING_DOWNLOADS[key] = {
        "task": task,
    }


# ============================================================
# TEXT HANDLER
# ============================================================

async def media_download_text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    if not text:
        return

    lower = text.lower()

    # ========================================================
    # CANCEL
    # ========================================================

    if lower in {
        "إلغاء",
        "الغاء",
        "cancel",
    }:

        if cancel_pending_request(update):

            context.user_data.pop(
                "media_download_url",
                None,
            )

            await message.reply_text(
                "Download request cancelled."
            )

        return

    # ========================================================
    # DETECT COMMAND
    # ========================================================

    is_download_command = False
    remainder = ""

    if lower == "تحميل":

        is_download_command = True

    elif lower.startswith("تحميل "):

        is_download_command = True
        remainder = text[len("تحميل"):].strip()

    elif lower == "download":

        is_download_command = True

    elif lower.startswith("download "):

        is_download_command = True
        remainder = text[len("download"):].strip()

    elif lower == "dl":

        is_download_command = True

    elif lower.startswith("dl "):

        is_download_command = True
        remainder = text[2:].strip()

    # Slash commands.
    elif lower == "/download":

        is_download_command = True

    elif lower.startswith("/download "):

        is_download_command = True
        remainder = text[
            len("/download"):
        ].strip()

    elif lower == "/dl":

        is_download_command = True

    elif lower.startswith("/dl "):

        is_download_command = True
        remainder = text[3:].strip()

    # ========================================================
    # NORMAL MESSAGE
    # ========================================================

    if not is_download_command:

        key = request_key(update)

        if key is None:
            return

        pending = PENDING_DOWNLOADS.get(
            key
        )

        # Only accept a URL when the user
        # actually has an active waiting request.
        if not pending:
            return

        url = extract_url(text)

        if not url:
            return

        # Consume request.
        data = PENDING_DOWNLOADS.pop(
            key,
            None,
        )

        if data:

            task = data.get("task")

            if task and not task.done():
                task.cancel()

        context.user_data[
            "media_download_url"
        ] = url

        await message.reply_text(
            "Choose what you want to download:",
            reply_markup=download_buttons(),
        )

        return

    # ========================================================
    # DOWNLOAD + URL
    # ========================================================

    url = extract_url(
        remainder
    )

    if url:

        context.user_data[
            "media_download_url"
        ] = url

        await message.reply_text(
            "Choose what you want to download:",
            reply_markup=download_buttons(),
        )

        return

    # ========================================================
    # REPLY TO MESSAGE CONTAINING URL
    # ========================================================

    if message.reply_to_message:

        replied_text = (
            message.reply_to_message.text
            or message.reply_to_message.caption
            or ""
        )

        url = extract_url(
            replied_text
        )

        if url:

            context.user_data[
                "media_download_url"
            ] = url

            await message.reply_text(
                "Choose what you want to download:",
                reply_markup=download_buttons(),
            )

            return

    # ========================================================
    # NO URL
    # ========================================================

    await start_download_request(
        update,
        context,
    )


# ============================================================
# BUTTON CALLBACK
# ============================================================

async def media_download_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    action = query.data

    # ========================================================
    # CANCEL
    # ========================================================

    if action == "md_cancel":

        context.user_data.pop(
            "media_download_url",
            None,
        )

        try:

            await query.edit_message_text(
                "Download cancelled."
            )

        except Exception:
            pass

        return

    # ========================================================
    # URL
    # ========================================================

    url = context.user_data.get(
        "media_download_url"
    )

    if not url:

        try:

            await query.edit_message_text(
                "No download request was found."
            )

        except Exception:
            pass

        return

    # ========================================================
    # MODE
    # ========================================================

    if action == "md_video":

        mode = "video"

    elif action == "md_audio":

        mode = "audio"

    else:
        return

    # Prevent the same buttons
    # from being reused accidentally.
    context.user_data.pop(
        "media_download_url",
        None,
    )

    try:

        await query.edit_message_text(
            "Starting download..."
        )

    except Exception:
        pass

    await perform_download(
        update,
        context,
        url,
        mode,
    )


# ============================================================
# REGISTER
# ============================================================

def register_media_download(
    application,
):

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            media_download_text_handler,
        ),
        group=30,
    )

    application.add_handler(
        CallbackQueryHandler(
            media_download_callback,
            pattern=r"^md_(video|audio|cancel)$",
        ),
        group=30,
  )
