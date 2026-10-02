"""
FixMyEnglish Media Player
Audio + Video + TTS Voice Chat player.

Uses:
- python-telegram-bot 22.8
- Pyrogram user session
- PyTgCalls 3.x
- FFmpeg
- edge-tts

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
import tempfile
import subprocess
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


# ============================================================
# ENVIRONMENT
# ============================================================

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


# ============================================================
# SETTINGS
# ============================================================

MAX_QUEUE = 50

MESSAGE_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 180
PLAY_TIMEOUT = 60
TTS_TIMEOUT = 60
SEEK_TIMEOUT = 60

TTS_VOICE = "en-US-AriaNeural"

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


# ============================================================
# GLOBAL ENGINE
# ============================================================

USER_CLIENT: Optional[Client] = None
CALLS = None
BOT_INSTANCE = None

ENGINE_LOCK = asyncio.Lock()

ENGINE_READY = False
EVENTS_REGISTERED = False


# ============================================================
# PLAYER STATE
# ============================================================

PLAYERS: dict[int, "PlayerState"] = {}
CHAT_LOCKS: dict[int, asyncio.Lock] = {}


@dataclass
class MediaItem:
    chat_id: int
    message_id: int
    title: str
    kind: str
    requester_id: int

    # Original downloaded/generated file.
    temp_path: Optional[str] = None

    # Temporary file currently being played.
    # Used for ±10 second seeking.
    play_path: Optional[str] = None

    thread_id: Optional[int] = None

    # True = video played as audio only.
    audio_only: bool = False

    # If this exists, this item is TTS.
    tts_text: Optional[str] = None


@dataclass
class PlayerState:
    queue: deque

    current: Optional[MediaItem] = None

    paused: bool = False
    starting: bool = False

    suppress_end_until: float = 0.0

    repeat: str = "off"
    # off / one / all

    # Absolute position in the original media.
    # Used by ±10 seek.
    current_offset: float = 0.0


# ============================================================
# STATE HELPERS
# ============================================================

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


# ============================================================
# MEDIA DETECTION
# ============================================================

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
        return (
            "audio",
            "Voice message",
        )

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


def _source_message(update: Update):
    message = update.effective_message

    if not message:
        return None

    return message.reply_to_message


# ============================================================
# ADMIN
# ============================================================

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


# ============================================================
# CONTROL BUTTONS
# ============================================================

def _buttons(chat_id: int):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏪ -10",
                callback_data=f"mp:{chat_id}:back10",
            ),
            InlineKeyboardButton(
                "⏸ Pause",
                callback_data=f"mp:{chat_id}:pause",
            ),
            InlineKeyboardButton(
                "▶️ Resume",
                callback_data=f"mp:{chat_id}:resume",
            ),
            InlineKeyboardButton(
                "⏩ +10",
                callback_data=f"mp:{chat_id}:forward10",
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
                "🔁 Repeat",
                callback_data=f"mp:{chat_id}:repeat",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔴 Hide",
                callback_data=f"mp:{chat_id}:hide",
            ),
        ],
    ])


# ============================================================
# QUEUE DISPLAY
# ============================================================

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
                f"{i}. {icon} {item.title}"
            )

    return (
        "\n".join(lines)
        if lines
        else "📋 Queue is empty."
    )


# ============================================================
# ENGINE
# ============================================================

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
                "Missing API_ID, API_HASH, "
                "or SESSION_STRING."
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


# ============================================================
# PYTGCall EVENTS
# ============================================================

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
    async def _stream_end(
        _,
        update,
    ):

        chat_id = getattr(
            update,
            "chat_id",
            None,
        )

        if chat_id is not None:

            log.info(
                "MEDIA EVENT: stream ended "
                "chat=%s",
                chat_id,
            )

            asyncio.create_task(
                _handle_stream_end(
                    int(chat_id)
                )
            )

    EVENTS_REGISTERED = True


# ============================================================
# STREAM BUILD
# ============================================================

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
            "MEDIA STREAM: VIDEO "
            "path=%s quality=720p",
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


# ============================================================
# LEAVE CALL
# ============================================================

async def _safe_leave(
    chat_id: int,
):

    if CALLS is None:
        return

    await asyncio.sleep(
        0.5
    )

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
                "MEDIA: assistant left "
                "chat=%s",
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
                    "MEDIA: leave failed "
                    "chat=%s error=%r",
                    chat_id,
                    exc,
                )


# ============================================================
# TTS
# ============================================================

async def _generate_tts(
    text: str,
    output_path: str,
):

    text = " ".join(
        text.split()
    ).strip()

    if not text:
        raise RuntimeError(
            "TTS text is empty."
        )

    if len(text) > 2000:
        text = text[:2000]

    log.info(
        "MEDIA TTS: generating "
        "American pronunciation: %r",
        text,
    )

    communicate = edge_tts.Communicate(
        text,
        TTS_VOICE,
    )

    await asyncio.wait_for(
        communicate.save(
            output_path
        ),
        TTS_TIMEOUT,
    )

    output = Path(
        output_path
    )

    if not output.exists():
        raise RuntimeError(
            "TTS output was not created."
        )

    if output.stat().st_size < 1000:
        raise RuntimeError(
            "TTS output is empty or invalid."
        )

    log.info(
        "MEDIA TTS: created %s",
        output_path,
    )


# ============================================================
# DOWNLOAD / GENERATE MEDIA
# ============================================================

async def _download_item(
    item: MediaItem,
) -> str:

    # --------------------------------------------------------
    # TEXT -> AMERICAN TTS
    # --------------------------------------------------------

    if item.tts_text:

        prefix = (
            f"fixmyenglish_tts_"
            f"{item.chat_id}_"
            f"{item.message_id}_"
        )

        path = (
            TEMP_ROOT
            / (
                prefix
                f"{random.randint(1000, 999999)}"
                ".mp3"
            )
        )

        await _generate_tts(
            item.tts_text,
            str(path),
        )

        return str(path)

    # --------------------------------------------------------
    # TELEGRAM MEDIA
    # --------------------------------------------------------

    if USER_CLIENT is None:
        raise RuntimeError(
            "Assistant is not running."
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
            "Source Telegram message "
            "was not found."
        )

    if _media_from_message(
        source
    ) is None:

        raise RuntimeError(
            "Source message no longer "
            "contains playable media."
        )

    prefix = (
        f"fixmyenglish_"
        f"{item.chat_id}_"
        f"{item.message_id}_"
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


# ============================================================
# CLEANUP
# ============================================================

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


async def _cleanup_item_files(
    item: Optional[MediaItem],
):

    if not item:
        return

    paths = []

    if item.temp_path:
        paths.append(
            item.temp_path
        )

    if (
        item.play_path
        and item.play_path
        != item.temp_path
    ):
        paths.append(
            item.play_path
        )

    for path in paths:
        await _cleanup_path(
            path
        )


# ============================================================
# NOW PLAYING MESSAGE
# ============================================================

async def _send_now_playing(
    chat_id: int,
    item: MediaItem,
):

    if BOT_INSTANCE is None:
        return

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

    text = (
        f"{icon} <b>Now Playing</b>\n\n"
        f"• {item.title}\n"
        f"• {media_type}"
    )

    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": _buttons(
            chat_id
        ),
    }

    if item.thread_id:
        kwargs[
            "message_thread_id"
        ] = item.thread_id

    try:

        await BOT_INSTANCE.send_message(
            **kwargs
        )

    except Exception as exc:

        if "Topic_closed" in repr(exc):

            log.info(
                "MEDIA: topic closed; "
                "status message skipped."
            )

        else:

            log.exception(
                "MEDIA: now-playing "
                "message failed."
            )


# ============================================================
# START CURRENT ITEM
# ============================================================

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
            "MEDIA START: chat=%s "
            "title=%r kind=%s",
            chat_id,
            item.title,
            item.kind,
        )

        await _ensure_engine()

        # ----------------------------------------------------
        # Select current playable path.
        # ----------------------------------------------------

        if (
            item.play_path
            and Path(
                item.play_path
            ).exists()
        ):

            path = item.play_path

        elif (
            item.temp_path
            and Path(
                item.temp_path
            ).exists()
        ):

            path = item.temp_path

        else:

            path = await _download_item(
                item
            )

            item.temp_path = path
            item.play_path = None

        stream = _build_stream(
            item,
            path,
        )

        log.info(
            "MEDIA PLAY: chat=%s "
            "path=%s",
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

        await _send_now_playing(
            chat_id,
            item,
        )

        log.info(
            "MEDIA START OK: "
            "chat=%s title=%r",
            chat_id,
            item.title,
        )

        return True

    except asyncio.CancelledError:

        s.starting = False
        raise

    except Exception as exc:

        log.exception(
            "MEDIA START FAILED: "
            "chat=%s error=%r",
            chat_id,
            exc,
        )

        await _safe_leave(
            chat_id
        )

        s.starting = False

        failed = s.current

        s.current = None

        s.current_offset = 0.0

        if failed:
            await _cleanup_item_files(
                failed
            )

        # Do not leave queue stuck.
        if s.queue:

            return await _start_current(
                chat_id
            )

        return False


# ============================================================
# STREAM END
# ============================================================

async def _handle_stream_end(
    chat_id: int,
):

    async with _lock(chat_id):

        s = _state(chat_id)

        now = (
            asyncio
            .get_running_loop()
            .time()
        )

        if (
            s.suppress_end_until
            > now
        ):

            log.info(
                "MEDIA END ignored: "
                "expected stop/skip/seek "
                "chat=%s",
                chat_id,
            )

            return

        finished = s.current

        if finished:

            await _cleanup_item_files(
                finished
            )

        # ----------------------------------------------------
        # Repeat ONE
        # ----------------------------------------------------

        if (
            s.repeat == "one"
            and finished
        ):

            s.current = MediaItem(
                chat_id=finished.chat_id,
                message_id=finished.message_id,
                title=finished.title,
                kind=finished.kind,
                requester_id=finished.requester_id,
                temp_path=finished.temp_path,
                play_path=None,
                thread_id=finished.thread_id,
                audio_only=finished.audio_only,
                tts_text=finished.tts_text,
            )

        else:

            # ------------------------------------------------
            # Repeat ALL
            # ------------------------------------------------

            if (
                s.repeat == "all"
                and finished
            ):

                finished.play_path = None

                s.queue.append(
                    finished
                )

            s.current = None

        s.paused = False
        s.starting = False
        s.current_offset = 0.0

        if (
            s.queue
            or s.current
        ):

            await _start_current(
                chat_id
            )

        else:

            log.info(
                "MEDIA END: queue empty; "
                "leaving chat=%s",
                chat_id,
            )

            await _safe_leave(
                chat_id
            )


# ============================================================
# ENQUEUE
# ============================================================

async def _enqueue(
    update: Update,
    context,
    mode="auto",
):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    source = _source_message(
        update
    )

    if source is None:

        await message.reply_text(
            "📝 رد على كلمة أو جملة "
            "للنطق، أو رد على صوت/فيديو "
            "ثم اكتب /play"
        )

        return

    media = _media_from_message(
        source
    )

    source_text = (
        getattr(
            source,
            "text",
            None,
        )
        or getattr(
            source,
            "caption",
            None,
        )
        or ""
    )

    source_text = source_text.strip()

    # --------------------------------------------------------
    # No media and no text.
    # --------------------------------------------------------

    if (
        media is None
        and not source_text
    ):

        await message.reply_text(
            "📝 رد على كلمة أو جملة، "
            "أو 🎵 صوت/🎬 فيديو "
            "ثم اكتب /play."
        )

        return

    # --------------------------------------------------------
    # TEXT -> TTS
    # Only /play / شغل.
    # --------------------------------------------------------

    if media is None:

        if mode != "auto":

            await message.reply_text(
                "❌ النطق النصي يستخدم "
                "مع /play أو شغل."
            )

            return

        kind = "audio"

        tts_text = source_text

        title = source_text

        if len(title) > 80:
            title = (
                title[:77]
                + "..."
            )

    # --------------------------------------------------------
    # Telegram media
    # --------------------------------------------------------

    else:

        kind, title = media

        tts_text = None

        if (
            mode == "video"
            and kind != "video"
        ):

            await message.reply_text(
                "❌ /video يجب أن يكون "
                "ردًا على فيديو."
            )

            return

        if (
            mode == "audio"
            and kind != "video"
        ):

            await message.reply_text(
                "❌ /audio هنا مخصص "
                "لتشغيل الفيديو كصوت فقط."
            )

            return

    audio_only = (
        mode == "audio"
    )

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
        temp_path=None,
        play_path=None,
        thread_id=thread_id,
        audio_only=audio_only,
        tts_text=tts_text,
    )

    chat_id = message.chat.id

    async with _lock(chat_id):

        s = _state(chat_id)

        if len(s.queue) >= MAX_QUEUE:

            await message.reply_text(
                f"❌ Queue is full. "
                f"Maximum: {MAX_QUEUE}."
            )

            return

        all_items = list(
            s.queue
        )

        if s.current:
            all_items.insert(
                0,
                s.current,
            )

        # Same Telegram message
        # should not be queued twice.
        if any(
            x.message_id
            == item.message_id
            and x.audio_only
            == item.audio_only
            and x.tts_text
            == item.tts_text
            for x in all_items
        ):

            await message.reply_text(
                "ℹ️ هذا الملف موجود "
                "بالفعل في المشغل."
            )

            return

        idle = (
            s.current is None
            and not s.starting
        )

        # ----------------------------------------------------
        # Start immediately.
        # ----------------------------------------------------

        if idle:

            s.current = item
            s.current_offset = 0.0

            await message.reply_text(
                f"▶️ Starting: "
                f"<b>{title}</b>",
                parse_mode="HTML",
            )

            started = await _start_current(
                chat_id
            )

            if not started:

                await message.reply_text(
                    "❌ تعذر تشغيل الوسائط. "
                    "راجع Railway Logs "
                    "التي تبدأ بـ MEDIA."
                )

            return

        # ----------------------------------------------------
        # Add to queue.
        # ----------------------------------------------------

        s.queue.append(
            item
        )

        await message.reply_text(
            f"➕ Added to queue:\n"
            f"<b>{title}</b>\n"
            f"📍 Position: "
            f"{len(s.queue)}",
            parse_mode="HTML",
        )


# ============================================================
# PLAY COMMANDS
# ============================================================

async def play_handler(
    update,
    context,
):
    await _enqueue(
        update,
        context,
        "auto",
    )


async def audio_handler(
    update,
    context,
):
    await _enqueue(
        update,
        context,
        "audio",
    )


async def video_handler(
    update,
    context,
):
    await _enqueue(
        update,
        context,
        "video",
    )


# ============================================================
# PAUSE
# ============================================================

async def pause_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
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

            result = CALLS.pause(
                chat_id
            )

            if inspect.isawaitable(result):

                await asyncio.wait_for(
                    result,
                    20,
                )

            s.paused = True

            await message.reply_text(
                "⏸ Paused."
            )

        except Exception as exc:

            log.exception(
                "MEDIA PAUSE FAILED"
            )

            await message.reply_text(
                f"❌ Pause failed: {exc}"
            )


# ============================================================
# RESUME
# ============================================================

async def resume_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
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

            result = CALLS.resume(
                chat_id
            )

            if inspect.isawaitable(result):

                await asyncio.wait_for(
                    result,
                    20,
                )

            s.paused = False

            await message.reply_text(
                "▶️ Resumed."
            )

        except Exception as exc:

            log.exception(
                "MEDIA RESUME FAILED"
            )

            await message.reply_text(
                f"❌ Resume failed: {exc}"
            )


# ============================================================
# CREATE SEEK FILE
# ============================================================

async def _make_seek_file(
    item: MediaItem,
    source_path: str,
    seconds: float,
) -> str:

    seconds = max(
        0.0,
        float(seconds),
    )

    source = Path(
        source_path
    )

    suffix = source.suffix

    if not suffix:

        if (
            item.kind == "video"
            and not item.audio_only
        ):
            suffix = ".mp4"
        else:
            suffix = ".mp3"

    output = (
        TEMP_ROOT
        / (
            f"fixmyenglish_seek_"
            f"{item.chat_id}_"
            f"{item.message_id}_"
            f"{random.randint(1000, 999999)}"
            f"{suffix}"
        )
    )

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(seconds),
        "-i",
        source_path,
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]

    log.info(
        "MEDIA SEEK: %.2fs -> %s",
        seconds,
        output,
    )

    process = None

    try:

        process = (
            await asyncio
            .create_subprocess_exec(
                *command,
                stdout=(
                    asyncio
                    .subprocess
                    .DEVNULL
                ),
                stderr=(
                    asyncio
                    .subprocess
                    .PIPE
                ),
            )
        )

        _, stderr = (
            await asyncio.wait_for(
                process.communicate(),
                SEEK_TIMEOUT,
            )
        )

    except asyncio.TimeoutError:

        if process:

            try:
                process.kill()
            except Exception:
                pass

        await _cleanup_path(
            str(output)
        )

        raise RuntimeError(
            "FFmpeg seek operation timed out."
        )

    if process.returncode != 0:

        error = stderr.decode(
            "utf-8",
            errors="replace",
        )

        log.error(
            "MEDIA SEEK FAILED: %s",
            error[-3000:],
        )

        await _cleanup_path(
            str(output)
        )

        raise RuntimeError(
            "FFmpeg seek failed."
        )

    if not output.exists():

        raise RuntimeError(
            "Seek output was not created."
        )

    if output.stat().st_size < 1000:

        await _cleanup_path(
            str(output)
        )

        raise RuntimeError(
            "Seek output is empty."
        )

    return str(output)


# ============================================================
# SEEK ±10
# ============================================================

async def _seek_handler(
    update: Update,
    context,
    amount: int,
):

    if not await _require_admin(
        update,
        context,
    ):
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

        if CALLS is None:

            await message.reply_text(
                "❌ المشغل غير جاهز."
            )

            return

        try:

            # ------------------------------------------------
            # Get current position inside current stream.
            # ------------------------------------------------

            result = CALLS.time(
                chat_id
            )

            if inspect.isawaitable(result):

                current_time = (
                    await asyncio.wait_for(
                        result,
                        10,
                    )
                )

            else:
                current_time = result

            try:
                current_time = float(
                    current_time
                )
            except (
                TypeError,
                ValueError,
            ):
                current_time = 0.0

            absolute_position = (
                s.current_offset
                + current_time
            )

            target = max(
                0.0,
                absolute_position
                + amount,
            )

            # ------------------------------------------------
            # Can't go further back than zero.
            # ------------------------------------------------

            if (
                amount < 0
                and target
                >= absolute_position
            ):

                await message.reply_text(
                    "ℹ️ أنت في بداية الملف."
                )

                return

            source_path = (
                s.current.temp_path
            )

            if not source_path:

                await message.reply_text(
                    "❌ مصدر الوسائط غير موجود."
                )

                return

            old_play_path = (
                s.current.play_path
            )

            # ------------------------------------------------
            # Prevent stream_end event from
            # advancing the queue.
            # ------------------------------------------------

            s.suppress_end_until = (
                asyncio
                .get_running_loop()
                .time()
                + 10
            )

            await _safe_leave(
                chat_id
            )

            # ------------------------------------------------
            # Create new file from target.
            # ------------------------------------------------

            new_path = (
                await _make_seek_file(
                    s.current,
                    source_path,
                    target,
                )
            )

            if old_play_path:

                await _cleanup_path(
                    old_play_path
                )

            s.current.play_path = (
                new_path
            )

            s.current_offset = (
                target
            )

            s.paused = False
            s.starting = True

            stream = _build_stream(
                s.current,
                new_path,
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

            s.starting = False

            direction = (
                "⏩ +10"
                if amount > 0
                else "⏪ -10"
            )

            await message.reply_text(
                f"{direction} — "
                f"{int(target)}s"
            )

        except Exception as exc:

            s.starting = False

            log.exception(
                "MEDIA SEEK FAILED: "
                "chat=%s",
                chat_id,
            )

            await message.reply_text(
                f"❌ Seek failed: {exc}"
            )


async def back10_handler(
    update,
    context,
):

    await _seek_handler(
        update,
        context,
        -10,
    )


async def forward10_handler(
    update,
    context,
):

    await _seek_handler(
        update,
        context,
        10,
    )


# ============================================================
# SKIP
# ============================================================

async def skip_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
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
            asyncio
            .get_running_loop()
            .time()
            + 8
        )

        await _safe_leave(
            chat_id
        )

        await _cleanup_item_files(
            old
        )

        s.current = None
        s.paused = False
        s.starting = False
        s.current_offset = 0.0

        if s.queue:

            started = (
                await _start_current(
                    chat_id
                )
            )

            if started:

                await message.reply_text(
                    "⏭ Skipped."
                )

                return

        await message.reply_text(
            "⏭ Skipped. Queue is empty."
        )


# ============================================================
# STOP
# ============================================================

async def stop_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
        return

    message = update.effective_message

    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):

        s = _state(chat_id)

        s.suppress_end_until = (
            asyncio
            .get_running_loop()
            .time()
            + 8
        )

        await _safe_leave(
            chat_id
        )

        if s.current:

            await _cleanup_item_files(
                s.current
            )

        for item in s.queue:

            await _cleanup_item_files(
                item
            )

        s.queue.clear()

        s.current = None
        s.paused = False
        s.starting = False
        s.current_offset = 0.0

        await message.reply_text(
            "⏹ Playback stopped "
            "and queue cleared."
        )


# ============================================================
# LEAVE
# ============================================================

async def leave_handler(
    update,
    context,
):

    await stop_handler(
        update,
        context,
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_handler(
    update,
    context,
):

    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        _format_queue(
            _state(
                message.chat.id
            )
        )
    )


# ============================================================
# NOW
# ============================================================

async def now_handler(
    update,
    context,
):

    message = update.effective_message

    if not message:
        return

    s = _state(
        message.chat.id
    )

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
        f"• Repeat: {s.repeat}\n"
        f"• Queue: {len(s.queue)}"
    )


# ============================================================
# CLEAR
# ============================================================

async def clear_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
        return

    message = update.effective_message

    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):

        s = _state(chat_id)

        count = len(
            s.queue
        )

        for item in s.queue:

            await _cleanup_item_files(
                item
            )

        s.queue.clear()

        await message.reply_text(
            f"🧹 Cleared "
            f"{count} queued item(s)."
        )


# ============================================================
# SHUFFLE
# ============================================================

async def shuffle_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
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

        items = list(
            s.queue
        )

        random.shuffle(
            items
        )

        s.queue = deque(
            items
        )

        await message.reply_text(
            "🔀 Queue shuffled."
        )


# ============================================================
# REPEAT
# ============================================================

async def repeat_handler(
    update,
    context,
):

    if not await _require_admin(
        update,
        context,
    ):
        return

    message = update.effective_message

    if not message:
        return

    chat_id = message.chat.id

    args = [
        x.lower()
        for x in (
            context.args
            or []
        )
    ]

    async with _lock(chat_id):

        s = _state(chat_id)

        if args and args[0] in {
            "off",
            "one",
            "all",
        }:

            s.repeat = args[0]

        else:

            s.repeat = {
                "off": "one",
                "one": "all",
                "all": "off",
            }[
                s.repeat
            ]

        await message.reply_text(
            f"🔁 Repeat: "
            f"{s.repeat}"
        )


# ============================================================
# CALLBACK BUTTONS
# ============================================================

async def _callback_handler(
    update,
    context,
):

    query = update.callback_query

    if not query:
        return

    try:

        await query.answer()

    except Exception:
        pass

    parts = (
        query.data or ""
    ).split(":")

    if (
        len(parts) != 3
        or parts[0] != "mp"
    ):
        return

    if not await _is_admin(
        update,
        context,
    ):

        try:

            await query.answer(
                "Admins only.",
                show_alert=True,
            )

        except Exception:
            pass

        return

    action = parts[2]

    # --------------------------------------------------------
    # Hide
    # --------------------------------------------------------

    if action == "hide":

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------

    handlers = {
        "pause": pause_handler,
        "resume": resume_handler,
        "back10": back10_handler,
        "forward10": forward10_handler,
        "skip": skip_handler,
        "stop": stop_handler,
        "queue": queue_handler,
        "repeat": repeat_handler,
    }

    handler = handlers.get(
        action
    )

    if handler:

        await handler(
            update,
            context,
        )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown_media_player():

    global USER_CLIENT
    global CALLS
    global ENGINE_READY
    global EVENTS_REGISTERED
    global BOT_INSTANCE

    log.info(
        "MEDIA SHUTDOWN: starting..."
    )

    for chat_id in list(
        PLAYERS
    ):

        try:

            async with _lock(
                chat_id
            ):

                s = PLAYERS[
                    chat_id
                ]

                if CALLS:

                    await _safe_leave(
                        chat_id
                    )

                if s.current:

                    await _cleanup_item_files(
                        s.current
                    )

                for item in s.queue:

                    await _cleanup_item_files(
                        item
                    )

        except Exception:

            log.exception(
                "MEDIA SHUTDOWN "
                "failed chat=%s",
                chat_id,
            )

    PLAYERS.clear()
    CHAT_LOCKS.clear()

    # --------------------------------------------------------
    # Stop PyTgCalls
    # --------------------------------------------------------

    if CALLS:

        try:

            result = CALLS.stop()

            if inspect.isawaitable(
                result
            ):

                await asyncio.wait_for(
                    result,
                    20,
                )

        except Exception:

            log.exception(
                "MEDIA SHUTDOWN: "
                "PyTgCalls stop failed."
            )

    CALLS = None

    # --------------------------------------------------------
    # Stop Pyrogram assistant
    # --------------------------------------------------------

    if USER_CLIENT:

        try:

            if (
                getattr(
                    USER_CLIENT,
                    "is_connected",
                    False,
                )
                or getattr(
                    USER_CLIENT,
                    "is_started",
                    False,
                )
            ):

                await asyncio.wait_for(
                    USER_CLIENT.stop(),
                    20,
                )

        except Exception:

            log.exception(
                "MEDIA SHUTDOWN: "
                "assistant stop failed."
            )

    USER_CLIENT = None

    ENGINE_READY = False
    EVENTS_REGISTERED = False
    BOT_INSTANCE = None

    log.info(
        "MEDIA SHUTDOWN: complete."
    )


# ============================================================
# POST SHUTDOWN
# ============================================================

async def _post_shutdown(
    application,
):

    await shutdown_media_player()


# ============================================================
# REGISTER MEDIA PLAYER
# ============================================================

def register_media_play(
    application: Application,
):

    global BOT_INSTANCE

    BOT_INSTANCE = application.bot

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "play",
            play_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "audio",
            audio_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "video",
            video_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "skip",
            skip_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "stop",
            stop_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "leave",
            leave_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "queue",
            queue_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "now",
            now_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            clear_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "shuffle",
            shuffle_handler,
        )
    )

    application.add_handler(
        CommandHandler(
            "repeat",
            repeat_handler,
        )
    )

    # --------------------------------------------------------
    # Callback buttons
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            _callback_handler,
            pattern=r"^mp:",
        )
    )

    # --------------------------------------------------------
    # Plain text commands
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex(
                r"^(?:شغل|play)$"
            ),
            play_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex(
                r"^(?:شغل صوت|شغل صوتي|play audio)$"
            ),
            audio_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & filters.Regex(
                r"^(?:شغل فيديو|play video|فيديو|video)$"
            ),
            video_handler,
        )
    )

    application.post_shutdown = (
        _post_shutdown
    )

    log.info(
        "MEDIA PLAY: handlers registered."
            )
