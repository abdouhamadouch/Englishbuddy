"""
FixMyEnglish Media Player (Full Integration)
Audio + Video Voice Chat player with YouTube, full commands & auto-cleanup support.

Uses:
• python-telegram-bot 22.8
• Pyrogram user session
• PyTgCalls 3.x
• FFmpeg
• yt-dlp (for YouTube links)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

log = logging.getLogger("FixMyEnglish.media_play")

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

API_ID = int(API_ID_RAW) if API_ID_RAW else 0
OWNER_ID: Optional[int] = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

MAX_QUEUE = 50
MESSAGE_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 180
PLAY_TIMEOUT = 60

TEMP_ROOT = Path(tempfile.gettempdir()) / "fixmyenglish_media"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

ADMIN_STATUSES = {"administrator", "creator", "owner"}

USER_CLIENT: Optional[Client] = None
CALLS = None
BOT_INSTANCE = None

ENGINE_LOCK = asyncio.Lock()
ENGINE_READY = False
EVENTS_REGISTERED = False

PLAYERS: dict[int, "PlayerState"] = {}
CHAT_LOCKS: dict[int, asyncio.Lock] = {}


@dataclass
class MediaItem:
    chat_id: int
    message_id: Optional[int]
    title: str
    kind: str
    requester_id: int
    temp_path: Optional[str] = None
    thread_id: Optional[int] = None
    audio_only: bool = False
    status_message_id: Optional[int] = None


@dataclass
class PlayerState:
    queue: deque
    current: Optional[MediaItem] = None
    paused: bool = False
    starting: bool = False
    suppress_end_until: float = 0.0
    repeat: str = "off"


def _state(chat_id: int) -> PlayerState:
    if chat_id not in PLAYERS:
        PLAYERS[chat_id] = PlayerState(queue=deque())
    return PLAYERS[chat_id]


def _lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS:
        CHAT_LOCKS[chat_id] = asyncio.Lock()
    return CHAT_LOCKS[chat_id]


def _media_from_message(message):
    if not message:
        return None
    if message.text and ("youtube.com" in message.text or "youtu.be" in message.text):
        return "youtube", message.text.strip()
    if getattr(message, "audio", None):
        return "audio", message.audio.file_name or "Audio"
    if getattr(message, "video", None):
        return "video", message.video.file_name or "Video"
    if getattr(message, "voice", None):
        return "audio", "Voice message"
    document = getattr(message, "document", None)
    if document:
        mime = (document.mime_type or "").lower()
        if mime.startswith("audio/"):
            return "audio", document.file_name or "Audio"
        if mime.startswith("video/"):
            return "video", document.file_name or "Video"
    return None


def _source_message(update: Update):
    message = update.effective_message
    if not message:
        return None
    return message.reply_to_message or message


async def _is_admin(update: Update, context) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    if OWNER_ID and user.id == OWNER_ID:
        return True
    if message.chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(message.chat.id, user.id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


async def _require_admin(update, context) -> bool:
    if await _is_admin(update, context):
        return True
    message = update.effective_message
    if message:
        await message.reply_text("❌ هذا الأمر متاح للمشرفين فقط.")
    return False


def _buttons(chat_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data=f"mp:{chat_id}:pause"),
            InlineKeyboardButton("▶️ Resume", callback_data=f"mp:{chat_id}:resume"),
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data=f"mp:{chat_id}:skip"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"mp:{chat_id}:stop"),
        ],
        [
            InlineKeyboardButton("📋 Queue", callback_data=f"mp:{chat_id}:queue"),
            InlineKeyboardButton("🔁 Repeat", callback_data=f"mp:{chat_id}:repeat"),
        ],
        [
            InlineKeyboardButton("🔴 Hide", callback_data=f"mp:{chat_id}:hide"),
        ],
    ])


def _format_queue(s: PlayerState) -> str:
    lines = []
    if s.current:
        status = "⏸ Paused" if s.paused else "▶️ Playing"
        icon = "🎬" if s.current.kind in ("video", "youtube") and not s.current.audio_only else "🎵"
        lines.append(f"{icon} {status}: {s.current.title}")

    if s.queue:
        lines.append("")
        lines.append("📋 Queue:")
        for i, item in enumerate(s.queue, 1):
            icon = "🎬" if item.kind in ("video", "youtube") and not item.audio_only else "🎵"
            lines.append(f"{i}. {icon} {item.title}")

    return "\n".join(lines) if lines else "📋 Queue is empty."


async def _ensure_engine():
    global USER_CLIENT, CALLS, ENGINE_READY
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
        return

    @CALLS.on_update(call_filters.stream_end())
    async def stream_end(_, update):
        chat_id = getattr(update, "chat_id", None)
        if chat_id is not None:
            asyncio.create_task(_handle_stream_end(int(chat_id)))

    EVENTS_REGISTERED = True


def _build_stream(item: MediaItem, path: str):
    if not Path(path).exists():
        raise FileNotFoundError(path)
    if MediaStream is None:
        raise RuntimeError("MediaStream is unavailable.")
    if item.kind in ("video", "youtube") and not item.audio_only:
        return MediaStream(
            path,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
        )
    return MediaStream(path, audio_parameters=AudioQuality.HIGH)


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
            if "NotInCall" in repr(exc) or "not in call" in repr(exc).lower():
                return
            if attempt == 0:
                await asyncio.sleep(1)


async def _download_item(item: MediaItem) -> str:
    if item.kind == "youtube":
        if not yt_dlp:
            raise RuntimeError("yt_dlp is not installed for YouTube streaming.")
        ydl_opts = {
            'format': 'best[ext=mp4]/best' if not item.audio_only else 'bestaudio/best',
            'outtmpl': str(TEMP_ROOT / f"yt_{item.chat_id}_%(id)s.%(ext)s"),
            'quiet': True,
        }
        def download_yt():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(item.title, download=True)
                return ydl.prepare_filename(info), info.get('title', 'YouTube Video')
        
        path, real_title = await asyncio.to_thread(download_yt)
        item.title = real_title
        return path

    if USER_CLIENT is None:
        raise RuntimeError("Assistant is not running.")
    source = await asyncio.wait_for(USER_CLIENT.get_messages(item.chat_id, item.message_id), MESSAGE_TIMEOUT)
    if not source:
        raise RuntimeError("Source Telegram message was not found.")
    
    prefix = f"fixmyenglish_{item.chat_id}_{item.message_id}"
    path = await asyncio.wait_for(USER_CLIENT.download_media(source, file_name=str(TEMP_ROOT / prefix)), DOWNLOAD_TIMEOUT)
    if not path:
        raise RuntimeError("Telegram media download failed.")
    return str(path)


async def _cleanup_path(path: Optional[str]):
    if not path:
        return
    try:
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)
    except Exception:
        pass


async def _delete_status_message(chat_id: int, item: MediaItem):
    if BOT_INSTANCE and item and item.status_message_id:
        try:
            await BOT_INSTANCE.delete_message(chat_id=chat_id, message_id=item.status_message_id)
        except Exception:
            pass
        item.status_message_id = None


async def _send_now_playing(chat_id: int, item: MediaItem):
    if BOT_INSTANCE is None:
        return
    icon = "🎬" if item.kind in ("video", "youtube") and not item.audio_only else "🎵"
    text = f"{icon} <b>Now Playing</b>\n\n• {item.title}"
    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": _buttons(chat_id),
    }
    if item.thread_id:
        kwargs["message_thread_id"] = item.thread_id
    try:
        msg = await BOT_INSTANCE.send_message(**kwargs)
        item.status_message_id = msg.message_id
    except Exception:
        pass


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
        if item.temp_path and Path(item.temp_path).exists():
            path = item.temp_path
        else:
            path = await _download_item(item)
            item.temp_path = path

        stream = _build_stream(item, path)
        result = CALLS.play(chat_id, stream)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, PLAY_TIMEOUT)

        s.paused = False
        s.starting = False
        await _send_now_playing(chat_id, item)
        return True

    except Exception as exc:
        log.exception("MEDIA START FAILED: chat=%s error=%r", chat_id, exc)
        await _safe_leave(chat_id)
        s.starting = False
        failed = s.current
        s.current = None
        if failed:
            await _delete_status_message(chat_id, failed)
            await _cleanup_path(failed.temp_path)
        if s.queue:
            return await _start_current(chat_id)
        return False


async def _handle_stream_end(chat_id: int):
    async with _lock(chat_id):
        s = _state(chat_id)
        now = asyncio.get_running_loop().time()
        if s.suppress_end_until > now:
            return

        finished = s.current
        if finished:
            await _delete_status_message(chat_id, finished)
            await _cleanup_path(finished.temp_path)

        if s.repeat == "one" and finished:
            s.current = MediaItem(
                chat_id=finished.chat_id,
                message_id=finished.message_id,
                title=finished.title,
                kind=finished.kind,
                requester_id=finished.requester_id,
                thread_id=finished.thread_id,
                audio_only=finished.audio_only,
            )
        else:
            if s.repeat == "all" and finished:
                finished.temp_path = None
                s.queue.append(finished)
            s.current = None

        s.paused = False
        s.starting = False

        if s.queue or s.current:
            await _start_current(chat_id)
        else:
            await _safe_leave(chat_id)


async def _enqueue(update: Update, context, mode="auto"):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    source = _source_message(update)
    media = _media_from_message(source)

    if source is None or media is None:
        await message.reply_text("🎵 رد على صوت/فيديو أو أرسل رابط يوتيوب ثم اكتب /play")
        return

    kind, title = media
    audio_only = (mode == "audio")
    thread_id = getattr(message, "message_thread_id", None) or getattr(source, "message_thread_id", None)

    item = MediaItem(
        chat_id=message.chat.id,
        message_id=source.message_id if kind != "youtube" else None,
        title=title,
        kind=kind,
        requester_id=user.id,
        thread_id=thread_id,
        audio_only=audio_only,
    )

    chat_id = message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        if len(s.queue) >= MAX_QUEUE:
            await message.reply_text(f"❌ Queue is full. Maximum: {MAX_QUEUE}.")
            return

        idle = (s.current is None and not s.starting)
        if idle:
            s.current = item
            await message.reply_text(f"▶️ Starting: <b>{title}</b>", parse_mode="HTML")
            started = await _start_current(chat_id)
            if not started:
                await message.reply_text("❌ تعذر تشغيل الوسائط. راجع Logs.")
            return

        s.queue.append(item)
        await message.reply_text(f"➕ Added to queue:\n<b>{title}</b>\n📍 Position: {len(s.queue)}", parse_mode="HTML")


async def play_handler(update, context): await _enqueue(update, context, "auto")
async def audio_handler(update, context): await _enqueue(update, context, "audio")
async def video_handler(update, context): await _enqueue(update, context, "video")


async def pause_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        if not s.current:
            await update.effective_message.reply_text("ℹ️ لا يوجد تشغيل.")
            return
        await CALLS.pause(chat_id)
        s.paused = True
        await update.effective_message.reply_text("⏸ Paused.")


async def resume_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        if not s.current:
            await update.effective_message.reply_text("ℹ️ لا يوجد تشغيل.")
            return
        await CALLS.resume(chat_id)
        s.paused = False
        await update.effective_message.reply_text("▶️ Resumed.")


async def skip_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        if not s.current:
            await update.effective_message.reply_text("ℹ️ لا يوجد تشغيل.")
            return
        old = s.current
        s.suppress_end_until = asyncio.get_running_loop().time() + 4
        await _safe_leave(chat_id)
        await _delete_status_message(chat_id, old)
        await _cleanup_path(old.temp_path)
        s.current = None
        s.paused = False
        s.starting = False
        if s.queue:
            started = await _start_current(chat_id)
            if started:
                await update.effective_message.reply_text("⏭ Skipped.")
                return
        await update.effective_message.reply_text("⏭ Skipped. Queue is empty.")


async def stop_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        s.suppress_end_until = asyncio.get_running_loop().time() + 4
        await _safe_leave(chat_id)
        if s.current:
            await _delete_status_message(chat_id, s.current)
            await _cleanup_path(s.current.temp_path)
        for item in s.queue:
            await _cleanup_path(item.temp_path)
        s.queue.clear()
        s.current = None
        s.paused = False
        s.starting = False
        await update.effective_message.reply_text("⏹ Playback stopped and queue cleared.")


async def leave_handler(update, context):
    await stop_handler(update, context)


async def queue_handler(update, context):
    message = update.effective_message
    if message:
        await message.reply_text(_format_queue(_state(message.chat.id)))


async def now_handler(update, context):
    message = update.effective_message
    if not message:
        return
    s = _state(message.chat.id)
    if not s.current:
        await message.reply_text("ℹ️ Nothing is playing.")
        return
    status = "⏸ Paused" if s.paused else "▶️ Playing"
    await message.reply_text(f"🎵 {status}\n\n• {s.current.title}\n• Repeat: {s.repeat}\n• Queue: {len(s.queue)}")


async def clear_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        count = len(s.queue)
        for item in s.queue:
            await _cleanup_path(item.temp_path)
        s.queue.clear()
        await update.effective_message.reply_text(f"🧹 Cleared {count} queued item(s).")


async def shuffle_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    async with _lock(chat_id):
        s = _state(chat_id)
        if len(s.queue) < 2:
            await update.effective_message.reply_text("ℹ️ Not enough items.")
            return
        items = list(s.queue)
        random.shuffle(items)
        s.queue = deque(items)
        await update.effective_message.reply_text("🔀 Queue shuffled.")


async def repeat_handler(update, context):
    if not await _require_admin(update, context): return
    chat_id = update.effective_message.chat.id
    args = [x.lower() for x in (context.args or [])]
    async with _lock(chat_id):
        s = _state(chat_id)
        if args and args[0] in {"off", "one", "all"}:
            s.repeat = args[0]
        else:
            s.repeat = {"off": "one", "one": "all", "all": "off"}[s.repeat]
        await update.effective_message.reply_text(f"🔁 Repeat: {s.repeat}")


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

    handlers = {
        "pause": pause_handler,
        "resume": resume_handler,
        "skip": skip_handler,
        "stop": stop_handler,
        "queue": queue_handler,
        "repeat": repeat_handler,
    }
    handler = handlers.get(action)
    if handler:
        await handler(update, context)


async def shutdown_media_player():
    global USER_CLIENT, CALLS, ENGINE_READY, EVENTS_REGISTERED, BOT_INSTANCE
    for chat_id in list(PLAYERS):
        try:
            async with _lock(chat_id):
                s = PLAYERS[chat_id]
                if CALLS: await _safe_leave(chat_id)
                if s.current:
                    await _delete_status_message(chat_id, s.current)
                    await _cleanup_path(s.current.temp_path)
                for item in s.queue:
                    await _cleanup_path(item.temp_path)
        except Exception:
            pass
    PLAYERS.clear()
    CHAT_LOCKS.clear()
    if CALLS:
        try: await CALLS.stop()
        except Exception: pass
        CALLS = None
    if USER_CLIENT:
        try: await USER_CLIENT.stop()
        except Exception: pass
        USER_CLIENT = None
    ENGINE_READY = False
    EVENTS_REGISTERED = False
    BOT_INSTANCE = None


async def _post_shutdown(application):
    await shutdown_media_player()


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
    application.add_handler(CommandHandler("repeat", repeat_handler))

    application.add_handler(CallbackQueryHandler(_callback_handler, pattern=r"^mp:"))

    application.post_shutdown = _post_shutdown
    log.info("MEDIA PLAY: full handlers registered successfully.")
