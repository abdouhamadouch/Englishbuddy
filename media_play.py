"""
FixMyEnglish - Telegram Voice Chat Media Player
Uses:
- python-telegram-bot for bot commands/buttons
- Pyrogram user session for reading/downloading Telegram media
- PyTgCalls for Telegram voice/video chats

Required environment variables:
    API_ID
    API_HASH
    SESSION_STRING

The bot itself still uses BOT_TOKEN in bot.py.
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
    from pytgcalls.types import StreamEnded
except Exception:
    PyTgCalls = None
    call_filters = None
    StreamEnded = None


log = logging.getLogger("FixMyEnglish.media_play")

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

DOWNLOAD_TIMEOUT = 120
MESSAGE_TIMEOUT = 30
PLAY_TIMEOUT = 60

MAX_QUEUE = 50

ADMIN_STATUSES = {
    "administrator",
    "creator",
    "owner",
}

TEMP_ROOT = Path(tempfile.gettempdir()) / "fixmyenglish_media"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def _int_env(value: str, name: str) -> int:
    try:
        return int(value)
    except Exception as exc:
        raise RuntimeError(f"{name} must be a valid integer.") from exc


if API_ID_RAW:
    API_ID = _int_env(API_ID_RAW, "API_ID")
else:
    API_ID = 0

OWNER_ID: Optional[int] = None
if OWNER_ID_RAW:
    try:
        OWNER_ID = int(OWNER_ID_RAW)
    except ValueError:
        log.warning("OWNER_ID is not a valid integer; owner override disabled.")


USER_CLIENT: Optional[Client] = None
CALLS = None

_ENGINE_LOCK = asyncio.Lock()
_ENGINE_READY = False
_EVENTS_REGISTERED = False


@dataclass
class MediaItem:
    chat_id: int
    message_id: int
    title: str
    kind: str
    requester_id: int
    temp_path: Optional[str] = None


@dataclass
class PlayerState:
    queue: deque
    current: Optional[MediaItem] = None
    paused: bool = False
    muted: bool = False
    volume: int = 100
    repeat: str = "off"  # off / one / all
    starting: bool = False


PLAYERS: dict[int, PlayerState] = {}
CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def _state(chat_id: int) -> PlayerState:
    if chat_id not in PLAYERS:
        PLAYERS[chat_id] = PlayerState(queue=deque())
    return PLAYERS[chat_id]


def _lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CHAT_LOCKS:
        CHAT_LOCKS[chat_id] = asyncio.Lock()
    return CHAT_LOCKS[chat_id]


def _media_from_message(message) -> tuple[str, str] | None:
    """
    Return (kind, title) for Telegram media that PyTgCalls can play.
    """
    if not message:
        return None

    if getattr(message, "audio", None):
        media = message.audio
        return "audio", getattr(media, "file_name", None) or "Audio"

    if getattr(message, "video", None):
        media = message.video
        return "video", getattr(media, "file_name", None) or "Video"

    if getattr(message, "voice", None):
        return "audio", "Voice message"

    document = getattr(message, "document", None)
    if document:
        mime = (getattr(document, "mime_type", None) or "").lower()
        if mime.startswith("audio/"):
            return "audio", getattr(document, "file_name", None) or "Audio"
        if mime.startswith("video/"):
            return "video", getattr(document, "file_name", None) or "Video"

    return None


def _source_message(update: Update):
    message = update.effective_message
    if not message:
        return None

    if message.reply_to_message:
        return message.reply_to_message

    return None


def _title(item: MediaItem) -> str:
    return item.title or item.kind.title()


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return False

    if OWNER_ID is not None and user.id == OWNER_ID:
        return True

    # Private chats do not need an admin check.
    if message.chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(message.chat.id, user.id)
        return member.status in ADMIN_STATUSES
    except Exception:
        log.exception("Admin check failed for user=%s chat=%s", user.id, message.chat.id)
        return False


async def _require_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if await _is_admin(update, context):
        return True

    message = update.effective_message
    if message:
        await message.reply_text("❌ هذا الأمر متاح للمشرفين فقط.")
    return False


def _buttons(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
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
        ]
    )


def _format_queue(state: PlayerState) -> str:
    lines = []

    if state.current:
        status = "⏸ Paused" if state.paused else "▶️ Playing"
        lines.append(f"🎵 {status}: {state.current.title}")

    if state.queue:
        lines.append("")
        lines.append("📋 Queue:")
        for index, item in enumerate(state.queue, 1):
            lines.append(f"{index}. {item.title}")

    if not lines:
        return "📋 Queue is empty."

    return "\n".join(lines)


async def _ensure_engine() -> None:
    global USER_CLIENT, CALLS, _ENGINE_READY

    if _ENGINE_READY and USER_CLIENT is not None and CALLS is not None:
        if getattr(USER_CLIENT, "is_connected", False) or getattr(
            USER_CLIENT, "is_started", False
        ):
            return

    async with _ENGINE_LOCK:
        if _ENGINE_READY and USER_CLIENT is not None and CALLS is not None:
            if getattr(USER_CLIENT, "is_connected", False) or getattr(
                USER_CLIENT, "is_started", False
            ):
                return

        if not API_ID or not API_HASH or not SESSION_STRING:
            raise RuntimeError(
                "Missing API_ID, API_HASH, or SESSION_STRING for the media player."
            )

        if PyTgCalls is None:
            raise RuntimeError(
                "PyTgCalls could not be imported. Check py-tgcalls installation."
            )

        ffmpeg = shutil.which("ffmpeg")
        log.info("MEDIA ENGINE: ffmpeg=%s", ffmpeg or "NOT FOUND")

        try:
            import importlib.metadata as metadata

            pyrogram_version = metadata.version("pyrogram")
            pytgcalls_version = metadata.version("py-tgcalls")
        except Exception:
            pyrogram_version = "unknown"
            pytgcalls_version = "unknown"

        log.info(
            "MEDIA ENGINE: pyrogram=%s pytgcalls=%s",
            pyrogram_version,
            pytgcalls_version,
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
            log.info("MEDIA ENGINE: starting Pyrogram user client...")
            await USER_CLIENT.start()
            log.info("MEDIA ENGINE: Pyrogram user client started.")

        if CALLS is None:
            log.info("MEDIA ENGINE: creating PyTgCalls...")
            CALLS = PyTgCalls(USER_CLIENT)

            log.info("MEDIA ENGINE: starting PyTgCalls...")
            result = CALLS.start()
            if inspect.isawaitable(result):
                await result

            log.info("MEDIA ENGINE: PyTgCalls started.")
            _register_call_events()

        _ENGINE_READY = True


def _register_call_events() -> None:
    global _EVENTS_REGISTERED

    if _EVENTS_REGISTERED:
        return

    if CALLS is None or call_filters is None or StreamEnded is None:
        raise RuntimeError("PyTgCalls event API is unavailable.")

    @CALLS.on_update(call_filters.stream_end())
    async def _stream_end_handler(_, update):
        chat_id = getattr(update, "chat_id", None)
        if chat_id is None:
            return

        log.info("MEDIA EVENT: stream ended chat=%s", chat_id)
        asyncio.create_task(_handle_stream_end(int(chat_id)))

    _EVENTS_REGISTERED = True


async def _safe_leave(chat_id: int) -> None:
    if CALLS is None:
        return

    try:
        result = CALLS.leave_call(chat_id)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, 20)
    except Exception as exc:
        log.warning("MEDIA: leave_call failed chat=%s error=%r", chat_id, exc)


async def _safe_stop_call(chat_id: int) -> None:
    # Current PyTgCalls exposes leave_call as the stop/leave operation.
    await _safe_leave(chat_id)


async def _download_item(item: MediaItem) -> str:
    if USER_CLIENT is None:
        raise RuntimeError("Pyrogram media client is not running.")

    source = await asyncio.wait_for(
        USER_CLIENT.get_messages(item.chat_id, item.message_id),
        timeout=MESSAGE_TIMEOUT,
    )

    if not source:
        raise RuntimeError("The source Telegram message could not be found.")

    if _media_from_message(source) is None:
        raise RuntimeError("The source message no longer contains playable media.")

    prefix = f"fixmyenglish_{item.chat_id}_{item.message_id}_"

    path = await asyncio.wait_for(
        USER_CLIENT.download_media(
            source,
            file_name=str(TEMP_ROOT / prefix),
        ),
        timeout=DOWNLOAD_TIMEOUT,
    )

    if not path:
        raise RuntimeError("Telegram media download returned no file.")

    log.info(
        "MEDIA DOWNLOAD: chat=%s message=%s path=%s",
        item.chat_id,
        item.message_id,
        path,
    )
    return str(path)


async def _cleanup_path(path: Optional[str]) -> None:
    if not path:
        return

    try:
        await asyncio.to_thread(Path(path).unlink, True)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("MEDIA: failed to delete temp file %s", path)


async def _send_now_playing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    item: MediaItem,
) -> None:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🎵 <b>Now Playing</b>\n\n"
                f"• {item.title}\n"
                f"• Type: {item.kind}\n"
                f"• Requested by: {item.requester_id}"
            ),
            parse_mode="HTML",
            reply_markup=_buttons(chat_id),
        )
    except Exception:
        log.exception("MEDIA: failed to send now-playing message.")


async def _start_current(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> bool:
    state = _state(chat_id)

    if state.current is None:
        if not state.queue:
            return False
        state.current = state.queue.popleft()

    if state.starting:
        return False

    state.starting = True
    item = state.current

    try:
        log.info(
            "MEDIA START: chat=%s message=%s title=%r",
            chat_id,
            item.message_id,
            item.title,
        )

        await _ensure_engine()

        if item.temp_path and Path(item.temp_path).exists():
            path = item.temp_path
        else:
            log.info("MEDIA START: downloading chat=%s message=%s", chat_id, item.message_id)
            path = await _download_item(item)
            item.temp_path = path

        log.info("MEDIA START: calling PyTgCalls.play chat=%s path=%s", chat_id, path)

        result = CALLS.play(chat_id, path)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, PLAY_TIMEOUT)

        state.paused = False
        state.muted = False
        state.starting = False

        log.info("MEDIA START: playback started chat=%s title=%r", chat_id, item.title)

        if context is not None:
            await _send_now_playing(context, chat_id, item)

        return True

    except asyncio.TimeoutError:
        log.error(
            "MEDIA START TIMEOUT: chat=%s message=%s",
            chat_id,
            item.message_id,
        )
        await _safe_leave(chat_id)
    except Exception as exc:
        log.exception(
            "MEDIA START FAILED: chat=%s message=%s error=%r",
            chat_id,
            item.message_id,
            exc,
        )
        await _safe_leave(chat_id)

    state.starting = False

    failed = state.current
    state.current = None

    if failed:
        await _cleanup_path(failed.temp_path)

    # If this track failed, try the next queued item instead of getting stuck.
    if state.queue:
        return await _start_current(chat_id, context)

    return False


async def _handle_stream_end(chat_id: int) -> None:
    async with _lock(chat_id):
        state = _state(chat_id)
        finished = state.current

        if finished:
            log.info(
                "MEDIA END: chat=%s title=%r repeat=%s",
                chat_id,
                finished.title,
                state.repeat,
            )

        if finished:
            await _cleanup_path(finished.temp_path)

        if state.repeat == "one" and finished is not None:
            state.current = MediaItem(
                chat_id=finished.chat_id,
                message_id=finished.message_id,
                title=finished.title,
                kind=finished.kind,
                requester_id=finished.requester_id,
            )
        else:
            if state.repeat == "all" and finished is not None:
                state.queue.append(finished)

            state.current = None

        state.paused = False
        state.starting = False

        await _start_current(chat_id, None)


async def _enqueue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    source = _source_message(update)
    media = _media_from_message(source)

    if source is None or media is None:
        await message.reply_text(
            "🎵 Reply to an audio or video message with <b>شغل</b> or <b>/play</b>.",
            parse_mode="HTML",
        )
        return

    state = _state(message.chat.id)

    if len(state.queue) >= MAX_QUEUE:
        await message.reply_text(
            f"❌ Queue is full. Maximum: {MAX_QUEUE} items."
        )
        return

    kind, title = media

    item = MediaItem(
        chat_id=message.chat.id,
        message_id=source.message_id,
        title=title,
        kind=kind,
        requester_id=user.id,
    )

    async with _lock(message.chat.id):
        state = _state(message.chat.id)

        # Prevent accidental duplicate enqueue of the same Telegram message.
        all_items = list(state.queue)
        if state.current:
            all_items.insert(0, state.current)

        if any(x.message_id == item.message_id for x in all_items):
            await message.reply_text("ℹ️ This media is already in the player.")
            return

        was_idle = state.current is None and not state.starting

        if was_idle:
            state.current = item
        else:
            state.queue.append(item)

        position = 0 if was_idle else len(state.queue)

        if was_idle:
            await message.reply_text(
                f"▶️ Starting: <b>{title}</b>",
                parse_mode="HTML",
            )

            started = await _start_current(message.chat.id, context)

            if not started:
                await message.reply_text(
                    "❌ Playback could not be started. Check the Railway logs for "
                    "MEDIA ENGINE / MEDIA DOWNLOAD / MEDIA START."
                )
            return

        await message.reply_text(
            f"➕ Added to queue: <b>{title}</b>\n"
            f"📍 Position: {position}",
            parse_mode="HTML",
        )


async def play_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _enqueue(update, context)


async def play_text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip().lower()

    if text == "شغل" or text == "play":
        await _enqueue(update, context)
    elif text == "فيديو" or text == "video":
        await _enqueue(update, context)


async def pause_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        if state.current is None:
            await message.reply_text("ℹ️ Nothing is playing.")
            return

        try:
            result = CALLS.pause(chat_id)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 20)

            state.paused = True
            await message.reply_text("⏸ Paused.")
        except Exception as exc:
            log.exception("MEDIA PAUSE FAILED: %r", exc)
            await message.reply_text(f"❌ Pause failed: {exc}")


async def resume_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        if state.current is None:
            await message.reply_text("ℹ️ Nothing is playing.")
            return

        try:
            result = CALLS.resume(chat_id)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 20)

            state.paused = False
            await message.reply_text("▶️ Resumed.")
        except Exception as exc:
            log.exception("MEDIA RESUME FAILED: %r", exc)
            await message.reply_text(f"❌ Resume failed: {exc}")


async def skip_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        if state.current is None:
            await message.reply_text("ℹ️ Nothing is playing.")
            return

        old = state.current
        await _safe_stop_call(chat_id)

        await _cleanup_path(old.temp_path)

        state.current = None
        state.paused = False
        state.starting = False

        started = await _start_current(chat_id, context)

        if started:
            await message.reply_text("⏭ Skipped.")
        else:
            await message.reply_text("⏭ Skipped. Queue is empty.")


async def stop_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        await _safe_stop_call(chat_id)

        if state.current:
            await _cleanup_path(state.current.temp_path)

        for item in state.queue:
            await _cleanup_path(item.temp_path)

        state.queue.clear()
        state.current = None
        state.paused = False
        state.starting = False

        await message.reply_text("⏹ Playback stopped and queue cleared.")


async def leave_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        await _safe_leave(chat_id)

        if state.current:
            await _cleanup_path(state.current.temp_path)

        for item in state.queue:
            await _cleanup_path(item.temp_path)

        state.queue.clear()
        state.current = None
        state.paused = False
        state.starting = False

        await message.reply_text("👋 Left the voice chat and cleared the player.")


async def queue_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(_format_queue(_state(message.chat.id)))


async def now_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message:
        return

    state = _state(message.chat.id)

    if not state.current:
        await message.reply_text("ℹ️ Nothing is playing.")
        return

    status = "⏸ Paused" if state.paused else "▶️ Playing"
    await message.reply_text(
        f"🎵 {status}\n\n"
        f"• {state.current.title}\n"
        f"• Volume: {state.volume}%\n"
        f"• Repeat: {state.repeat}\n"
        f"• Queue: {len(state.queue)}"
    )


async def clear_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        count = len(state.queue)
        for item in state.queue:
            await _cleanup_path(item.temp_path)

        state.queue.clear()
        await message.reply_text(f"🧹 Cleared {count} queued item(s).")


async def shuffle_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        state = _state(chat_id)

        if len(state.queue) < 2:
            await message.reply_text("ℹ️ Not enough items to shuffle.")
            return

        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)

        await message.reply_text("🔀 Queue shuffled.")


async def repeat_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id
    args = [x.lower() for x in (context.args or [])]

    async with _lock(chat_id):
        state = _state(chat_id)

        if args and args[0] in {"off", "one", "all"}:
            state.repeat = args[0]
        else:
            state.repeat = {
                "off": "one",
                "one": "all",
                "all": "off",
            }[state.repeat]

        await message.reply_text(f"🔁 Repeat: {state.repeat}")


async def _set_mute(chat_id: int, muted: bool) -> None:
    if CALLS is None:
        raise RuntimeError("Media engine is not running.")

    method_name = "mute" if muted else "unmute"
    method = getattr(CALLS, method_name, None)

    if method is None:
        raise RuntimeError(f"PyTgCalls does not expose {method_name}().")

    result = method(chat_id)
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, 20)


async def mute_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        try:
            await _set_mute(chat_id, True)
            _state(chat_id).muted = True
            await message.reply_text("🔇 Muted.")
        except Exception as exc:
            log.exception("MEDIA MUTE FAILED: %r", exc)
            await message.reply_text(f"❌ Mute failed: {exc}")


async def unmute_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    chat_id = message.chat.id

    async with _lock(chat_id):
        try:
            await _set_mute(chat_id, False)
            _state(chat_id).muted = False
            await message.reply_text("🔊 Unmuted.")
        except Exception as exc:
            log.exception("MEDIA UNMUTE FAILED: %r", exc)
            await message.reply_text(f"❌ Unmute failed: {exc}")


async def volume_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _require_admin(update, context):
        return

    message = update.effective_message
    if not message:
        return

    if not context.args:
        await message.reply_text("Usage: /volume 100")
        return

    try:
        volume = int(context.args[0])
    except ValueError:
        await message.reply_text("❌ Volume must be a number from 0 to 200.")
        return

    volume = max(0, min(200, volume))
    chat_id = message.chat.id

    async with _lock(chat_id):
        try:
            result = CALLS.change_volume_call(chat_id, volume)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 20)

            _state(chat_id).volume = volume
            await message.reply_text(f"🔊 Volume: {volume}%")
        except Exception as exc:
            log.exception("MEDIA VOLUME FAILED: %r", exc)
            await message.reply_text(f"❌ Volume failed: {exc}")


async def _callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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

    try:
        chat_id = int(parts[1])
    except ValueError:
        return

    action = parts[2]

    # The callback message belongs to a group. Check the clicker's permissions.
    if query.message is None:
        return

    if not await _is_admin(update, context):
        await query.answer("Admins only.", show_alert=True)
        return

    fake_update = update

    if action == "pause":
        await pause_handler(fake_update, context)
    elif action == "resume":
        await resume_handler(fake_update, context)
    elif action == "skip":
        await skip_handler(fake_update, context)
    elif action == "stop":
        await stop_handler(fake_update, context)
    elif action == "queue":
        await queue_handler(fake_update, context)
    elif action == "repeat":
        await repeat_handler(fake_update, context)


async def shutdown_media_player() -> None:
    global USER_CLIENT, CALLS, _ENGINE_READY, _EVENTS_REGISTERED

    log.info("MEDIA SHUTDOWN: starting cleanup...")

    for chat_id in list(PLAYERS):
        try:
            async with _lock(chat_id):
                state = PLAYERS[chat_id]
                if CALLS is not None:
                    await _safe_leave(chat_id)

                if state.current:
                    await _cleanup_path(state.current.temp_path)

                for item in state.queue:
                    await _cleanup_path(item.temp_path)

                state.queue.clear()
                state.current = None
                state.paused = False
                state.starting = False
        except Exception:
            log.exception("MEDIA SHUTDOWN: chat cleanup failed chat=%s", chat_id)

    PLAYERS.clear()
    CHAT_LOCKS.clear()

    if CALLS is not None:
        try:
            result = CALLS.stop()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 20)
            log.info("MEDIA SHUTDOWN: PyTgCalls stopped.")
        except Exception:
            log.exception("MEDIA SHUTDOWN: PyTgCalls stop failed.")

    CALLS = None
    _EVENTS_REGISTERED = False

    if USER_CLIENT is not None:
        try:
            if getattr(USER_CLIENT, "is_connected", False) or getattr(
                USER_CLIENT, "is_started", False
            ):
                await asyncio.wait_for(USER_CLIENT.stop(), 20)
            log.info("MEDIA SHUTDOWN: Pyrogram stopped.")
        except Exception:
            log.exception("MEDIA SHUTDOWN: Pyrogram stop failed.")

    USER_CLIENT = None
    _ENGINE_READY = False

    log.info("MEDIA SHUTDOWN: complete.")


async def _post_shutdown(application: Application) -> None:
    await shutdown_media_player()


def register_media_play(application: Application) -> None:
    """
    Called from bot.py:

        media_play.register_media_play(application)

    This module intentionally keeps all player state inside itself.
    """
    # Slash commands
    application.add_handler(CommandHandler("play", play_handler))
    application.add_handler(CommandHandler("video", play_handler))
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
    application.add_handler(CommandHandler("mute", mute_handler))
    application.add_handler(CommandHandler("unmute", unmute_handler))
    application.add_handler(CommandHandler("volume", volume_handler))

    # Buttons
    application.add_handler(CallbackQueryHandler(_callback_handler, pattern=r"^mp:"))

    # Plain Arabic/English player commands.
    # No flags argument is used, avoiding the Regex(flags=...) issue.
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^(?:شغل|play)$"),
            play_text_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^(?:فيديو|video)$"),
            play_text_handler,
        )
    )

    # Ensure Pyrogram/PyTgCalls are cleaned up during normal PTB shutdown.
    application.post_shutdown = _post_shutdown

    log.info("MEDIA PLAY: handlers registered successfully.")
