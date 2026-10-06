"""
FixMyEnglish Media Player
Audio + Video Voice Chat player.

Uses:
• python-telegram-bot 22.8
• Pyrogram user session
• PyTgCalls 3.x
• FFmpeg

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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pyrogram import Client

try:
    import yt_dlp
except Exception:
    yt_dlp = None

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
SEEK_STEP = 10

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
    message_id: int
    title: str
    kind: str
    requester_id: int
    temp_path: Optional[str] = None
    thread_id: Optional[int] = None
    audio_only: bool = False
    start_position: float = 0.0
    source_url: Optional[str] = None


@dataclass
class PlayerState:
    queue: deque
    current: Optional[MediaItem] = None
    paused: bool = False
    starting: bool = False
    suppress_end_until: float = 0.0
    repeat: str = "off"
    position: float = 0.0
    started_at: Optional[float] = None
    status_messages: set[int] = field(default_factory=set)


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

    return message.reply_to_message


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
        member = await context.bot.get_chat_member(
            message.chat.id,
            user.id,
        )

        return member.status in ADMIN_STATUSES

    except Exception:
        log.exception("Admin check failed.")
        return False


async def _require_admin(update, context) -> bool:
    if await _is_admin(update, context):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "❌ هذا الأمر متاح للمشرفين فقط."
        )

    return False


def _buttons(chat_id: int):
    return InlineKeyboardMarkup(
        [
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
        ]
    )


def _format_queue(s: PlayerState) -> str:
    lines = []

    if s.current:
        status = "⏸ Paused" if s.paused else "▶️ Playing"

        icon = (
            "🎬"
            if s.current.kind == "video" and not s.current.audio_only
            else "🎵"
        )

        lines.append(
            f"{icon} {status}: {s.current.title}"
        )

    if s.queue:
        lines.append("")
        lines.append("📋 Queue:")

        for i, item in enumerate(s.queue, 1):
            icon = (
                "🎬"
                if item.kind == "video" and not item.audio_only
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


async def _ensure_engine():
    global USER_CLIENT, CALLS, ENGINE_READY

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

        if not API_ID or not API_HASH or not SESSION_STRING:
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

        if shutil.which("ffprobe") is None:
            raise RuntimeError(
                "FFprobe is not installed."
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

        if not getattr(USER_CLIENT, "is_connected", False):
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
            CALLS = PyTgCalls(USER_CLIENT)

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

    if CALLS is None or call_filters is None:
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


def _build_stream(
    item: MediaItem,
    path: str,
    start_position: float = 0.0,
):
    if not Path(path).exists():
        raise FileNotFoundError(path)

    if MediaStream is None:
        raise RuntimeError(
            "MediaStream is unavailable."
        )

    ffmpeg_parameters = None

    if start_position > 0.05:
        ffmpeg_parameters = (
            f"-ss {max(0.0, start_position):.3f}"
        )

    if (
        item.kind == "video"
        and not item.audio_only
    ):
        log.info(
            "MEDIA STREAM: VIDEO path=%s quality=720p start=%.2f",
            path,
            start_position,
        )

        return MediaStream(
            path,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
            ffmpeg_parameters=ffmpeg_parameters,
        )

    log.info(
        "MEDIA STREAM: AUDIO path=%s start=%.2f",
        path,
        start_position,
    )

    return MediaStream(
        path,
        audio_parameters=AudioQuality.HIGH,
        ffmpeg_parameters=ffmpeg_parameters,
    )


async def _safe_leave(chat_id: int):
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
                or "not in call" in text.lower()
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


def _youtube_url(value: str) -> bool:
    value = (
        value or ""
    ).strip().lower()

    return value.startswith(
        (
            "https://www.youtube.com/",
            "http://www.youtube.com/",
            "https://youtube.com/",
            "http://youtube.com/",
            "https://youtu.be/",
            "http://youtu.be/",
            "https://m.youtube.com/",
            "http://m.youtube.com/",
        )
    )


def _url_from_command(update: Update) -> Optional[str]:
    message = update.effective_message

    if not message:
        return None

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    parts = text.split()

    if len(parts) < 2:
        return None

    candidate = (
        parts[1]
        .strip()
        .strip("<>")
    )

    return (
        candidate
        if _youtube_url(candidate)
        else None
    )


async def _download_youtube(
    item: MediaItem,
) -> str:

    if not item.source_url:
        raise RuntimeError(
            "YouTube URL is missing."
        )

    if yt_dlp is None:
        raise RuntimeError(
            "yt-dlp is not installed. Add yt-dlp to requirements.txt."
        )

    out_dir = (
        TEMP_ROOT / "youtube"
    )

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    prefix = (
        f"yt_{item.chat_id}_"
        f"{item.message_id}_"
        f"{abs(hash(item.source_url)) % 10000000}"
    )

    output = (
        out_dir
        / f"{prefix}.%(ext)s"
    )

    opts = {
        "format": (
            "bestvideo[height<=720]"
            "+bestaudio/"
            "best[height<=720]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(output),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 10) "
                "AppleWebKit/537.36 "
                "Chrome/120 Mobile Safari/537.36"
            )
        },
    }

    def _run():

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                item.source_url,
                download=True,
            )

            title = (
                info.get("title")
                if info
                else None
            )

            if title:
                item.title = str(title)[:200]

            prepared = Path(
                ydl.prepare_filename(info)
            )

            candidates = [
                prepared,
                prepared.with_suffix(".mp4"),
            ]

            for candidate in candidates:

                if candidate.exists():
                    return str(candidate)

            matches = sorted(
                out_dir.glob(
                    f"{prefix}.*"
                ),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )

            for candidate in matches:

                if candidate.is_file():
                    return str(candidate)

            raise RuntimeError(
                "yt-dlp finished but the downloaded video file was not found."
            )

    path = await asyncio.wait_for(
        asyncio.to_thread(_run),
        DOWNLOAD_TIMEOUT,
    )

    log.info(
        "MEDIA YOUTUBE DOWNLOAD: %s",
        path,
    )

    return path


async def _download_item(
    item: MediaItem,
) -> str:

    if item.source_url:
        return await _download_youtube(
            item
        )

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
            "Source Telegram message was not found."
        )

    if _media_from_message(source) is None:
        raise RuntimeError(
            "Source message no longer contains playable media."
        )

    prefix = (
        f"fixmyenglish_"
        f"{item.chat_id}_"
        f"{item.message_id}"
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


async def _delete_status_messages(
    chat_id: int,
):
    """
    Delete only messages created by
    the media player; never delete user media.
    """

    if BOT_INSTANCE is None:
        return

    s = _state(chat_id)

    message_ids = list(
        s.status_messages
    )

    s.status_messages.clear()

    if not message_ids:
        return

    for message_id in message_ids:

        try:
            await BOT_INSTANCE.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )

        except Exception as exc:

            text = repr(exc).lower()

            if (
                "message to delete not found"
                not in text
                and "message_id_invalid"
                not in text
            ):
                log.debug(
                    "MEDIA: could not delete status message %s: %r",
                    message_id,
                    exc,
                )


async def _send_status(
    chat_id: int,
    text: str,
    thread_id: Optional[int] = None,
    markup=None,
):
    if BOT_INSTANCE is None:
        return None

    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    if markup is not None:
        kwargs["reply_markup"] = markup

    if thread_id:
        kwargs[
            "message_thread_id"
        ] = thread_id

    try:
        msg = await BOT_INSTANCE.send_message(
            **kwargs
        )

        _state(chat_id).status_messages.add(
            msg.message_id
        )

        return msg

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


async def _send_now_playing(
    chat_id: int,
    item: MediaItem,
):
    icon = (
        "🎬"
        if item.kind == "video"
        and not item.audio_only
        else "🎵"
    )

    text = (
        f"{icon} <b>Now Playing</b>\n\n"
        f"• {item.title}\n"
        f"• "
        f"{'Video' if item.kind == 'video' and not item.audio_only else 'Audio'}"
    )

    await _send_status(
        chat_id,
        text,
        item.thread_id,
        _buttons(chat_id),
    )


def _current_position(
    s: PlayerState,
) -> float:

    position = s.position

    if (
        s.started_at is not None
        and not s.paused
    ):
        position += max(
            0.0,
            asyncio.get_running_loop().time()
            - s.started_at,
        )

    return max(
        0.0,
        position,
    )


def _reset_position(
    s: PlayerState,
    position: float = 0.0,
):
    s.position = max(
        0.0,
        position,
    )

    s.started_at = (
        asyncio.get_running_loop().time()
    )


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
            "MEDIA START: chat=%s title=%r kind=%s start=%.2f",
            chat_id,
            item.title,
            item.kind,
            item.start_position,
        )

        await _ensure_engine()

        if (
            item.temp_path
            and Path(item.temp_path).exists()
        ):
            path = item.temp_path

        else:
            path = await _download_item(
                item
            )

            item.temp_path = path

        stream = _build_stream(
            item,
            path,
            item.start_position,
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

        _reset_position(
            s,
            item.start_position,
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

        await _safe_leave(
            chat_id
        )

        s.starting = False

        failed = s.current
        s.current = None

        if failed:
            await _cleanup_path(
                failed.temp_path
            )

        if s.queue:
            return await _start_current(
                chat_id
            )

        return False


async def _restart_current_at(
    chat_id: int,
    target: float,
):
    """
    Restart the same media stream
    from an exact FFmpeg seek point.
    """

    s = _state(chat_id)
    item = s.current

    if item is None:
        return (
            False,
            "ℹ️ لا يوجد تشغيل.",
        )

    if s.starting:
        return (
            False,
            "⏳ جارٍ تجهيز الملف...",
        )

    if (
        not item.temp_path
        or not Path(item.temp_path).exists()
    ):
        item.temp_path = (
            await _download_item(item)
        )

    target = max(
        0.0,
        target,
    )

    item.start_position = target

    s.suppress_end_until = (
        asyncio.get_running_loop().time()
        + 4
    )

    s.starting = True

    try:

        await _ensure_engine()

        stream = _build_stream(
            item,
            item.temp_path,
            target,
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

        s.position = target
        s.started_at = (
            asyncio.get_running_loop().time()
        )

        s.paused = False
        s.starting = False

        await _delete_status_messages(
            chat_id
        )

        await _send_now_playing(
            chat_id,
            item,
        )

        log.info(
            "MEDIA SEEK OK: chat=%s target=%.2f",
            chat_id,
            target,
        )

        return (
            True,
            f"⏩ {int(target)}s",
        )

    except Exception as exc:

        s.starting = False

        log.exception(
            "MEDIA SEEK FAILED: chat=%s error=%r",
            chat_id,
            exc,
        )

        return (
            False,
            "❌ تعذر تقديم الصوت 10 ثوانٍ.",
        )


async def _seek_handler(
    update,
    context,
    delta: int,
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

        current = _current_position(s)

        target = max(
            0.0,
            current + delta,
        )

        ok, text = await _restart_current_at(
            chat_id,
            target,
        )

        if ok:
            await _send_status(
                chat_id,
                text,
                (
                    s.current.thread_id
                    if s.current
                    else None
                ),
            )

        else:
            await message.reply_text(
                text
            )


async def _handle_stream_end(
    chat_id: int,
):
    async with _lock(chat_id):

        s = _state(chat_id)

        now = (
            asyncio.get_running_loop().time()
        )

        if s.suppress_end_until > now:
            log.info(
                "MEDIA END ignored: expected stop/skip/seek chat=%s",
                chat_id,
            )
            return

        finished = s.current

        await _delete_status_messages(
            chat_id
        )

        if finished:
            await _cleanup_path(
                finished.temp_path
            )

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
                thread_id=finished.thread_id,
                audio_only=finished.audio_only,
                source_url=finished.source_url,
            )

        else:

            if (
                s.repeat == "all"
                and finished
            ):
                finished.temp_path = None
                finished.start_position = 0.0
                s.queue.append(
                    finished
                )

            s.current = None

        s.paused = False
        s.starting = False
        s.position = 0.0
        s.started_at = None

        if s.queue or s.current:

            await _start_current(
                chat_id
            )

        else:

            log.info(
                "MEDIA END: queue empty; leaving chat=%s",
                chat_id,
            )

            await _safe_leave(
                chat_id
            )

            await _delete_status_messages(
                chat_id
            )


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

    media = _media_from_message(
        source
    )

    source_url = _url_from_command(
        update
    )

    if source_url:

        if mode == "audio":
            await message.reply_text(
                "❌ /audio بالرابط غير مخصص حاليًا. "
                "استعمل /video أو /play."
            )
            return

        kind, title = (
            "video",
            "YouTube video",
        )

        source_message_id = (
            message.message_id
        )

    else:

        if (
            source is None
            or media is None
        ):
            await message.reply_text(
                "🎵 رد على صوت أو 🎬 فيديو، "
                "أو استعمل /play أو /video "
                "مع رابط YouTube."
            )
            return

        kind, title = media

        source_message_id = (
            source.message_id
        )

    if (
        mode == "video"
        and kind != "video"
    ):
        await message.reply_text(
            "❌ /video يجب أن يكون ردًا على فيديو."
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
        message_id=source_message_id,
        title=title,
        kind=kind,
        requester_id=user.id,
        thread_id=thread_id,
        audio_only=audio_only,
        source_url=source_url,
    )

    chat_id = message.chat.id

    async with _lock(chat_id):

        s = _state(chat_id)

        if len(s.queue) >= MAX_QUEUE:
            await message.reply_text(
                f"❌ Queue is full. Maximum: {MAX_QUEUE}."
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

        if any(
            x.message_id == item.message_id
            and x.audio_only == item.audio_only
            and x.source_url == item.source_url
            for x in all_items
        ):
            await message.reply_text(
                "ℹ️ هذا الملف موجود بالفعل في المشغل."
            )
            return

        idle = (
            s.current is None
            and not s.starting
        )

        if idle:

            s.current = item

            await _delete_status_messages(
                chat_id
            )

            await _send_status(
                chat_id,
                f"▶️ Starting: <b>{title}</b>",
                thread_id,
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

        s.queue.append(
            item
        )

        await _send_status(
            chat_id,
            (
                f"➕ Added to queue:\n"
                f"<b>{title}</b>\n"
                f"📍 Position: {len(s.queue)}"
            ),
            thread_id,
        )


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

            if not s.paused:
                s.position = _current_position(
                    s
                )
                s.started_at = None

            result = CALLS.pause(
                chat_id
            )

            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    result,
                    20,
                )

            s.paused = True

            await _delete_status_messages(
                chat_id
            )

            await _send_status(
                chat_id,
                "⏸ Paused.",
                (
                    s.current.thread_id
                    if s.current
                    else None
                ),
            )

        except Exception as exc:

            log.exception(
                "MEDIA PAUSE FAILED"
            )

            await message.reply_text(
                f"❌ Pause failed: {exc}"
            )


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

            s.started_at = (
                asyncio.get_running_loop().time()
            )

            await _delete_status_messages(
                chat_id
            )

            await _send_status(
                chat_id,
                "▶️ Resumed.",
                (
                    s.current.thread_id
                    if s.current
                    else None
                ),
            )

        except Exception as exc:

            log.exception(
                "MEDIA RESUME FAILED"
            )

            await message.reply_text(
                f"❌ Resume failed: {exc}"
            )


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
            asyncio.get_running_loop().time()
            + 4
        )

        await _delete_status_messages(
            chat_id
        )

        await _safe_leave(
            chat_id
        )

        await _cleanup_path(
            old.temp_path
        )

        s.current = None
        s.paused = False
        s.starting = False
        s.position = 0.0
        s.started_at = None

        if s.queue:

            started = await _start_current(
                chat_id
            )

            if started:
                await _send_status(
                    chat_id,
                    "⏭ Skipped.",
                )
                return

        await _send_status(
            chat_id,
            "⏭ Skipped. Queue is empty.",
        )

        await _safe_leave(
            chat_id
        )

        await _delete_status_messages(
            chat_id
        )


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
            asyncio.get_running_loop().time()
            + 4
        )

        await _delete_status_messages(
            chat_id
        )

        await _safe_leave(
            chat_id
        )

        if s.current:
            await _cleanup_path(
                s.current.temp_path
            )

        for item in s.queue:
            await _cleanup_path(
                item.temp_path
            )

        s.queue.clear()
        s.current = None
        s.paused = False
        s.starting = False
        s.position = 0.0
        s.started_at = None

        await _send_status(
            chat_id,
            "⏹ Playback stopped and queue cleared.",
        )

        await _delete_status_messages(
            chat_id
        )


async def leave_handler(
    update,
    context,
):
    await stop_handler(
        update,
        context,
    )


async def queue_handler(
    update,
    context,
):
    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        _format_queue(
            _state(message.chat.id)
        )
    )


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

    position = int(
        _current_position(s)
    )

    await message.reply_text(
        f"🎵 {status}\n\n"
        f"• {s.current.title}\n"
        f"• Position: {position}s\n"
        f"• Repeat: {s.repeat}\n"
        f"• Queue: {len(s.queue)}"
    )


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
            await _cleanup_path(
                item.temp_path
            )

        s.queue.clear()

        await _send_status(
            chat_id,
            f"🧹 Cleared {count} queued item(s).",
        )


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

        await _send_status(
            chat_id,
            "🔀 Queue shuffled.",
        )


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

        if (
            args
            and args[0]
            in {"off", "one", "all"}
        ):
            s.repeat = args[0]

        else:
            s.repeat = {
                "off": "one",
                "one": "all",
                "all": "off",
            }[s.repeat]

        await _send_status(
            chat_id,
            f"🔁 Repeat: {s.repeat}",
        )


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
        query.data
        or ""
    ).split(":")

    if (
        len(parts) != 3
        or parts[0] != "mp"
    ):
        return

    try:
        target_chat_id = int(
            parts[1]
        )

    except ValueError:
        return

    if (
        update.effective_chat
        and update.effective_chat.id
        != target_chat_id
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

    if action == "hide":

        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )

        except Exception:
            pass

        return

    if action == "forward10":
        await _seek_handler(
            update,
            context,
            SEEK_STEP,
        )
        return

    if action == "back10":
        await _seek_handler(
            update,
            context,
            -SEEK_STEP,
        )
        return

    handlers = {
        "pause": pause_handler,
        "resume": resume_handler,
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

                await _delete_status_messages(
                    chat_id
                )

                if CALLS:
                    await _safe_leave(
                        chat_id
                    )

                if s.current:
                    await _cleanup_path(
                        s.current.temp_path
                    )

                for item in s.queue:
                    await _cleanup_path(
                        item.temp_path
                    )

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
                await asyncio.wait_for(
                    result,
                    20,
                )

        except Exception:
            log.exception(
                "MEDIA SHUTDOWN: PyTgCalls stop failed."
            )

    CALLS = None

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
                "MEDIA SHUTDOWN: assistant stop failed."
            )

    USER_CLIENT = None

    ENGINE_READY = False
    EVENTS_REGISTERED = False
    BOT_INSTANCE = None

    log.info(
        "MEDIA SHUTDOWN: complete."
    )


async def _post_shutdown(
    application,
):
    await shutdown_media_player()


def register_media_play(
    application: Application,
):
    global BOT_INSTANCE

    BOT_INSTANCE = application.bot

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

    # لا نستخدم CommandHandler مع العربية.
    # "انهاء" و "إنهاء" تتم معالجتهما كرسالة نصية أدناه.

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
                r"^(?:انهاء|إنهاء)$"
            ),
            leave_handler,
        )
    )

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
