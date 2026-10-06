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

    repeat_count: int = 1
    fail_count: int = 0

@dataclass
class PlayerState:
    queue: deque

    current: Optional[MediaItem] = None  

    paused: bool = False  
    starting: bool = False  

    suppress_end_until: float = 0.0  
    status_message_ids: set[int] = None  
    ending: bool = False  

    last_finished_key: Optional[str] = None  
    last_finished_at: float = 0.0  

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

    document = getattr(message, "document", None)  

    if document:  
        mime = (document.mime_type or "").lower()  

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

    url = match.group(0).strip().rstrip(".,!?;:)]}")  
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
    args = list(context.args or [])  
    if args:  
        for arg in args:  
            normalized = _normalize_digits(str(arg).strip())  
            if normalized.isdigit():  
                try:  
                    count = int(normalized)  
                    return max(1, min(count, MAX_REPEAT_COUNT))  
                except Exception:  
                    pass  

    message = update.effective_message  
    if not message:  
        return 1  

    text = (getattr(message, "text", None) or "").strip()  
    match = re.search(  
        r"(?:شغل|play|video|audio)"  
        r"\s+([0-9٠-٩۰-۹]+)"  
        r"\s*$",  
        text,  
        re.IGNORECASE,  
    )  

    if match:  
        normalized = _normalize_digits(match.group(1))  
        try:  
            count = int(normalized)  
            return max(1, min(count, MAX_REPEAT_COUNT))  
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
        log.exception("Admin check failed.")  
        return False

async def _require_admin(
    update,
    context,
) -> bool:
    if await _is_admin(update, context):
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

def _repeat_text(item: MediaItem) -> str:
    if item.repeat_count > 1:
        return f" ×{item.repeat_count}"
    return ""

def _format_queue(s: PlayerState) -> str:
    lines = []

    if s.current:  
        status = "⏸ Paused" if s.paused else "▶️ Playing"  
        icon = "🎬" if (s.current.kind == "video" and not s.current.audio_only) else "🎵"  
        lines.append(  
            f"{icon} {status}: "  
            f"{s.current.title}"  
            f"{_repeat_text(s.current)}"  
        )  

    if s.queue:  
        lines.append("")  
        lines.append("📋 Queue:")  

        for i, item in enumerate(s.queue, 1):  
            icon = "🎬" if (item.kind == "video" and not item.audio_only) else "🎵"  
            lines.append(  
                f"{i}. {icon} "  
                f"{item.title}"  
                f"{_repeat_text(item)}"  
            )  

    return "\n".join(lines) if lines else "📋 Queue is empty."

# =========================================================
# STATUS MESSAGES
# =========================================================

async def _delete_status_messages(chat_id: int):
    if BOT_INSTANCE is None:  
        return  

    s = _state(chat_id)  
    message_ids = list(s.status_message_ids)  

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
            if "MessageToDeleteNotFound" in text or "message to delete not found" in text.lower():  
                continue  
            if "Topic_closed" in text:  
                continue  

async def _send_status_message(
    chat_id: int,
    text: str,
    reply_markup=None,
    thread_id: Optional[int] = None,
):
    if BOT_INSTANCE is None:
        return None

    await _delete_status_messages(chat_id)

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
        sent = await BOT_INSTANCE.send_message(**kwargs)  
        _state(chat_id).status_message_ids.add(sent.message_id)  
        return sent  
    except Exception as exc:  
        if "Topic_closed" not in repr(exc):  
            log.exception("MEDIA: status message failed.")  
        return None

# =========================================================
# PYTGCALLS ENGINE
# =========================================================

async def _ensure_engine():
    global USER_CLIENT
    global CALLS
    global ENGINE_READY

    if ENGINE_READY and USER_CLIENT is not None and CALLS is not None:  
        return  

    async with ENGINE_LOCK:  
        if ENGINE_READY and USER_CLIENT is not None and CALLS is not None:  
            return  

        if not API_ID or not API_HASH or not SESSION_STRING:  
            raise RuntimeError("Missing API_ID, API_HASH, or SESSION_STRING.")  

        if PyTgCalls is None:  
            raise RuntimeError("PyTgCalls import failed.")  

        if shutil.which("ffmpeg") is None:  
            raise RuntimeError("FFmpeg is not installed.")  

        if USER_CLIENT is None:  
            USER_CLIENT = Client(  
                "fixmyenglish_player",  
                api_id=API_ID,  
                api_hash=API_HASH,  
                session_string=SESSION_STRING,  
                in_memory=True,  
            )  

        if not getattr(USER_CLIENT, "is_connected", False):  
            await USER_CLIENT.start()  

        if CALLS is None:  
            CALLS = PyTgCalls(USER_CLIENT)  
            result = CALLS.start()  
            if inspect.isawaitable(result):  
                await result  

        _register_call_events()  
        ENGINE_READY = True  

def _register_call_events():
    global EVENTS_REGISTERED
    if EVENTS_REGISTERED:  
        return  

    if CALLS is None or call_filters is None:  
        raise RuntimeError("PyTgCalls event API unavailable.")  

    @CALLS.on_update(call_filters.stream_end())  
    async def stream_end(_, update):  
        chat_id = getattr(update, "chat_id", None)  
        if chat_id is not None:  
            asyncio.create_task(_handle_stream_end(int(chat_id)))  

    EVENTS_REGISTERED = True

# =========================================================
# STREAM BUILDING
# =========================================================

def _build_stream(item: MediaItem, path: str):
    if not Path(path).exists():
        raise FileNotFoundError(path)

    if MediaStream is None:  
        raise RuntimeError("MediaStream is unavailable.")  

    if item.kind == "video" and not item.audio_only:  
        return MediaStream(  
            path,  
            audio_parameters=AudioQuality.HIGH,  
            video_parameters=VideoQuality.HD_720p,  
        )  

    return MediaStream(  
        path,  
        audio_parameters=AudioQuality.HIGH,  
    )

# =========================================================
# LEAVE VOICE CHAT
# =========================================================

async def _safe_leave(chat_id: int):
    if CALLS is None:
        return

    await asyncio.sleep(0.5)  
    for attempt in range(2):  
        try:  
            result = CALLS.leave_call(chat_id)  
            if inspect.isawaitable(result):  
                await asyncio.wait_for(result, 20)  
            return  
        except Exception as exc:  
            text = repr(exc)  
            if "NotInCall" in text or "not in call" in text.lower():  
                return  
            if attempt == 0:  
                await asyncio.sleep(1)  

# =========================================================
# TTS & DOWNLOADS
# =========================================================

async def _make_tts(text: str) -> str:
    safe_name = f"tts{abs(hash(text))}_{random.randint(1000, 9999)}"
    path = TEMP_ROOT / f"{safe_name}.mp3"  

    communicate = edge_tts.Communicate(text=text, voice="en-US-AriaNeural", rate="+0%")  
    await asyncio.wait_for(communicate.save(str(path)), 60)  

    if not path.exists():  
        raise RuntimeError("TTS file was not created.")  
    return str(path)

async def _download_url_item(item: MediaItem) -> str:
    if not item.url:
        raise RuntimeError("URL is missing.")

    safe_id = f"url_{item.chat_id}_{item.message_id}_{random.randint(100000, 999999)}"  
    output_template = str(TEMP_ROOT / f"{safe_id}.%(ext)s")  

    if item.audio_only:  
        ydl_opts = {  
            "outtmpl": output_template,  
            "format": "bestaudio/best",  
            "noplaylist": True,  
            "quiet": True,  
            "no_warnings": True,  
            "restrictfilenames": True,  
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],  
        }  
    else:  
        ydl_opts = {  
            "outtmpl": output_template,  
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",  
            "merge_output_format": "mp4",  
            "noplaylist": True,  
            "quiet": True,  
            "no_warnings": True,  
            "restrictfilenames": True,  
        }  

    def download():  
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  
            info = ydl.extract_info(item.url, download=True)  
            if not info:  
                raise RuntimeError("yt-dlp returned no information.")  
            if info.get("entries"):  
                entries = info["entries"]  
                if entries:  
                    info = entries[0]  
            item.title = info.get("title") or item.title or "Media"  

    await asyncio.wait_for(asyncio.to_thread(download), DOWNLOAD_TIMEOUT)  

    candidates = sorted(TEMP_ROOT.glob(f"{safe_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)  
    candidates = [p for p in candidates if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".tmp"}]  

    if not candidates:  
        raise RuntimeError("yt-dlp downloaded no playable file.")  

    return str(candidates[0])

async def _download_item(item: MediaItem) -> str:
    if item.url:  
        return await _download_url_item(item)  

    if USER_CLIENT is None:  
        raise RuntimeError("Assistant is not running.")  

    if item.tts_text:  
        return await _make_tts(item.tts_text)  

    source = await asyncio.wait_for(USER_CLIENT.get_messages(item.chat_id, item.message_id), MESSAGE_TIMEOUT)  
    if not source or _media_from_message(source) is None:  
        raise RuntimeError("Source message not found or invalid.")  

    prefix = f"fixmyenglish_{item.chat_id}_{item.message_id}_{random.randint(100000, 999999)}"  
    path = await asyncio.wait_for(USER_CLIENT.download_media(source, file_name=str(TEMP_ROOT / prefix)), DOWNLOAD_TIMEOUT)  

    if not path:  
        raise RuntimeError("Telegram media download failed.")  
    return str(path)

# =========================================================
# FFMPEG HELPERS
# =========================================================

async def _run_ffmpeg(command: list[str], timeout: int):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)  

    if process.returncode != 0:  
        raise RuntimeError(stderr.decode(errors="ignore")[-4000:] or "FFmpeg failed.")  
    return stdout, stderr

async def _normalize_video(source_path: str) -> str:
    source = Path(source_path)  
    output = TEMP_ROOT / f"normalized_{source.stem}_{random.randint(100000, 999999)}.mp4"  
    command = [  
        "ffmpeg", "-y", "-i", str(source),  
        "-map", "0:v:0", "-map", "0:a:0?",  
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",  
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),  
    ]  
    await _run_ffmpeg(command, VIDEO_NORMALIZE_TIMEOUT)  
    return str(output)

async def _extract_audio(source_path: str) -> str:
    source = Path(source_path)  
    output = TEMP_ROOT / f"audio_{source.stem}_{random.randint(100000, 999999)}.mp3"  
    command = ["ffmpeg", "-y", "-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(output)]  
    await _run_ffmpeg(command, AUDIO_EXTRACT_TIMEOUT)  
    return str(output)

async def _prepare_playable_path(item: MediaItem, source_path: str) -> str:
    if item.kind == "video" and not item.audio_only:  
        if item.temp_path and Path(item.temp_path).exists() and item.temp_path != item.source_path:  
            return item.temp_path  
        return await _normalize_video(source_path)  

    if item.kind == "video" and item.audio_only:  
        if item.temp_path and Path(item.temp_path).exists() and item.temp_path != item.source_path:  
            return item.temp_path  
        if Path(source_path).suffix.lower() in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav"}:  
            return source_path  
        return await _extract_audio(source_path)  

    return source_path

# =========================================================
# CLEANUP
# =========================================================

async def _cleanup_path(path: Optional[str]):
    if not path:
        return
    try:  
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)  
    except Exception:  
        pass

async def _cleanup_item(item: Optional[MediaItem]):
    if not item:
        return
    if item.temp_path:  
        await _cleanup_path(item.temp_path)  
    if item.source_path:  
        await _cleanup_path(item.source_path)

# =========================================================
# START CURRENT
# =========================================================

async def _send_now_playing(chat_id: int, item: MediaItem):
    icon = "🎬" if (item.kind == "video" and not item.audio_only) else "🎵"  
    media_type = "Video" if (item.kind == "video" and not item.audio_only) else "Audio"  
    repeat_info = f"\n• 🔁 Remaining plays: {item.repeat_count}" if item.repeat_count > 1 else ""  

    text = f"{icon} <b>Now Playing</b>\n\n• {item.title}\n• {media_type}{repeat_info}"  
    await _send_status_message(chat_id=chat_id, text=text, reply_markup=_buttons(chat_id), thread_id=item.thread_id)

async def _start_current(chat_id: int) -> bool:
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
        await _ensure_engine()  

        if item.source_path and Path(item.source_path).exists():  
            source_path = item.source_path  
        elif item.temp_path and Path(item.temp_path).exists():  
            source_path = item.temp_path  
        else:  
            try:  
                source_path = await _download_item(item)  
                item.source_path = source_path  
                item.fail_count = 0  
            except Exception as download_err:  
                item.fail_count += 1  
                log.error("MEDIA DOWNLOAD ERROR: %r (fail_count=%d)", download_err, item.fail_count)  
                
                if item.fail_count >= 3:  
                    failed = s.current  
                    s.current = None  
                    s.starting = False  
                    await _cleanup_item(failed)  
                    if BOT_INSTANCE:  
                        await BOT_INSTANCE.send_message(chat_id=chat_id, text=f"❌ فشل تنزيل المقطع وتم تخطيه: {item.title}")  
                    if s.queue:  
                        return await _start_current(chat_id)  
                    return False  
                raise download_err  

        path = item.temp_path if (item.temp_path and Path(item.temp_path).exists() and item.temp_path != item.source_path) else await _prepare_playable_path(item, source_path)  
        item.temp_path = path  

        stream = _build_stream(item, path)  
        result = CALLS.play(chat_id, stream)  
        if inspect.isawaitable(result):  
            await asyncio.wait_for(result, PLAY_TIMEOUT)  

        s.paused = False  
        s.starting = False  
        s.ignore_end_until = asyncio.get_running_loop().time() + 2.5  

        await _send_now_playing(chat_id, item)  
        return True  

    except Exception as exc:  
        log.exception("MEDIA START FAILED: %r", exc)  
        s.starting = False  
        failed = s.current  
        s.current = None  
        await _delete_status_messages(chat_id)  
        if failed:  
            await _cleanup_item(failed)  
        await _safe_leave(chat_id)  

        if s.queue:  
            return await _start_current(chat_id)  
        return False

# =========================================================
# ITEM IDENTITY & STREAM END
# =========================================================

def _item_key(item: Optional[MediaItem]) -> Optional[str]:
    if not item:
        return None
    if item.url:  
        return f"url:{item.url}:{item.audio_only}"  
    if item.tts_text:  
        return f"tts:{item.chat_id}:{item.message_id}"  
    return f"telegram:{item.chat_id}:{item.message_id}:{item.audio_only}"

async def _handle_stream_end(chat_id: int):
    async with _lock(chat_id):
        s = _state(chat_id)  
        loop = asyncio.get_running_loop()  
        now = loop.time()  

        if s.suppress_end_until > now or s.ignore_end_until > now or s.ending:  
            return  

        finished = s.current  
        if not finished:  
            return  

        s.ending = True  
        try:  
            await _delete_status_messages(chat_id)  

            if finished.repeat_count > 1:  
                finished.repeat_count -= 1  
                s.current = finished  
                s.paused = False  
                s.starting = False  
                await asyncio.sleep(0.25)  
                if not await _start_current(chat_id):  
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
                if not await _start_current(chat_id):  
                    s.current = None  
                    if not s.queue:  
                        await _safe_leave(chat_id)  
                return  

            await _safe_leave(chat_id)  
        finally:  
            s.ending = False

# =========================================================
# SEEK (FIXED)
# =========================================================

async def _seek_current(chat_id: int, seconds: int):
    s = _state(chat_id)
    if not s.current:  
        return False, "ℹ️ لا يوجد تشغيل."  
    if s.paused:  
        return False, "ℹ️ أوقف الإيقاف المؤقت أولًا."  

    item = s.current  

    try:  
        await _ensure_engine()  
        current_time = CALLS.time(chat_id)  
        if inspect.isawaitable(current_time):  
            current_time = await current_time  

        try:  
            current_time = float(current_time)  
        except Exception:  
            current_time = 0.0  

        target = max(0, int(current_time) + seconds)  

        source_path = item.temp_path if (item.temp_path and Path(item.temp_path).exists()) else item.source_path  
        if not source_path or not Path(source_path).exists():  
            source_path = await _download_item(item)  
            item.source_path = source_path  
            source_path = await _prepare_playable_path(item, source_path)  
            item.temp_path = source_path  

        seek_ext = "mp4" if (item.kind == "video" and not item.audio_only) else "mp3"  
        seek_path = TEMP_ROOT / f"seek_{chat_id}_{item.message_id}_{random.randint(100000, 999999)}.{seek_ext}"  

        command = ["ffmpeg", "-y", "-ss", str(target), "-i", source_path]  
        if seek_ext == "mp4":  
            command += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(seek_path)]  
        else:  
            command += ["-vn", "-c:a", "libmp3lame", "-q:a", "4", str(seek_path)]  

        await _run_ffmpeg(command, 120)  

        old_temp = item.temp_path  
        await _delete_status_messages(chat_id)  

        s.suppress_end_until = asyncio.get_running_loop().time() + 8  
        s.ignore_end_until = asyncio.get_running_loop().time() + 8  

        item.temp_path = str(seek_path)  
        stream = _build_stream(item, str(seek_path))  
        
        result = CALLS.play(chat_id, stream)  
        if inspect.isawaitable(result):  
            await asyncio.wait_for(result, PLAY_TIMEOUT)  

        if old_temp and old_temp != item.source_path and old_temp != str(seek_path):  
            await _cleanup_path(old_temp)  

        s.paused = False  
        await _send_now_playing(chat_id, item)  

        return True, f"⏱ تم الانتقال إلى {target} ثانية."  
    except Exception as exc:  
        log.exception("MEDIA SEEK FAILED: %r", exc)  
        return False, "❌ تعذر تغيير موضع التشغيل."

# =========================================================
# ENQUEUE & HANDLERS
# =========================================================

async def _enqueue(update: Update, context, mode="auto"):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:  
        return  

    source = _source_message(update)  
    if source is None:  
        await message.reply_text("🎵 رد على صوت أو 🎬 فيديو أو 📝 نص ثم اكتب /play")  
        return  

    play_count = _extract_play_count(update, context)  
    url = _extract_url(source)  

    if url:  
        item = MediaItem(chat_id=message.chat.id, message_id=source.message_id, title="YouTube", kind="video", requester_id=user.id, audio_only=(mode == "audio"), url=url, repeat_count=play_count)  
    elif mode == "auto" and getattr(source, "text", None):  
        text = source.text.strip()  
        if not text:  
            await message.reply_text("❌ النص فارغ.")  
            return  
        item = MediaItem(chat_id=message.chat.id, message_id=source.message_id, title=f"Reading: {text[:60]}...", kind="audio", requester_id=user.id, audio_only=True, tts_text=text, repeat_count=play_count)  
    else:  
        media = _media_from_message(source)  
        if media is None:  
            await message.reply_text("🎵 رد على صوت أو 🎬 فيديو أو 📝 نص ثم اكتب /play")  
            return  
        kind, title = media  
        item = MediaItem(chat_id=message.chat.id, message_id=source.message_id, title=title, kind=kind, requester_id=user.id, audio_only=(mode == "audio"), repeat_count=play_count)  

    chat_id = message.chat.id  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        if len(s.queue) >= MAX_QUEUE:  
            await message.reply_text(f"❌ Queue is full. Maximum: {MAX_QUEUE}.")  
            return  

        if s.current is None and not s.starting:  
            s.current = item  
            await _send_now_playing(chat_id, item)  
            if not await _start_current(chat_id):  
                await _delete_status_messages(chat_id)  
                await message.reply_text("❌ تعذر تشغيل الوسائط.")  
            return  

        s.queue.append(item)  
        await _send_status_message(chat_id=chat_id, text=f"➕ Added to queue:\n<b>{item.title}</b>\n📍 Position: {len(s.queue)}")

async def play_handler(update, context): await _enqueue(update, context, "auto")
async def audio_handler(update, context): await _enqueue(update, context, "audio")
async def video_handler(update, context): await _enqueue(update, context, "video")

async def pause_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    await _delete_status_messages(chat_id)  
    async with _lock(chat_id):  
        try:  
            await _ensure_engine()  
            result = CALLS.pause(chat_id)  
            if inspect.isawaitable(result): await asyncio.wait_for(result, 20)  
            _state(chat_id).paused = True  
            await message.reply_text("⏸ Paused.")  
        except Exception as exc:  
            await message.reply_text(f"❌ Pause failed: {exc}")

async def resume_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    await _delete_status_messages(chat_id)  
    async with _lock(chat_id):  
        try:  
            await _ensure_engine()  
            result = CALLS.resume(chat_id)  
            if inspect.isawaitable(result): await asyncio.wait_for(result, 20)  
            _state(chat_id).paused = False  
            await message.reply_text("▶️ Resumed.")  
        except Exception as exc:  
            await message.reply_text(f"❌ Resume failed: {exc}")

async def skip_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    await _delete_status_messages(chat_id)  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        if not s.current:  
            await message.reply_text("ℹ️ لا يوجد تشغيل.")  
            return  
        old = s.current  
        s.suppress_end_until = asyncio.get_running_loop().time() + 6  
        s.ignore_end_until = asyncio.get_running_loop().time() + 6  
        await _safe_leave(chat_id)  
        await _cleanup_item(old)  
        s.current = None  
        s.paused = False  
        s.starting = False  
        s.ending = False  

        if s.queue and await _start_current(chat_id):  
            await message.reply_text("⏭ Skipped.")  
            return  
        await message.reply_text("⏭ Skipped. Queue is empty.")

async def stop_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    
    await _delete_status_messages(chat_id)  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        s.suppress_end_until = asyncio.get_running_loop().time() + 8  
        s.ignore_end_until = asyncio.get_running_loop().time() + 8  
        await _safe_leave(chat_id)  

        if s.current: await _cleanup_item(s.current)  
        for item in s.queue: await _cleanup_item(item)  

        s.queue.clear()  
        s.current = None  
        s.paused = False  
        s.starting = False  
        s.ending = False  
        await message.reply_text("⏹ Playback stopped and queue cleared.")

async def leave_handler(update, context): await stop_handler(update, context)
async def finish_handler(update, context): await stop_handler(update, context)

async def queue_handler(update, context):
    message = update.effective_message
    if message: await message.reply_text(_format_queue(_state(message.chat.id)))

async def now_handler(update, context):
    message = update.effective_message
    if not message: return  
    s = _state(message.chat.id)  
    if not s.current:  
        await message.reply_text("ℹ️ Nothing is playing.")  
        return  
    status = "⏸ Paused" if s.paused else "▶️ Playing"  
    await message.reply_text(f"🎵 {status}\n\n• {s.current.title}\n• 🔁 Plays remaining: {s.current.repeat_count}\n• Queue: {len(s.queue)}")

async def clear_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        count = len(s.queue)  
        for item in s.queue: await _cleanup_item(item)  
        s.queue.clear()  
        await message.reply_text(f"🧹 Cleared {count} queued item(s).")

async def shuffle_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        if len(s.queue) < 2:  
            await message.reply_text("ℹ️ Not enough items.")  
            return  
        items = list(s.queue)  
        random.shuffle(items)  
        s.queue = deque(items)  
        await message.reply_text("🔀 Queue shuffled.")

async def repeat_add_handler(update, context):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    async with _lock(chat_id):  
        s = _state(chat_id)  
        if not s.current:  
            await message.reply_text("ℹ️ لا يوجد مقطع حالي.")  
            return  
        s.current.repeat_count = min(MAX_REPEAT_COUNT, s.current.repeat_count + 1)  
        await message.reply_text(f"🔁 تمت إضافة تشغيل واحد.\n📌 الإجمالي: {s.current.repeat_count}")

async def _seek_handler(update, context, seconds: int):
    if not await _require_admin(update, context): return  
    message = update.effective_message  
    if not message: return  
    chat_id = message.chat.id  
    await _delete_status_messages(chat_id)  
    async with _lock(chat_id):  
        _, text = await _seek_current(chat_id, seconds)  
        await message.reply_text(text)

async def back10_handler(update, context): await _seek_handler(update, context, -10)
async def forward10_handler(update, context): await _seek_handler(update, context, 10)

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
            await query.answer("Admins only.", show_alert=True) 
        except Exception: 
            pass  
        return  

    action = parts[2]  
    if action == "hide":  
        try: 
            await query.edit_message_reply_markup(reply_markup=None) 
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
# SHUTDOWN & REGISTER
# =========================================================

async def shutdown_media_player():
    global USER_CLIENT, CALLS, ENGINE_READY, EVENTS_REGISTERED, BOT_INSTANCE
    for chat_id in list(PLAYERS):  
        try:  
            async with _lock(chat_id):  
                s = PLAYERS[chat_id]  
                await _delete_status_messages(chat_id)  
                if CALLS: await _safe_leave(chat_id)  
                if s.current: await _cleanup_item(s.current)  
                for item in s.queue: await _cleanup_item(item)  
        except Exception:  
            pass  
    PLAYERS.clear()  
    CHAT_LOCKS.clear()  
    if CALLS:  
        try:  
            result = CALLS.stop()  
            if inspect.isawaitable(result): await asyncio.wait_for(result, 20)  
        except Exception:  
            pass  
    CALLS = None  
    if USER_CLIENT:  
        try:  
            if getattr(USER_CLIENT, "is_connected", False): await asyncio.wait_for(USER_CLIENT.stop(), 20)  
        except Exception:  
            pass  
    USER_CLIENT = None  
    ENGINE_READY = False  
    EVENTS_REGISTERED = False  
    BOT_INSTANCE = None

async def _post_shutdown(application): await shutdown_media_player()

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
    application.add_handler(CommandHandler("finish", finish_handler))  
    application.add_handler(CommandHandler("إنهاء", finish_handler))  
    application.add_handler(CommandHandler("انهاء", finish_handler))  
    application.add_handler(CommandHandler("queue", queue_handler))  
    application.add_handler(CommandHandler("now", now_handler))  
    application.add_handler(CommandHandler("clear", clear_handler))  
    application.add_handler(CommandHandler("shuffle", shuffle_handler))  

    application.add_handler(CallbackQueryHandler(_callback_handler, pattern=r"^mp:"))  

    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(?:شغل|play)(?:\s+[0-9٠-٩۰-۹]+)?$"), play_handler))  
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(?:شغل صوت|شغل صوتي|play audio)(?:\s+[0-9٠-٩۰-۹]+)?$"), audio_handler))  
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(?:شغل فيديو|play video|فيديو|video)(?:\s+[0-9٠-٩۰-۹]+)?$"), video_handler))  

    application.post_shutdown = _post_shutdown  
    log.info("MEDIA PLAY: handlers registered successfully.")
