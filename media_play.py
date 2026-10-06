"""
FixMyEnglish Media Player
Audio + Video Voice Chat player.

Uses:
• python-telegram-bot 22.8
• Pyrogram user session
• PyTgCalls 3.x
• FFmpeg
• edge-tts
• yt-dlp

Required Railway variables:
API_ID
API_HASH
SESSION_STRING
OWNER_ID (optional)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import shutil
import subprocess
import tempfile
import re

import yt_dlp

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import edge_tts
from pyrogram import Client

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from pytgcalls import PyTgCalls
    from pytgcalls import filters as call_filters
    from pytgcalls.types import (
        StreamEnded,
        MediaStream,
        AudioQuality,
        VideoQuality,
    )
except Exception:
    PyTgCalls = None
    call_filters = None
    StreamEnded = None
    MediaStream = None
    AudioQuality = None
    VideoQuality = None

log = logging.getLogger("FixMyEnglish.media_play")

# =========================================================
# ENVIRONMENT
# =========================================================

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

API_ID = int(API_ID_RAW) if API_ID_RAW else 0

OWNER_ID: Optional[int] = (
    int(OWNER_ID_RAW)
    if OWNER_ID_RAW.isdigit()
    else None
)

# =========================================================
# CONSTANTS
# =========================================================

MAX_QUEUE = 50
MAX_REPEAT_COUNT = 1000

MESSAGE_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 180
PLAY_TIMEOUT = 60

VIDEO_NORMALIZE_TIMEOUT = 300
AUDIO_EXTRACT_TIMEOUT = 180

TEMP_ROOT = (
    Path(tempfile.gettempdir())
    / "fixmyenglish_media"
)

TEMP_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)

ADMIN_STATUSES = {
    "administrator",
    "creator",
    "owner",
}

URL_RE = re.compile(
    r"https?://[^\s<>()]+",
    re.IGNORECASE,
)

# =========================================================
# GLOBAL ENGINE STATE
# =========================================================

USER_CLIENT: Optional[Client] = None
CALLS = None
BOT_INSTANCE = None

ENGINE_LOCK = asyncio.Lock()
ENGINE_READY = False
EVENTS_REGISTERED = False

PLAYERS: dict[int, "PlayerState"] = {}
CHAT_LOCKS: dict[int, asyncio.Lock] = {}

# =========================================================
# DATA CLASSES
# =========================================================

@dataclass
class MediaItem:
    chat_id: int
    message_id: int
    title: str
    kind: str
    requester_id: int

    temp_path: Optional[str] = None  
    source_path: Optional[str] = None  

    thread_id: Optional[int] = None  

    audio_only: bool = False  

    tts_text: Optional[str] = None  
    url: Optional[str] = None  

    # Number of total plays still represented by this item.  
    # 1 = play once  
    # 3 = play three times total  
    # Every "🔁 كرر" press adds +1.  
    repeat_count: int = 1

@dataclass
class PlayerState:
    queue: deque

    current: Optional[MediaItem] = None  

    paused: bool = False  
    starting: bool = False  

    # Ignore stream_end events caused by intentional  
    # leave/skip/seek operations.  
    suppress_end_until: float = 0.0  

    # Messages sent by this media player.  
    status_message_ids: set[int] = None  

    # Prevent duplicate stream_end events.  
    ending: bool = False  

    # Last handled stream end.  
    last_finished_key: Optional[str] = None  
    last_finished_at: float = 0.0  

    # Ignore delayed events immediately after starting  
    # a replacement stream.  
    ignore_end_until: float = 0.0  

    def __post_init__(self):  
        if self.status_message_ids is None:  
            self.status_message_ids = set()

# =========================================================
# STATE HELPERS
# =========================================================

def _state(chat_id: int) -> PlayerState:
    if chat_id not in PLAYERS:
        PLAYERS[chat_id] = PlayerState(
            queue=deque()
        )

    return PLAYERS[chat_id]

def _lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS:
        CHAT_LOCKS[chat_id] = asyncio.Lock()

    return CHAT_LOCKS[chat_id]

# =========================================================
# MESSAGE / MEDIA HELPERS
# =========================================================

def _media_from_message(message):
    if not message:
        return None

    if getattr(message, "audio", None):  
        return (  
            "audio",  
            message.audio.file_name or "Audio",  
        )  

    if getattr(message, "video", None):  
        return (  
            "video",  
            message.video.file_name or "Video",  
        )  

    if getattr(message, "voice", None):  
        return "audio", "Voice message"  

    document = getattr(  
        message,  
        "document",  
        None,  
    )  

    if document:  
        mime = (  
            document.mime_type or ""  
        ).lower()  

        if mime.startswith("audio/"):  
            return (  
                "audio",  
                document.file_name or "Audio",  
            )  

        if mime.startswith("video/"):  
            return (  
                "video",  
                document.file_name or "Video",  
            )  

    return None

def _message_text(message) -> str:
    if not message:
        return ""

    text = (  
        getattr(message, "text", None)  
        or getattr(message, "caption", None)  
        or ""  
    )  

    return text.strip()

def _extract_url(message) -> Optional[str]:
    text = _message_text(message)

    if not text:  
        return None  

    match = URL_RE.search(text)  

    if not match:  
        return None  

    url = match.group(0).strip()  

    url = url.rstrip(  
        ".,!?;:)]}"  
    )  

    return url

def _source_message(update: Update):
    message = update.effective_message

    if not message:  
        return None  

    return message.reply_to_message

def _normalize_digits(value: str) -> str:
    translation = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
        "01234567890123456789",
    )

    return value.translate(translation)

def _extract_play_count(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """
    Supports:
    /play  
    /play 3  
    شغل  
    شغل 3  
    شغل ٣  
    /video 3  
    /audio 3  
    """  

    args = list(  
        context.args or []  
    )  

    if args:  
        for arg in args:  
            normalized = _normalize_digits(  
                str(arg).strip()  
            )  

            if normalized.isdigit():  
                try:  
                    count = int(normalized)  

                    return max(  
                        1,  
                        min(  
                            count,  
                            MAX_REPEAT_COUNT,  
                        ),  
                    )  
                except Exception:  
                    pass  

    message = update.effective_message  

    if not message:  
        return 1  

    text = (  
        getattr(message, "text", None)  
        or ""  
    ).strip()  

    match = re.search(  
        r"(?:شغل|play|video|audio)"  
        r"\s+([0-9٠-٩۰-۹]+)"  
        r"\s*$",  
        text,  
        re.IGNORECASE,  
    )  

    if match:  
        normalized = _normalize_digits(  
            match.group(1)  
        )  

        try:  
            count = int(normalized)  

            return max(  
                1,  
                min(  
                    count,  
                    MAX_REPEAT_COUNT,  
                ),  
            )  

        except Exception:  
            pass  

    return 1

# =========================================================
# ADMIN
# =========================================================

async def _is_admin(
    update: Update,
    context,
) -> bool:
    user = update.effective_user
    message = update.effective_message

    if not user or not message:  
        return False  

    if OWNER_ID and user.id == OWNER_ID:  
        return True  

    if message.chat.type == "private":  
        return True  

    try:  
        member = await context.bot.get_chat_member(  
            message.chat.id,  
            user.id,  
        )  

        return member.status in ADMIN_STATUSES  

    except Exception:  
        log.exception(  
            "Admin check failed."  
        )  

        return False

async def _require_admin(
    update,
    context,
) -> bool:
    if await _is_admin(
        update,
        context,
    ):
        return True

    message = update.effective_message  

    if message:  
        await message.reply_text(  
            "❌ هذا الأمر متاح للمشرفين فقط."  
        )  

    return False

# =========================================================
# PLAYER BUTTONS
# =========================================================

def _buttons(chat_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏪ -10",
                callback_data=f"mp:{chat_id}:back10",
            ),
            InlineKeyboardButton(
                "⏩ +10",
                callback_data=f"mp:{chat_id}:forward10",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏸ Pause",
                callback_data=f"mp:{chat_id}:pause",
            ),
            InlineKeyboardButton(
                "▶️ Resume",
                callback_data=f"mp:{chat_id}:resume",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏭ Skip",
                callback_data=f"mp:{chat_id}:skip",
            ),
            InlineKeyboardButton(
                "⏹ Stop",
                callback_data=f"mp:{chat_id}:stop",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Queue",
                callback_data=f"mp:{chat_id}:queue",
            ),
            InlineKeyboardButton(
                "🔁 كرر",
                callback_data=f"mp:{chat_id}:repeat_add",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔴 Hide",
                callback_data=f"mp:{chat_id}:hide",
            ),
        ],
    ])

# =========================================================
# QUEUE DISPLAY
# =========================================================

def _repeat_text(
    item: MediaItem,
) -> str:
    if item.repeat_count > 1:
        return f" ×{item.repeat_count}"

    return ""

def _format_queue(
    s: PlayerState,
) -> str:
    lines = []

    if s.current:  
        status = (  
            "⏸ Paused"  
            if s.paused  
            else "▶️ Playing"  
        )  

        icon = (  
            "🎬"  
            if (  
                s.current.kind == "video"  
                and not s.current.audio_only  
            )  
            else "🎵"  
        )  

        lines.append(  
            f"{icon} {status}: "  
            f"{s.current.title}"  
            f"{_repeat_text(s.current)}"  
        )  

    if s.queue:  
        lines.append("")  
        lines.append("📋 Queue:")  

        for i, item in enumerate(  
            s.queue,  
            1,  
        ):  
            icon = (  
                "🎬"  
                if (  
                    item.kind == "video"  
                    and not item.audio_only  
                )  
                else "🎵"  
            )  

            lines.append(  
                f"{i}. {icon} "  
                f"{item.title}"  
                f"{_repeat_text(item)}"  
            )  

    return (  
        "\n".join(lines)  
        if lines  
        else "📋 Queue is empty."  
    )

# =========================================================
# STATUS MESSAGES
# =========================================================

async def _delete_status_messages(
    chat_id: int,
):
    """
    Delete only messages generated by the media player.
    User's original messages are never deleted.
    """

    if BOT_INSTANCE is None:  
        return  

    s = _state(chat_id)  

    message_ids = list(  
        s.status_message_ids  
    )  

    if not message_ids:  
        return  

    s.status_message_ids.clear()  

    for message_id in message_ids:  
        try:  
            await BOT_INSTANCE.delete_message(  
                chat_id=chat_id,  
                message_id=message_id,  
            )  

        except Exception as exc:  
            text = repr(exc)  

            if (  
                "MessageToDeleteNotFound"  
                in text  
                or "message to delete not found"  
                in text.lower()  
            ):  
                continue  

            if "Topic_closed" in text:  
                continue  

            log.info(  
                "MEDIA: could not delete status "  
                "message chat=%s message=%s error=%r",  
                chat_id,  
                message_id,  
                exc,  
            )

async def _send_status_message(
    chat_id: int,
    text: str,
    reply_markup=None,
    thread_id: Optional[int] = None,
):
    if BOT_INSTANCE is None:
        return None

    kwargs = {  
        "chat_id": chat_id,  
        "text": text,  
        "parse_mode": "HTML",  
    }  

    if reply_markup is not None:  
        kwargs["reply_markup"] = reply_markup  

    if thread_id:  
        kwargs["message_thread_id"] = thread_id  

    try:  
        sent = await BOT_INSTANCE.send_message(  
            **kwargs  
        )  

        _state(  
            chat_id  
        ).status_message_ids.add(  
            sent.message_id  
        )  

        return sent  

    except Exception as exc:  
        if "Topic_closed" in repr(exc):  
            log.info(  
                "MEDIA: topic closed; status message skipped."  
            )  
        else:  
            log.exception(  
                "MEDIA: status message failed."  
            )  

        return None

# =========================================================
# PYTGCALLS ENGINE
# =========================================================

async def _ensure_engine():
    global USER_CLIENT
    global CALLS
    global ENGINE_READY

    if (  
        ENGINE_READY  
        and USER_CLIENT is not None  
        and CALLS is not None  
    ):  
        return  

    async with ENGINE_LOCK:  

        if (  
            ENGINE_READY  
            and USER_CLIENT is not None  
            and CALLS is not None  
        ):  
            return  

        if (  
            not API_ID  
            or not API_HASH  
            or not SESSION_STRING  
        ):  
            raise RuntimeError(  
                "Missing API_ID, API_HASH, or SESSION_STRING."  
            )  

        if PyTgCalls is None:  
            raise RuntimeError(  
                "PyTgCalls import failed."  
            )  

        if shutil.which("ffmpeg") is None:  
            raise RuntimeError(  
                "FFmpeg is not installed."  
            )  

        log.info(  
            "MEDIA: ffmpeg=%s",  
            shutil.which("ffmpeg"),  
        )  

        log.info(  
            "MEDIA: ffprobe=%s",  
            shutil.which("ffprobe"),  
        )  

        if USER_CLIENT is None:  
            USER_CLIENT = Client(  
                "fixmyenglish_player",  
                api_id=API_ID,  
                api_hash=API_HASH,  
                session_string=SESSION_STRING,  
                in_memory=True,  
            )  

        if not getattr(  
            USER_CLIENT,  
            "is_connected",  
            False,  
        ):  
            log.info(  
                "MEDIA: starting assistant..."  
            )  

            await USER_CLIENT.start()  

            log.info(  
                "MEDIA: assistant started as @%s",  
                getattr(  
                    USER_CLIENT.me,  
                    "username",  
                    "unknown",  
                ),  
            )  

        if CALLS is None:  
            CALLS = PyTgCalls(  
                USER_CLIENT  
            )  

            log.info(  
                "MEDIA: starting PyTgCalls..."  
            )  

            result = CALLS.start()  

            if inspect.isawaitable(result):  
                await result  

        _register_call_events()  

        ENGINE_READY = True  

        log.info(  
            "MEDIA: engine ready."  
        )

def _register_call_events():
    global EVENTS_REGISTERED

    if EVENTS_REGISTERED:  
        return  

    if (  
        CALLS is None  
        or call_filters is None  
    ):  
        raise RuntimeError(  
            "PyTgCalls event API unavailable."  
        )  

    @CALLS.on_update(  
        call_filters.stream_end()  
    )  
    async def stream_end(_, update):  
        chat_id = getattr(  
            update,  
            "chat_id",  
            None,  
        )  

        if chat_id is not None:  
            log.info(  
                "MEDIA EVENT: stream ended chat=%s",  
                chat_id,  
            )  

            asyncio.create_task(  
                _handle_stream_end(  
                    int(chat_id)  
                )  
            )  

    EVENTS_REGISTERED = True

# =========================================================
# STREAM BUILDING
# =========================================================

def _build_stream(
    item: MediaItem,
    path: str,
):
    if not Path(path).exists():
        raise FileNotFoundError(path)

    if MediaStream is None:  
        raise RuntimeError(  
            "MediaStream is unavailable."  
        )  

    if (  
        item.kind == "video"  
        and not item.audio_only  
    ):  
        log.info(  
            "MEDIA STREAM: VIDEO path=%s quality=720p",  
            path,  
        )  

        return MediaStream(  
            path,  
            audio_parameters=AudioQuality.HIGH,  
            video_parameters=VideoQuality.HD_720p,  
        )  

    log.info(  
        "MEDIA STREAM: AUDIO path=%s",  
        path,  
    )  

    return MediaStream(  
        path,  
        audio_parameters=AudioQuality.HIGH,  
    )

# =========================================================
# LEAVE VOICE CHAT
# =========================================================

async def _safe_leave(
    chat_id: int,
):
    if CALLS is None:
        return

    await asyncio.sleep(0.5)  

    for attempt in range(2):  
        try:  
            result = CALLS.leave_call(  
                chat_id  
            )  

            if inspect.isawaitable(result):  
                await asyncio.wait_for(  
                    result,  
                    20,  
                )  

            log.info(  
                "MEDIA: assistant left chat=%s",  
                chat_id,  
            )  

            return  

        except Exception as exc:  
            text = repr(exc)  

            if (  
                "NotInCall" in text  
                or "not in call"  
                in text.lower()  
            ):  
                return  

            if attempt == 0:  
                await asyncio.sleep(1)  

            else:  
                log.warning(  
                    "MEDIA: leave failed chat=%s error=%r",  
                    chat_id,  
                    exc,  
                )

# =========================================================
# TTS
# =========================================================

async def _make_tts(
    text: str,
) -> str:
    safe_name = (
        f"tts"
        f"{abs(hash(text))}"
        f"_{random.randint(1000, 9999)}"
    )

    path = (  
        TEMP_ROOT  
        / f"{safe_name}.mp3"  
    )  

    communicate = edge_tts.Communicate(  
        text=text,  
        voice="en-US-AriaNeural",  
        rate="+0%",  
    )  

    await asyncio.wait_for(  
        communicate.save(str(path)),  
        60,  
    )  

    if not path.exists():  
        raise RuntimeError(  
            "TTS file was not created."  
        )  

    log.info(  
        "MEDIA TTS: created %s",  
        path,  
    )  

    return str(path)

# =========================================================
# URL DOWNLOAD
# =========================================================

async def _download_url_item(
    item: MediaItem,
) -> str:
    if not item.url:
        raise RuntimeError(
            "URL is missing."
        )

    safe_id = (  
        f"url_"  
        f"{item.chat_id}_"  
        f"{item.message_id}_"  
        f"{random.randint(100000, 999999)}"  
    )  

    output_template = str(  
        TEMP_ROOT  
        / f"{safe_id}.%(ext)s"  
    )  

    if item.audio_only:  
        ydl_opts = {  
            "outtmpl": output_template,  
            "format": "bestaudio/best",  
            "noplaylist": True,  
            "quiet": True,  
            "no_warnings": True,  
            "restrictfilenames": True,  
            "postprocessors": [  
                {  
                    "key": "FFmpegExtractAudio",  
                    "preferredcodec": "mp3",  
                    "preferredquality": "192",  
                }  
            ],  
        }  
    else:  
        ydl_opts = {  
            "outtmpl": output_template,  
            "format": (  
                "bestvideo[height<=720]+"  
                "bestaudio/"  
                "best[height<=720]/"  
                "best"  
            ),  
            "merge_output_format": "mp4",  
            "noplaylist": True,  
            "quiet": True,  
            "no_warnings": True,  
            "restrictfilenames": True,  
        }  

    def download():  
        with yt_dlp.YoutubeDL(  
            ydl_opts  
        ) as ydl:  
            info = ydl.extract_info(  
                item.url,  
                download=True,  
            )  

            if not info:  
                raise RuntimeError(  
                    "yt-dlp returned no information."  
                )  

            if info.get("entries"):  
                entries = info["entries"]  

                if entries:  
                    info = entries[0]  

            title = (  
                info.get("title")  
                or item.title  
                or "Media"  
            )  

            item.title = title  

    log.info(  
        "MEDIA URL DOWNLOAD: %s audio_only=%s",  
        item.url,  
        item.audio_only,  
    )  

    await asyncio.wait_for(  
        asyncio.to_thread(download),  
        DOWNLOAD_TIMEOUT,  
    )  

    candidates = sorted(  
        TEMP_ROOT.glob(  
            f"{safe_id}.*"  
        ),  
        key=lambda p: p.stat().st_mtime,  
        reverse=True,  
    )  

    candidates = [  
        p  
        for p in candidates  
        if p.is_file()  
        and p.suffix.lower()  
        not in {  
            ".part",  
            ".ytdl",  
            ".tmp",  
        }  
    ]  

    if not candidates:  
        raise RuntimeError(  
            "yt-dlp downloaded no playable file."  
        )  

    if item.audio_only:  
        preferred = [  
            p  
            for p in candidates  
            if p.suffix.lower()  
            in {  
                ".mp3",  
                ".m4a",  
                ".aac",  
                ".opus",  
                ".ogg",  
            }  
        ]  

        if preferred:  
            candidates = preferred  
    else:  
        preferred = [  
            p  
            for p in candidates  
            if p.suffix.lower()  
            in {  
                ".mp4",  
                ".mkv",  
                ".webm",  
                ".mov",  
            }  
        ]  

        if preferred:  
            candidates = preferred  

    path = str(  
        candidates[0]  
    )  

    log.info(  
        "MEDIA URL DOWNLOAD OK: %s",  
        path,  
    )  

    return path

# =========================================================
# TELEGRAM DOWNLOAD
# =========================================================

async def _download_item(
    item: MediaItem,
) -> str:
    if item.url:  
        return await _download_url_item(  
            item  
        )  

    if USER_CLIENT is None:  
        raise RuntimeError(  
            "Assistant is not running."  
        )  

    if item.tts_text:  
        return await _make_tts(  
            item.tts_text  
        )  

    source = await asyncio.wait_for(  
        USER_CLIENT.get_messages(  
            item.chat_id,  
            item.message_id,  
        ),  
        MESSAGE_TIMEOUT,  
    )  

    if not source:  
        raise RuntimeError(  
            "Source Telegram message was not found."  
        )  

    if _media_from_message(source) is None:  
        raise RuntimeError(  
            "Source message no longer contains playable media."  
        )  

    prefix = (  
        f"fixmyenglish_"  
        f"{item.chat_id}_"  
        f"{item.message_id}_"  
        f"{random.randint(100000, 999999)}"  
    )  

    path = await asyncio.wait_for(  
        USER_CLIENT.download_media(  
            source,  
            file_name=str(  
                TEMP_ROOT / prefix  
            ),  
        ),  
        DOWNLOAD_TIMEOUT,  
    )  

    if not path:  
        raise RuntimeError(  
            "Telegram media download failed."  
        )  

    log.info(  
        "MEDIA DOWNLOAD: %s",  
        path,  
    )  

    return str(path)

# =========================================================
# FFMPEG HELPERS
# =========================================================

async def _run_ffmpeg(
    command: list[str],
    timeout: int,
):
    process = (
        await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )

    stdout, stderr = (  
        await asyncio.wait_for(  
            process.communicate(),  
            timeout,  
        )  
    )  

    if process.returncode != 0:  
        error = stderr.decode(  
            errors="ignore"  
        )  

        raise RuntimeError(  
            error[-4000:]  
            or "FFmpeg failed."  
        )  

    return stdout, stderr

async def _normalize_video(
    source_path: str,
) -> str:
    source = Path(  
        source_path  
    )  

    if not source.exists():  
        raise FileNotFoundError(  
            source_path  
        )  

    output = (  
        TEMP_ROOT  
        / (  
            f"normalized_"  
            f"{source.stem}_"  
            f"{random.randint(100000, 999999)}"  
            f".mp4"  
        )  
    )  

    command = [  
        "ffmpeg",  
        "-y",  
        "-i",  
        str(source),  
        "-map",  
        "0:v:0",  
        "-map",  
        "0:a:0?",  
        "-c:v",  
        "libx264",  
        "-preset",  
        "veryfast",  
        "-crf",  
        "23",  
        "-pix_fmt",  
        "yuv420p",  
        "-c:a",  
        "aac",  
        "-b:a",  
        "128k",  
        "-movflags",  
        "+faststart",  
        str(output),  
    ]  

    log.info(  
        "MEDIA VIDEO NORMALIZE: %s -> %s",  
        source,  
        output,  
    )  

    await _run_ffmpeg(  
        command,  
        VIDEO_NORMALIZE_TIMEOUT,  
    )  

    if not output.exists():  
        raise RuntimeError(  
            "Video normalization produced no file."  
        )  

    log.info(  
        "MEDIA VIDEO NORMALIZE OK: %s",  
        output,  
    )  

    return str(output)

async def _extract_audio(
    source_path: str,
) -> str:
    source = Path(  
        source_path  
    )  

    if not source.exists():  
        raise FileNotFoundError(  
            source_path  
        )  

    output = (  
        TEMP_ROOT  
        / (  
            f"audio_"  
            f"{source.stem}_"  
            f"{random.randint(100000, 999999)}"  
            f".mp3"  
        )  
    )  

    command = [  
        "ffmpeg",  
        "-y",  
        "-i",  
        str(source),  
        "-vn",  
        "-c:a",  
        "libmp3lame",  
        "-b:a",  
        "192k",  
        str(output),  
    ]  

    log.info(  
        "MEDIA AUDIO EXTRACT: %s -> %s",  
        source,  
        output,  
    )  

    await _run_ffmpeg(  
        command,  
        AUDIO_EXTRACT_TIMEOUT,  
    )  

    if not output.exists():  
        raise RuntimeError(  
            "Audio extraction produced no file."  
        )  

    return str(output)

async def _prepare_playable_path(
    item: MediaItem,
    source_path: str,
) -> str:
    if (  
        item.kind == "video"  
        and not item.audio_only  
    ):  
        if (  
            item.temp_path  
            and item.temp_path != item.source_path  
            and Path(  
                item.temp_path  
            ).exists()  
        ):  
            return item.temp_path  

        normalized = (  
            await _normalize_video(  
                source_path  
            )  
        )  

        item.temp_path = normalized  

        return normalized  

    if (  
        item.kind == "video"  
        and item.audio_only  
    ):  
        if (  
            item.temp_path  
            and item.temp_path != item.source_path  
            and Path(  
                item.temp_path  
            ).exists()  
        ):  
            return item.temp_path  

        suffix = Path(  
            source_path  
        ).suffix.lower()  

        if suffix in {  
            ".mp3",  
            ".m4a",  
            ".aac",  
            ".opus",  
            ".ogg",  
            ".wav",  
        }:  
            item.temp_path = source_path  

            return source_path  

        extracted = (  
            await _extract_audio(  
                source_path  
            )  
        )  

        item.temp_path = extracted  

        return extracted  

    return source_path

# =========================================================
# CLEANUP
# =========================================================

async def _cleanup_path(
    path: Optional[str],
):
    if not path:
        return

    try:  
        await asyncio.to_thread(  
            Path(path).unlink,  
            missing_ok=True,  
        )  
    except Exception:  
        log.exception(  
            "MEDIA: cleanup failed: %s",  
            path,  
        )

async def _cleanup_item(
    item: Optional[MediaItem],
):
    if not item:
        return

    paths = set()  

    if item.temp_path:  
        paths.add(  
            item.temp_path  
        )  

    if item.source_path:  
        paths.add(  
            item.source_path  
        )  

    for path in paths:  
        await _cleanup_path(  
            path  
        )

# =========================================================
# NOW PLAYING
# =========================================================

async def _send_now_playing(
    chat_id: int,
    item: MediaItem,
):
    icon = (  
        "🎬"  
        if (  
            item.kind == "video"  
            and not item.audio_only  
        )  
        else "🎵"  
    )  

    media_type = (  
        "Video"  
        if (  
            item.kind == "video"  
            and not item.audio_only  
        )  
        else "Audio"  
    )  

    repeat_info = ""  

    if item.repeat_count > 1:  
        repeat_info = (  
            f"\n"  
            f"• 🔁 Remaining plays: "  
            f"{item.repeat_count}"  
        )  

    text = (  
        f"{icon} <b>Now Playing</b>\n\n"  
        f"• {item.title}\n"  
        f"• {media_type}"  
        f"{repeat_info}"  
    )  

    await _send_status_message(  
        chat_id=chat_id,  
        text=text,  
        reply_markup=_buttons(chat_id),  
        thread_id=item.thread_id,  
    )

# =========================================================
# START CURRENT
# =========================================================

async def _start_current(
    chat_id: int,
) -> bool:
    s = _state(chat_id)

    if s.current is None:  
        if not s.queue:  
            return False  

        s.current = s.queue.popleft()  

    if s.starting:  
        return False  

    s.starting = True  

    item = s.current  

    try:  
        log.info(  
            "MEDIA START: chat=%s title=%r kind=%s "  
            "audio_only=%s url=%s repeat=%s",  
            chat_id,  
            item.title,  
            item.kind,  
            item.audio_only,  
            bool(item.url),  
            item.repeat_count,  
        )  

        await _ensure_engine()  

        if (  
            item.source_path  
            and Path(  
                item.source_path  
            ).exists()  
        ):  
            source_path = (  
                item.source_path  
            )  
        elif (  
            item.temp_path  
            and Path(  
                item.temp_path  
            ).exists()  
        ):  
            source_path = (  
                item.temp_path  
            )  
        else:  
            source_path = (  
                await _download_item(  
                    item  
                )  
            )  

            item.source_path = (  
                source_path  
            )  

        if (  
            item.temp_path  
            and item.temp_path != item.source_path  
            and Path(  
                item.temp_path  
            ).exists()  
        ):  
            path = item.temp_path  
        else:  
            path = (  
                await _prepare_playable_path(  
                    item,  
                    source_path,  
                )  
            )  

        if not Path(path).exists():  
            raise FileNotFoundError(  
                path  
            )  

        stream = _build_stream(  
            item,  
            path,  
        )  

        log.info(  
            "MEDIA PLAY: chat=%s path=%s",  
            chat_id,  
            path,  
        )  

        result = CALLS.play(  
            chat_id,  
            stream,  
        )  

        if inspect.isawaitable(result):  
            await asyncio.wait_for(  
                result,  
                PLAY_TIMEOUT,  
            )  

        s.paused = False  
        s.starting = False  

        s.ignore_end_until = (  
            asyncio.get_running_loop()  
            .time() + 2.5  
        )  

        await _send_now_playing(  
            chat_id,  
            item,  
        )  

        log.info(  
            "MEDIA START OK: chat=%s title=%r",  
            chat_id,  
            item.title,  
        )  

        return True  

    except asyncio.CancelledError:  
        s.starting = False  
        raise  

    except Exception as exc:  
        log.exception(  
            "MEDIA START FAILED: chat=%s error=%r",  
            chat_id,  
            exc,  
        )  

        s.starting = False  

        failed = s.current  
        s.current = None  

        await _delete_status_messages(  
            chat_id  
        )  

        if failed:  
            await _cleanup_item(  
                failed  
            )  

        await _safe_leave(  
            chat_id  
        )  

        if s.queue:  
            return await _start_current(  
                chat_id  
            )  

        return False

# =========================================================
# ITEM IDENTITY
# =========================================================

def _item_key(
    item: Optional[MediaItem],
) -> Optional[str]:
    if not item:
        return None

    if item.url:  
        return (  
            f"url:"  
            f"{item.url}:"  
            f"{item.audio_only}"  
        )  

    if item.tts_text:  
        return (  
            f"tts:"  
            f"{item.chat_id}:"  
            f"{item.message_id}"  
        )  

    return (  
        f"telegram:"  
        f"{item.chat_id}:"  
        f"{item.message_id}:"  
        f"{item.audio_only}"  
    )

def _same_item(
    first: MediaItem,
    second: MediaItem,
) -> bool:
    return (
        _item_key(first)
        == _item_key(second)
    )

# =========================================================
# STREAM END
# =========================================================

async def _handle_stream_end(
    chat_id: int,
):
    async with _lock(chat_id):
        s = _state(chat_id)  
        loop = asyncio.get_running_loop()  
        now = loop.time()  

        if s.suppress_end_until > now:  
            log.info(  
                "MEDIA END ignored: suppressed chat=%s",  
                chat_id,  
            )  
            return  

        if s.ignore_end_until > now:  
            log.info(  
                "MEDIA END ignored: delayed event chat=%s",  
                chat_id,  
            )  
            return  

        if s.ending:  
            log.info(  
                "MEDIA END ignored: already processing chat=%s",  
                chat_id,  
            )  
            return  

        finished = s.current  

        if not finished:  
            return  

        finished_key = _item_key(finished)  

        if (  
            s.last_finished_key == finished_key  
            and (now - s.last_finished_at) < 2.0  
        ):  
            log.info(  
                "MEDIA END ignored: duplicate event chat=%s key=%s",  
                chat_id,  
                finished_key,  
            )  
            return  

        s.ending = True  
        s.last_finished_key = finished_key  
        s.last_finished_at = now  

        try:  
            await _delete_status_messages(chat_id)  

            if finished.repeat_count > 1:  
                finished.repeat_count -= 1  
                s.current = finished  
                s.paused = False  
                s.starting = False  

                await asyncio.sleep(0.25)  

                started = await _start_current(chat_id)  

                if not started:  
                    s.current = None  
                    await _cleanup_item(finished)  

                    if s.queue:  
                        await _start_current(chat_id)  
                    else:  
                        await _safe_leave(chat_id)  

                return  

            await _cleanup_item(finished)  

            s.current = None  
            s.paused = False  
            s.starting = False  

            if s.queue:  
                s.ignore_end_until = loop.time() + 2.5  
                await asyncio.sleep(0.25)  

                started = await _start_current(chat_id)  

                if not started:  
                    s.current = None  
                    if not s.queue:  
                        await _safe_leave(chat_id)  

                return  

            s.current = None  

            log.info(  
                "MEDIA END: queue empty; leaving chat=%s",  
                chat_id,  
            )  

            await _safe_leave(chat_id)  

        except Exception:  
            log.exception(  
                "MEDIA END HANDLER FAILED chat=%s",  
                chat_id,  
            )  
        finally:  
            s.ending = False

# =========================================================
# SEEK
# =========================================================

async def _seek_current(
    chat_id: int,
    seconds: int,
):
    s = _state(chat_id)

    if not s.current:  
        return (  
            False,  
            "ℹ️ لا يوجد تشغيل."  
        )  

    if s.paused:  
        return (  
            False,  
            "ℹ️ أوقف الإيقاف المؤقت أولًا."  
        )  

    item = s.current  

    try:  
        await _ensure_engine()  

        current_time = CALLS.time(chat_id)  

        if inspect.isawaitable(current_time):  
            current_time = await current_time  

        try:  
            current_time = float(current_time)  
        except Exception:  
            return (  
                False,  
                "❌ تعذر معرفة موضع التشغيل الحالي.",  
            )  

        target = max(  
            0,  
            int(current_time) + seconds,  
        )  

        if (  
            item.temp_path  
            and Path(item.temp_path).exists()  
        ):  
            source_path = item.temp_path  
        elif (  
            item.source_path  
            and Path(item.source_path).exists()  
        ):  
            source_path = item.source_path  
        else:  
            source_path = await _download_item(item)  
            item.source_path = source_path  
            source_path = await _prepare_playable_path(  
                item,  
                source_path,  
            )  
            item.temp_path = source_path  

        duration_check = None  

        try:  
            probe = (  
                await asyncio.create_subprocess_exec(  
                    "ffprobe",  
                    "-v",  
                    "error",  
                    "-show_entries",  
                    "format=duration",  
                    "-of",  
                    "default=noprint_wrappers=1:nokey=1",  
                    source_path,  
                    stdout=asyncio.subprocess.PIPE,  
                    stderr=asyncio.subprocess.PIPE,  
                )  
            )  

            stdout, _ = await asyncio.wait_for(  
                probe.communicate(),  
                20,  
            )  

            duration_check = float(  
                stdout.decode().strip()  
            )  
        except Exception:  
            duration_check = None  

        if (  
            duration_check is not None  
            and target >= duration_check  
        ):  
            return (  
                False,  
                "ℹ️ وصلت إلى نهاية الملف.",  
            )  

        if (  
            item.kind == "video"  
            and not item.audio_only  
        ):  
            seek_path = (  
                TEMP_ROOT  
                / (  
                    f"seek_"  
                    f"{chat_id}_"  
                    f"{item.message_id}_"  
                    f"{random.randint(100000, 999999)}"  
                    f".mp4"  
                )  
            )  
        else:  
            seek_path = (  
                TEMP_ROOT  
                / (  
                    f"seek_"  
                    f"{chat_id}_"  
                    f"{item.message_id}_"  
                    f"{random.randint(100000, 999999)}"  
                    f".mp3"  
                )  
            )  

        command = [  
            "ffmpeg",  
            "-y",  
            "-ss",  
            str(target),  
            "-i",  
            source_path,  
        ]  

        if (  
            item.kind == "video"  
            and not item.audio_only  
        ):  
            command += [  
                "-map",  
                "0:v:0",  
                "-map",  
                "0:a:0?",  
                "-c:v",  
                "libx264",  
                "-preset",  
                "veryfast",  
                "-crf",  
                "23",  
                "-pix_fmt",  
                "yuv420p",  
                "-c:a",  
                "aac",  
                "-b:a",  
                "128k",  
                "-movflags",  
                "+faststart",  
                str(seek_path),  
            ]  
        else:  
            command += [  
                "-vn",  
                "-c:a",  
                "libmp3lame",  
                "-q:a",  
                "4",  
                str(seek_path),  
            ]  

        try:  
            await _run_ffmpeg(command, 120)  
        except Exception as exc:  
            log.error(  
                "MEDIA SEEK FAILED: %r",  
                exc,  
            )  
            await _cleanup_path(str(seek_path))  
            return (  
                False,  
                "❌ تعذر تحريك التشغيل.",  
            )  

        old_temp = item.temp_path  

        await _delete_status_messages(chat_id)  

        s.suppress_end_until = (  
            asyncio.get_running_loop()  
            .time() + 6  
        )  

        await _safe_leave(chat_id)  

        item.temp_path = str(seek_path)  

        stream = _build_stream(  
            item,  
            str(seek_path),  
        )  

        result = CALLS.play(chat_id, stream)  

        if inspect.isawaitable(result):  
            await asyncio.wait_for(  
                result,  
                PLAY_TIMEOUT,  
            )  

        if (  
            old_temp  
            and old_temp != item.source_path  
            and old_temp != str(seek_path)  
        ):  
            await _cleanup_path(old_temp)  

        s.suppress_end_until = (  
            asyncio.get_running_loop()  
            .time() + 2.5  
        )  
        s.paused = False  

        await _send_now_playing(chat_id, item)  

        log.info(  
            "MEDIA SEEK OK: chat=%s from=%s to=%s",  
            chat_id,  
            current_time,  
            target,  
        )  

        return (  
            True,  
            f"⏱ تم الانتقال إلى {target} ثانية.",  
        )  

    except Exception as exc:  
        log.exception(  
            "MEDIA SEEK FAILED: %r",  
            exc,  
        )  

        return (  
            False,  
            "❌ تعذر تغيير موضع التشغيل.",  
        )

# =========================================================
# ENQUEUE
# =========================================================

async def _enqueue(
    update: Update,
    context,
    mode="auto",
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:  
        return  

    source = _source_message(update)  

    if source is None:  
        await message.reply_text(  
            "🎵 رد على صوت أو 🎬 فيديو أو 📝 نص ثم اكتب /play"  
        )  
        return  

    play_count = _extract_play_count(  
        update,  
        context,  
    )  

    url = _extract_url(source)  

    if url:  
        thread_id = (  
            getattr(  
                message,  
                "message_thread_id",  
                None,  
            )  
            or getattr(  
                source,  
                "message_thread_id",  
                None,  
            )  
        )  

        item = MediaItem(  
            chat_id=message.chat.id,  
            message_id=source.message_id,  
            title="YouTube",  
            kind="video",  
            requester_id=user.id,  
            thread_id=thread_id,  
            audio_only=(mode == "audio"),  
            url=url,  
            repeat_count=play_count,  
        )  

    elif (  
        mode == "auto"  
        and getattr(source, "text", None)  
    ):  
        text = source.text.strip()  

        if not text:  
            await message.reply_text(  
                "❌ النص فارغ."  
            )  
            return  

        thread_id = (  
            getattr(  
                message,  
                "message_thread_id",  
                None,  
            )  
            or getattr(  
                source,  
                "message_thread_id",  
                None,  
            )  
        )  

        item = MediaItem(  
            chat_id=message.chat.id,  
            message_id=source.message_id,  
            title=(  
                f"Reading: "  
                f"{text[:60]}"  
                + (  
                    "..."  
                    if len(text) > 60  
                    else ""  
                )  
            ),  
            kind="audio",  
            requester_id=user.id,  
            thread_id=thread_id,  
            audio_only=True,  
            tts_text=text,  
            repeat_count=play_count,  
        )  
    else:  
        media = _media_from_message(source)  

        if media is None:  
            await message.reply_text(  
                "🎵 رد على صوت أو 🎬 فيديو أو 📝 نص ثم اكتب /play"  
            )  
            return  

        kind, title = media  

        if (  
            mode == "video"  
            and kind != "video"  
        ):  
            await message.reply_text(  
                "❌ /video يجب أن يكون ردًا على فيديو أو رابط YouTube."  
            )  
            return  

        if (  
            mode == "audio"  
            and kind != "video"  
        ):  
            await message.reply_text(  
                "❌ /audio هنا مخصص لتشغيل الفيديو كصوت فقط."  
            )  
            return  

        thread_id = (  
            getattr(  
                message,  
                "message_thread_id",  
                None,  
            )  
            or getattr(  
                source,  
                "message_thread_id",  
                None,  
            )  
        )  

        item = MediaItem(  
            chat_id=message.chat.id,  
            message_id=source.message_id,  
            title=title,  
            kind=kind,  
            requester_id=user.id,  
            thread_id=thread_id,  
            audio_only=(mode == "audio"),  
            repeat_count=play_count,  
        )  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        existing = None  

        if s.current and _same_item(  
            s.current,  
            item,  
        ):  
            existing = s.current  
        else:  
            for queued in s.queue:  
                if _same_item(  
                    queued,  
                    item,  
                ):  
                    existing = queued  
                    break  

        if existing:  
            existing.repeat_count = min(  
                MAX_REPEAT_COUNT,  
                existing.repeat_count  
                + play_count,  
            )  

            location = (  
                "التشغيل الحالي"  
                if existing is s.current  
                else "قائمة الانتظار"  
            )  

            await message.reply_text(  
                f"🔁 تمت إضافة "  
                f"{play_count} تشغيل"  
                f" إلى {location}.\n"  
                f"📌 الإجمالي الآن: "  
                f"{existing.repeat_count}"  
            )  
            return  

        if len(s.queue) >= MAX_QUEUE:  
            await message.reply_text(  
                f"❌ Queue is full. Maximum: {MAX_QUEUE}."  
            )  
            return  

        idle = (  
            s.current is None  
            and not s.starting  
        )  

        if idle:  
            s.current = item  

            await _send_status_message(  
                chat_id=chat_id,  
                text=(  
                    f"▶️ Starting: "  
                    f"<b>{item.title}</b>"  
                    + (  
                        f"\n🔁 Plays: "  
                        f"{item.repeat_count}"  
                        if item.repeat_count > 1  
                        else ""  
                    )  
                ),  
                thread_id=item.thread_id,  
            )  

            started = await _start_current(  
                chat_id  
            )  

            if not started:  
                await _delete_status_messages(  
                    chat_id  
                )  
                await message.reply_text(  
                    "❌ تعذر تشغيل الوسائط. "  
                    "راجع Railway Logs التي تبدأ بـ MEDIA."  
                )  
            return  

        s.queue.append(item)  

        await _send_status_message(  
            chat_id=chat_id,  
            text=(  
                f"➕ Added to queue:\n"  
                f"<b>{item.title}</b>\n"  
                f"📍 Position: {len(s.queue)}"  
                + (  
                    f"\n🔁 Plays: "  
                    f"{item.repeat_count}"  
                    if item.repeat_count > 1  
                    else ""  
                )  
            ),  
            thread_id=item.thread_id,  
        )

# =========================================================
# PLAY COMMANDS
# =========================================================

async def play_handler(update, context):
    await _enqueue(update, context, "auto")

async def audio_handler(update, context):
    await _enqueue(update, context, "audio")

async def video_handler(update, context):
    await _enqueue(update, context, "video")

# =========================================================
# PAUSE & RESUME
# =========================================================

async def pause_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        if not s.current:  
            await message.reply_text(  
                "ℹ️ لا يوجد تشغيل."  
            )  
            return  

        try:  
            await _ensure_engine()  
            result = CALLS.pause(chat_id)  
            if inspect.isawaitable(result):  
                await asyncio.wait_for(result, 20)  

            s.paused = True  
            await message.reply_text("⏸ Paused.")  
        except Exception as exc:  
            log.exception("MEDIA PAUSE FAILED")  
            await message.reply_text(f"❌ Pause failed: {exc}")

async def resume_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        if not s.current:  
            await message.reply_text(  
                "ℹ️ لا يوجد تشغيل."  
            )  
            return  

        try:  
            await _ensure_engine()  
            result = CALLS.resume(chat_id)  
            if inspect.isawaitable(result):  
                await asyncio.wait_for(result, 20)  

            s.paused = False  
            await message.reply_text("▶️ Resumed.")  
        except Exception as exc:  
            log.exception("MEDIA RESUME FAILED")  
            await message.reply_text(f"❌ Resume failed: {exc}")

# =========================================================
# SKIP & STOP
# =========================================================

async def skip_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        if not s.current:  
            await message.reply_text(  
                "ℹ️ لا يوجد تشغيل."  
            )  
            return  

        old = s.current  

        s.suppress_end_until = (  
            asyncio.get_running_loop()  
            .time() + 6  
        )  
        s.ignore_end_until = (  
            asyncio.get_running_loop()  
            .time() + 6  
        )  

        await _delete_status_messages(chat_id)  
        await _safe_leave(chat_id)  
        await _cleanup_item(old)  

        s.current = None  
        s.paused = False  
        s.starting = False  
        s.ending = False  

        if s.queue:  
            started = await _start_current(chat_id)  
            if started:  
                await message.reply_text("⏭ Skipped.")  
                return  

        await message.reply_text("⏭ Skipped. Queue is empty.")

async def stop_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        now = asyncio.get_running_loop().time()  

        s.suppress_end_until = now + 8  
        s.ignore_end_until = now + 8  

        await _delete_status_messages(chat_id)  
        await _safe_leave(chat_id)  

        if s.current:  
            await _cleanup_item(s.current)  

        for item in s.queue:  
            await _cleanup_item(item)  

        s.queue.clear()  
        s.current = None  
        s.paused = False  
        s.starting = False  
        s.ending = False  
        s.last_finished_key = None  
        s.last_finished_at = 0.0  

        await message.reply_text(  
            "⏹ Playback stopped and queue cleared."  
        )

async def leave_handler(update, context):
    await stop_handler(update, context)

# =========================================================
# QUEUE, NOW & CLEAR
# =========================================================

async def queue_handler(update, context):
    message = update.effective_message
    if not message:  
        return  

    await message.reply_text(  
        _format_queue(_state(message.chat.id))  
    )

async def now_handler(update, context):
    message = update.effective_message
    if not message:  
        return  

    s = _state(message.chat.id)  

    if not s.current:  
        await message.reply_text(  
            "ℹ️ Nothing is playing."  
        )  
        return  

    status = (  
        "⏸ Paused"  
        if s.paused  
        else "▶️ Playing"  
    )  

    await message.reply_text(  
        f"🎵 {status}\n\n"  
        f"• {s.current.title}\n"  
        f"• 🔁 Plays remaining: "  
        f"{s.current.repeat_count}\n"  
        f"• Queue: {len(s.queue)}"  
    )

async def clear_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  
        count = len(s.queue)  

        for item in s.queue:  
            await _cleanup_item(item)  

        s.queue.clear()  
        await message.reply_text(  
            f"🧹 Cleared {count} queued item(s)."  
        )

async def shuffle_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        if len(s.queue) < 2:  
            await message.reply_text(  
                "ℹ️ Not enough items."  
            )  
            return  

        items = list(s.queue)  
        random.shuffle(items)  
        s.queue = deque(items)  

        await message.reply_text("🔀 Queue shuffled.")

async def repeat_add_handler(update, context):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        s = _state(chat_id)  

        if not s.current:  
            await message.reply_text(  
                "ℹ️ لا يوجد مقطع حالي."  
            )  
            return  

        if s.current.repeat_count >= MAX_REPEAT_COUNT:  
            await message.reply_text(  
                f"❌ وصلت إلى الحد الأقصى ({MAX_REPEAT_COUNT})."  
            )  
            return  

        s.current.repeat_count += 1  
        remaining = s.current.repeat_count  

        await message.reply_text(  
            f"🔁 تمت إضافة تشغيل واحد.\n"  
            f"📌 إجمالي مرات التشغيل المتبقية: {remaining}"  
        )

# =========================================================
# SEEK HANDLERS
# =========================================================

async def _seek_handler(
    update,
    context,
    seconds: int,
):
    if not await _require_admin(update, context):
        return  

    message = update.effective_message  
    if not message:  
        return  

    chat_id = message.chat.id  

    async with _lock(chat_id):  
        success, text = await _seek_current(  
            chat_id,  
            seconds,  
        )  
        await message.reply_text(text)

async def back10_handler(update, context):
    await _seek_handler(update, context, -10)

async def forward10_handler(update, context):
    await _seek_handler(update, context, 10)

# =========================================================
# CALLBACKS
# =========================================================

async def _callback_handler(update, context):
    query = update.callback_query
    if not query:  
        return  

    try:  
        await query.answer()  
    except Exception:  
        pass  

    parts = (query.data or "").split(":")  

    if len(parts) != 3 or parts[0] != "mp":  
        return  

    if not await _is_admin(update, context):  
        try:  
            await query.answer(  
                "Admins only.",  
                show_alert=True,  
            )  
        except Exception:  
            pass  
        return  

    action = parts[2]  

    if action == "hide":  
        try:  
            await query.edit_message_reply_markup(  
                reply_markup=None  
            )  
        except Exception:  
            pass  
        return  

    if action == "repeat_add":  
        await repeat_add_handler(update, context)  
        return  

    handlers = {  
        "back10": back10_handler,  
        "forward10": forward10_handler,  
        "pause": pause_handler,  
        "resume": resume_handler,  
        "skip": skip_handler,  
        "stop": stop_handler,  
        "queue": queue_handler,  
    }  

    handler = handlers.get(action)  
    if handler:  
        await handler(update, context)

# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown_media_player():
    global USER_CLIENT
    global CALLS
    global ENGINE_READY
    global EVENTS_REGISTERED
    global BOT_INSTANCE

    log.info("MEDIA SHUTDOWN: starting...")  

    for chat_id in list(PLAYERS):  
        try:  
            async with _lock(chat_id):  
                s = PLAYERS[chat_id]  
                await _delete_status_messages(chat_id)  

                if CALLS:  
                    await _safe_leave(chat_id)  

                if s.current:  
                    await _cleanup_item(s.current)  

                for item in s.queue:  
                    await _cleanup_item(item)  
        except Exception:  
            log.exception(  
                "MEDIA SHUTDOWN failed chat=%s",  
                chat_id,  
            )  

    PLAYERS.clear()  
    CHAT_LOCKS.clear()  

    if CALLS:  
        try:  
            result = CALLS.stop()  
            if inspect.isawaitable(result):  
                await asyncio.wait_for(result, 20)  
        except Exception:  
            log.exception(  
                "MEDIA SHUTDOWN: PyTgCalls stop failed."  
            )  

    CALLS = None  

    if USER_CLIENT:  
        try:  
            if (  
                getattr(USER_CLIENT, "is_connected", False)  
                or getattr(USER_CLIENT, "is_started", False)  
            ):  
                await asyncio.wait_for(  
                    USER_CLIENT.stop(),  
                    20,  
                )  
        except Exception:  
            log.exception(  
                "MEDIA SHUTDOWN: assistant stop failed."  
            )  

    USER_CLIENT = None  
    ENGINE_READY = False  
    EVENTS_REGISTERED = False  
    BOT_INSTANCE = None  

    log.info("MEDIA SHUTDOWN: complete.")

async def _post_shutdown(application):
    await shutdown_media_player()

# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_media_play(application: Application):
    global BOT_INSTANCE
    BOT_INSTANCE = application.bot  

    application.add_handler(CommandHandler("play", play_handler))  
    application.add_handler(CommandHandler("audio", audio_handler))  
    application.add_handler(CommandHandler("video", video_handler))  
    application.add_handler(CommandHandler("pause", pause_handler))  
    application.add_handler(CommandHandler("resume", resume_handler))  
    application.add_handler(CommandHandler("skip", skip_handler))  
    application.add_handler(CommandHandler("stop", stop_handler))  
    application.add_handler(CommandHandler("leave", leave_handler))  
    application.add_handler(CommandHandler("queue", queue_handler))  
    application.add_handler(CommandHandler("now", now_handler))  
    application.add_handler(CommandHandler("clear", clear_handler))  
    application.add_handler(CommandHandler("shuffle", shuffle_handler))  

    application.add_handler(  
        CallbackQueryHandler(  
            _callback_handler,  
            pattern=r"^mp:",  
        )  
    )  

    application.add_handler(  
        MessageHandler(  
            filters.TEXT  
            & filters.Regex(  
                r"^(?:شغل|play)"  
                r"(?:\s+[0-9٠-٩۰-۹]+)?$"  
            ),  
            play_handler,  
        )  
    )  

    application.add_handler(  
        MessageHandler(  
            filters.TEXT  
            & filters.Regex(  
                r"^(?:شغل صوت|شغل صوتي|play audio)"  
                r"(?:\s+[0-9٠-٩۰-۹]+)?$"  
            ),  
            audio_handler,  
        )  
    )  

    application.add_handler(  
        MessageHandler(  
            filters.TEXT  
            & filters.Regex(  
                r"^(?:شغل فيديو|play video|فيديو|video)"  
                r"(?:\s+[0-9٠-٩۰-۹]+)?$"  
            ),  
            video_handler,  
        )  
    )  

    application.post_shutdown = _post_shutdown  

    log.info("MEDIA PLAY: handlers registered.")
