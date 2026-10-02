from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import edge_tts

from pyrogram import Client
from pyrogram import filters
from pyrogram.types import Message

from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import AudioQuality
from pytgcalls.types import MediaStream
from pytgcalls.types import VideoQuality


# ============================================================
# CONFIG
# ============================================================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

TTS_VOICE = "en-US-AriaNeural"

TMP_DIR = Path(tempfile.gettempdir()) / "fixmyenglish_media"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CLIENTS
# ============================================================

USER_CLIENT: Optional[Client] = None
CALLS: Optional[PyTgCalls] = None

_ENGINE_LOCK = asyncio.Lock()
_ENGINE_STARTED = False
_EVENTS_REGISTERED = False


# ============================================================
# DATA
# ============================================================

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

    # TTS item
    tts_text: Optional[str] = None


@dataclass
class PlayerState:
    queue: list[MediaItem] = field(default_factory=list)

    current: Optional[MediaItem] = None

    paused: bool = False
    starting: bool = False
    seeking: bool = False

    suppress_end_until: float = 0.0

    repeat: str = "off"


STATES: dict[int, PlayerState] = {}
STATE_LOCKS: dict[int, asyncio.Lock] = {}


def get_state(chat_id: int) -> PlayerState:
    if chat_id not in STATES:
        STATES[chat_id] = PlayerState()

    return STATES[chat_id]


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in STATE_LOCKS:
        STATE_LOCKS[chat_id] = asyncio.Lock()

    return STATE_LOCKS[chat_id]


# ============================================================
# BASIC HELPERS
# ============================================================

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def format_time(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00"

    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "00:00"

    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"


def get_topic_id(message: Message) -> Optional[int]:
    return getattr(message, "message_thread_id", None)


def _reply_target(message: Message) -> Optional[Message]:
    return getattr(message, "reply_to_message", None)


def _source_message(message: Message) -> Optional[Message]:
    target = _reply_target(message)

    if target:
        return target

    return None


# ============================================================
# PERMISSIONS
# ============================================================

async def _is_admin(message: Message) -> bool:
    user = getattr(message, "from_user", None)

    if user and OWNER_ID and user.id == OWNER_ID:
        return True

    if message.chat.type == "private":
        return True

    if not user:
        return False

    try:
        member = await USER_CLIENT.get_chat_member(
            message.chat.id,
            user.id,
        )

        status = str(member.status).lower()

        return status in {
            "administrator",
            "owner",
        }

    except Exception:
        return False


# ============================================================
# MEDIA DETECTION
# ============================================================

def _media_from_message(message: Message) -> Optional[tuple[str, str]]:
    if message.audio:
        title = (
            message.audio.title
            or message.audio.file_name
            or "Audio"
        )
        return "audio", title

    if message.video:
        title = (
            message.video.file_name
            or "Video"
        )
        return "video", title

    if message.voice:
        return "audio", "Voice"

    if message.document:
        mime = (message.document.mime_type or "").lower()
        name = message.document.file_name or "Media"

        if mime.startswith("video/"):
            return "video", name

        if mime.startswith("audio/"):
            return "audio", name

    return None


# ============================================================
# TTS
# ============================================================

async def _make_tts(text: str) -> str:
    text = text.strip()

    if not text:
        raise ValueError("Empty text")

    output = tempfile.NamedTemporaryFile(
        suffix=".mp3",
        delete=False,
        dir=TMP_DIR,
    )

    output_path = output.name
    output.close()

    communicate = edge_tts.Communicate(
        text,
        TTS_VOICE,
    )

    await asyncio.wait_for(
        communicate.save(output_path),
        timeout=45,
    )

    if not os.path.exists(output_path):
        raise RuntimeError("TTS file was not created")

    if os.path.getsize(output_path) < 100:
        raise RuntimeError("TTS file is empty")

    return output_path


# ============================================================
# ENGINE
# ============================================================

async def _ensure_engine() -> None:
    global USER_CLIENT
    global CALLS
    global _ENGINE_STARTED

    async with _ENGINE_LOCK:

        if _ENGINE_STARTED:
            return

        if not API_ID:
            raise RuntimeError("API_ID is missing")

        if not API_HASH:
            raise RuntimeError("API_HASH is missing")

        if not SESSION_STRING:
            raise RuntimeError("SESSION_STRING is missing")

        if not ffmpeg_available():
            raise RuntimeError("FFmpeg is not installed")

        USER_CLIENT = Client(
            "fixmyenglish_media",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
            in_memory=True,
        )

        await USER_CLIENT.start()

        CALLS = PyTgCalls(USER_CLIENT)

        CALLS.start()

        _ENGINE_STARTED = True

        _register_call_events()


def _register_call_events() -> None:
    global _EVENTS_REGISTERED

    if _EVENTS_REGISTERED:
        return

    _EVENTS_REGISTERED = True

    @CALLS.on_update(call_filters.stream_end())
    async def _stream_end_handler(_, update):
        try:
            await _handle_stream_end(update.chat_id)
        except Exception as exc:
            print("stream_end error:", repr(exc))


# ============================================================
# STREAM
# ============================================================

def _build_stream(item: MediaItem) -> MediaStream:
    path = item.temp_path or item.source_path

    if not path:
        raise RuntimeError("No media file")

    if item.kind == "video" and not item.audio_only:
        return MediaStream(
            path,
            AudioQuality.HIGH,
            VideoQuality.HD_720p,
        )

    return MediaStream(
        path,
        AudioQuality.HIGH,
    )


# ============================================================
# SAFE LEAVE
# ============================================================

async def _safe_leave(chat_id: int) -> None:
    if not CALLS:
        return

    try:
        await CALLS.leave_call(chat_id)

    except Exception as exc:
        text = str(exc).lower()

        if (
            "notincall" in text
            or "not in call" in text
            or "no active call" in text
        ):
            return

        print("leave_call error:", repr(exc))


# ============================================================
# DOWNLOAD
# ============================================================

async def _download_item(item: MediaItem) -> str:
    if item.source_path and os.path.exists(item.source_path):
        return item.source_path

    if not USER_CLIENT:
        raise RuntimeError("Media client is not started")

    message = await USER_CLIENT.get_messages(
        item.chat_id,
        item.message_id,
    )

    if not message:
        raise RuntimeError("Message not found")

    media = _media_from_message(message)

    if not media:
        raise RuntimeError("No playable media")

    path = await USER_CLIENT.download_media(
        message,
        file_name=str(TMP_DIR) + "/",
    )

    if not path:
        raise RuntimeError("Download failed")

    item.source_path = path

    return path


# ============================================================
# CLEANUP
# ============================================================

def _remove_file(path: Optional[str]) -> None:
    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_item(item: Optional[MediaItem]) -> None:
    if not item:
        return

    paths = {
        item.temp_path,
        item.source_path,
    }

    for path in paths:
        _remove_file(path)

    item.temp_path = None
    item.source_path = None


# ============================================================
# NOW PLAYING
# ============================================================

async def _send_now_playing(
    chat_id: int,
    item: MediaItem,
) -> None:

    if not USER_CLIENT:
        return

    text = (
        "🎵 <b>Now Playing</b>\n\n"
        f"<b>{item.title}</b>"
    )

    try:
        await USER_CLIENT.send_message(
            chat_id,
            text,
            message_thread_id=item.thread_id,
        )

    except Exception as exc:
        error = str(exc).lower()

        if "topic_closed" in error:
            try:
                await USER_CLIENT.send_message(
                    chat_id,
                    text,
                )
            except Exception:
                pass
        else:
            print("now playing error:", repr(exc))


# ============================================================
# START CURRENT
# ============================================================

async def _start_current(chat_id: int) -> bool:
    state = get_state(chat_id)

    item = state.current

    if not item:
        return False

    state.starting = True

    try:
        if not item.source_path:
            await _download_item(item)

        stream = _build_stream(item)

        await CALLS.play(
            chat_id,
            stream,
        )

        state.paused = False

        await _send_now_playing(
            chat_id,
            item,
        )

        return True

    except Exception as exc:
        print("start_current error:", repr(exc))

        await _safe_leave(chat_id)

        return False

    finally:
        state.starting = False


# ============================================================
# STREAM END
# ============================================================

async def _handle_stream_end(chat_id: int) -> None:
    state = get_state(chat_id)

    if state.suppress_end_until > time.monotonic():
        return

    lock = get_lock(chat_id)

    async with lock:

        if state.suppress_end_until > time.monotonic():
            return

        current = state.current

        if not current:
            return

        # ----------------------------------------------------
        # REPEAT ONE
        # ----------------------------------------------------

        if state.repeat == "one":

            if current.tts_text:
                _cleanup_item(current)

                try:
                    path = await _make_tts(
                        current.tts_text
                    )

                    current.source_path = path

                    await _start_current(chat_id)

                except Exception as exc:
                    print("repeat TTS error:", repr(exc))

                    state.current = None
                    await _safe_leave(chat_id)

                return

            _cleanup_item(current)

            try:
                await _start_current(chat_id)
            except Exception as exc:
                print("repeat one error:", repr(exc))

            return

        # ----------------------------------------------------
        # REPEAT ALL
        # ----------------------------------------------------

        if state.repeat == "all":

            finished = current

            if finished.tts_text:
                _cleanup_item(finished)

                try:
                    path = await _make_tts(
                        finished.tts_text
                    )

                    finished.source_path = path
                except Exception as exc:
                    print("repeat all TTS error:", repr(exc))
                    finished = None

            else:
                _cleanup_item(finished)

            if finished:
                state.queue.append(finished)

        else:
            _cleanup_item(current)

        # ----------------------------------------------------
        # NEXT
        # ----------------------------------------------------

        if state.queue:
            state.current = state.queue.pop(0)

            await _start_current(chat_id)

        else:
            state.current = None
            state.paused = False

            await _safe_leave(chat_id)


# ============================================================
# ENQUEUE
# ============================================================

async def _enqueue(
    message: Message,
    forced_kind: Optional[str] = None,
) -> None:

    chat_id = message.chat.id

    target = _source_message(message)

    if not target:
        await message.reply_text(
            "❌ Reply to an audio, video, or text message."
        )
        return

    # ========================================================
    # TTS
    # ========================================================

    if target.text and not _media_from_message(target):

        text = target.text.strip()

        if not text:
            await message.reply_text(
                "❌ The text is empty."
            )
            return

        try:
            await _ensure_engine()

            await message.reply_text(
                "🔊 Preparing pronunciation..."
            )

            path = await _make_tts(text)

        except Exception as exc:
            print("TTS error:", repr(exc))

            await message.reply_text(
                "❌ I couldn't generate the pronunciation."
            )
            return

        item = MediaItem(
            chat_id=chat_id,
            message_id=target.id,
            title=text[:80],
            kind="audio",
            requester_id=(
                message.from_user.id
                if message.from_user
                else 0
            ),
            source_path=path,
            thread_id=get_topic_id(message),
            audio_only=True,
            tts_text=text,
        )

    else:

        detected = _media_from_message(target)

        if not detected:
            await message.reply_text(
                "❌ Reply to an audio or video file."
            )
            return

        kind, title = detected

        if forced_kind == "video" and kind != "video":
            await message.reply_text(
                "❌ This is not a video."
            )
            return

        if forced_kind == "audio":
            audio_only = True
        else:
            audio_only = False

        item = MediaItem(
            chat_id=chat_id,
            message_id=target.id,
            title=title,
            kind=kind,
            requester_id=(
                message.from_user.id
                if message.from_user
                else 0
            ),
            thread_id=get_topic_id(message),
            audio_only=audio_only,
        )

    state = get_state(chat_id)

    lock = get_lock(chat_id)

    async with lock:

        if state.current:

            state.queue.append(item)

            position = len(state.queue)

            await message.reply_text(
                f"➕ Added to queue\n"
                f"Position: {position}"
            )

            return

        state.current = item

        success = await _start_current(chat_id)

        if not success:
            _cleanup_item(item)

            state.current = None

            await message.reply_text(
                "❌ I couldn't start playback."
            )


# ============================================================
# PLAY COMMAND
# ============================================================

async def play_handler(client, message: Message):
    if not await _is_admin(message):
        await message.reply_text(
            "❌ Admin permission required."
        )
        return

    await _ensure_engine()

    await _enqueue(message)


# ============================================================
# AUDIO COMMAND
# ============================================================

async def audio_handler(client, message: Message):
    if not await _is_admin(message):
        await message.reply_text(
            "❌ Admin permission required."
        )
        return

    await _ensure_engine()

    await _enqueue(
        message,
        forced_kind="audio",
    )


# ============================================================
# VIDEO COMMAND
# ============================================================

async def video_handler(client, message: Message):
    if not await _is_admin(message):
        await message.reply_text(
            "❌ Admin permission required."
        )
        return

    await _ensure_engine()

    await _enqueue(
        message,
        forced_kind="video",
    )


# ============================================================
# PAUSE
# ============================================================

async def pause_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    if not state.current:
        await message.reply_text(
            "❌ Nothing is playing."
        )
        return

    try:
        await CALLS.pause(chat_id)

        state.paused = True

        await message.reply_text(
            "⏸ Paused."
        )

    except Exception as exc:
        print("pause error:", repr(exc))

        await message.reply_text(
            "❌ Couldn't pause playback."
        )


# ============================================================
# RESUME
# ============================================================

async def resume_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    if not state.current:
        await message.reply_text(
            "❌ Nothing is playing."
        )
        return

    try:
        await CALLS.resume(chat_id)

        state.paused = False

        await message.reply_text(
            "▶️ Resumed."
        )

    except Exception as exc:
        print("resume error:", repr(exc))

        await message.reply_text(
            "❌ Couldn't resume playback."
        )


# ============================================================
# SKIP
# ============================================================

async def skip_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    lock = get_lock(chat_id)

    async with lock:

        if not state.current:
            await message.reply_text(
                "❌ Nothing is playing."
            )
            return

        state.suppress_end_until = (
            time.monotonic() + 8
        )

        await _safe_leave(chat_id)

        _cleanup_item(state.current)

        if state.queue:
            state.current = state.queue.pop(0)

            success = await _start_current(
                chat_id
            )

            if success:
                await message.reply_text(
                    "⏭ Skipped."
                )
            else:
                state.current = None
                await message.reply_text(
                    "❌ Couldn't start the next item."
                )

        else:
            state.current = None
            state.paused = False

            await message.reply_text(
                "⏭ Queue is empty."
            )


# ============================================================
# STOP
# ============================================================

async def stop_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    lock = get_lock(chat_id)

    async with lock:

        state.suppress_end_until = (
            time.monotonic() + 8
        )

        await _safe_leave(chat_id)

        _cleanup_item(state.current)

        for item in state.queue:
            _cleanup_item(item)

        state.current = None
        state.queue.clear()
        state.paused = False
        state.starting = False
        state.seeking = False

    await message.reply_text(
        "⏹ Stopped."
    )


# ============================================================
# LEAVE
# ============================================================

async def leave_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    lock = get_lock(chat_id)

    async with lock:

        state.suppress_end_until = (
            time.monotonic() + 8
        )

        await _safe_leave(chat_id)

        _cleanup_item(state.current)

        for item in state.queue:
            _cleanup_item(item)

        state.current = None
        state.queue.clear()
        state.paused = False

    await message.reply_text(
        "👋 Left the voice chat."
    )


# ============================================================
# QUEUE
# ============================================================

async def queue_handler(client, message: Message):
    chat_id = message.chat.id

    state = get_state(chat_id)

    if not state.current and not state.queue:
        await message.reply_text(
            "📭 Queue is empty."
        )
        return

    lines = ["📋 <b>Queue</b>", ""]

    if state.current:
        lines.append(
            f"▶️ <b>Now:</b> {state.current.title}"
        )

    if state.queue:
        lines.append("")
        lines.append("<b>Next:</b>")

        for index, item in enumerate(
            state.queue,
            start=1,
        ):
            lines.append(
                f"{index}. {item.title}"
            )

    await message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# NOW PLAYING
# ============================================================

async def now_handler(client, message: Message):
    chat_id = message.chat.id

    state = get_state(chat_id)

    if not state.current:
        await message.reply_text(
            "❌ Nothing is playing."
        )
        return

    current_time = 0

    try:
        current_time = await CALLS.time(
            chat_id
        )
    except Exception:
        pass

    status = (
        "⏸ Paused"
        if state.paused
        else "▶️ Playing"
    )

    await message.reply_text(
        "🎵 <b>Now Playing</b>\n\n"
        f"<b>{state.current.title}</b>\n\n"
        f"{status}\n"
        f"⏱ {format_time(current_time)}"
    )


# ============================================================
# CLEAR QUEUE
# ============================================================

async def clear_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    for item in state.queue:
        _cleanup_item(item)

    state.queue.clear()

    await message.reply_text(
        "🗑 Queue cleared."
    )


# ============================================================
# SHUFFLE
# ============================================================

async def shuffle_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    if len(state.queue) < 2:
        await message.reply_text(
            "❌ Not enough items to shuffle."
        )
        return

    random.shuffle(state.queue)

    await message.reply_text(
        "🔀 Queue shuffled."
    )


# ============================================================
# REPEAT
# ============================================================

async def repeat_handler(client, message: Message):
    chat_id = message.chat.id

    if not await _is_admin(message):
        return

    state = get_state(chat_id)

    state.repeat = {
        "off": "one",
        "one": "all",
        "all": "off",
    }[state.repeat]

    names = {
        "off": "Off",
        "one": "One",
        "all": "All",
    }

    await message.reply_text(
        f"🔁 Repeat: <b>{names[state.repeat]}</b>"
    )


# ============================================================
# SEEK HELPERS
# ============================================================

async def _create_seek_file(
    source: str,
    seconds: int,
) -> str:

    if not ffmpeg_available():
        raise RuntimeError("FFmpeg unavailable")

    suffix = Path(source).suffix or ".mp4"

    output = tempfile.NamedTemporaryFile(
        suffix=suffix,
        delete=False,
        dir=TMP_DIR,
    )

    output_path = output.name
    output.close()

    # Re-encoding is intentionally used here.
    # It is slower than stream-copy, but much more
    # reliable for exact -10/+10 seeking.

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(seconds),
        "-i",
        source,
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        _remove_file(output_path)

        error_text = stderr.decode(
            "utf-8",
            errors="ignore",
        )

        raise RuntimeError(
            error_text[-1000:]
        )

    if not os.path.exists(output_path):
        raise RuntimeError(
            "Seek file was not created"
        )

    if os.path.getsize(output_path) < 100:
        _remove_file(output_path)

        raise RuntimeError(
            "Seek file is empty"
        )

    return output_path


# ============================================================
# SEEK
# ============================================================

async def _seek(
    chat_id: int,
    delta: int,
) -> tuple[bool, str]:

    state = get_state(chat_id)

    if not state.current:
        return False, "Nothing is playing."

    if state.seeking:
        return False, "A seek operation is already running."

    item = state.current

    state.seeking = True

    try:
        try:
            current_time = await CALLS.time(
                chat_id
            )
        except Exception:
            current_time = 0

        try:
            current_time = float(current_time)
        except Exception:
            current_time = 0

        target = max(
            0,
            int(current_time) + delta,
        )

        source = item.source_path

        if not source or not os.path.exists(source):
            await _download_item(item)
            source = item.source_path

        if not source:
            return False, "Media source is unavailable."

        # If already at the beginning and user presses -10.
        if delta < 0 and target == 0:
            return True, "Already at 00:00."

        seek_path = await _create_seek_file(
            source,
            target,
        )

        old_temp = item.temp_path

        state.suppress_end_until = (
            time.monotonic() + 10
        )

        await _safe_leave(chat_id)

        if old_temp and old_temp != source:
            _remove_file(old_temp)

        item.temp_path = seek_path

        stream = _build_stream(item)

        await CALLS.play(
            chat_id,
            stream,
        )

        state.paused = False

        return True, format_time(target)

    except Exception as exc:
        print("seek error:", repr(exc))

        return False, "Seek failed."

    finally:
        state.seeking = False


# ============================================================
# SEEK BUTTON CALLBACKS
# ============================================================

async def _seek_handler(
    client,
    callback_query,
    delta: int,
):
    chat_id = callback_query.message.chat.id

    try:
        await callback_query.answer(
            "Seeking..."
        )
    except Exception:
        pass

    if not await _is_admin(
        callback_query.message
    ):
        try:
            await callback_query.answer(
                "Admin permission required.",
                show_alert=True,
            )
        except Exception:
            pass

        return

    success, result = await _seek(
        chat_id,
        delta,
    )

    if success:
        try:
            await callback_query.answer(
                f"⏱ {result}"
            )
        except Exception:
            pass
    else:
        try:
            await callback_query.answer(
                result,
                show_alert=True,
            )
        except Exception:
            pass


# ============================================================
# CALLBACK BUTTONS
# ============================================================

def _buttons(state: PlayerState):
    from pyrogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )

    rows = []

    if state.paused:
        rows.append(
            [
                InlineKeyboardButton(
                    "▶️ Resume",
                    callback_data="media_resume",
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏸ Pause",
                    callback_data="media_pause",
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "⏪ -10",
                callback_data="media_back10",
            ),
            InlineKeyboardButton(
                "⏩ +10",
                callback_data="media_forward10",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "⏭ Skip",
                callback_data="media_skip",
            ),
            InlineKeyboardButton(
                "⏹ Stop",
                callback_data="media_stop",
            ),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "📋 Queue",
                callback_data="media_queue",
            ),
            InlineKeyboardButton(
                "🔁 Repeat",
                callback_data="media_repeat",
            ),
        ]
    )

    return InlineKeyboardMarkup(rows)


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def _callback_handler(
    client,
    callback_query,
):

    data = callback_query.data

    chat_id = callback_query.message.chat.id

    state = get_state(chat_id)

    if data == "media_back10":
        await _seek_handler(
            client,
            callback_query,
            -10,
        )
        return

    if data == "media_forward10":
        await _seek_handler(
            client,
            callback_query,
            10,
        )
        return

    try:
        await callback_query.answer()
    except Exception:
        pass

    if data == "media_pause":
        if not await _is_admin(
            callback_query.message
        ):
            return

        try:
            await CALLS.pause(chat_id)
            state.paused = True
        except Exception as exc:
            print("callback pause:", repr(exc))

    elif data == "media_resume":
        if not await _is_admin(
            callback_query.message
        ):
            return

        try:
            await CALLS.resume(chat_id)
            state.paused = False
        except Exception as exc:
            print("callback resume:", repr(exc))

    elif data == "media_skip":
        await skip_handler(
            client,
            callback_query.message,
        )
        return

    elif data == "media_stop":
        await stop_handler(
            client,
            callback_query.message,
        )
        return

    elif data == "media_queue":
        await queue_handler(
            client,
            callback_query.message,
        )
        return

    elif data == "media_repeat":
        await repeat_handler(
            client,
            callback_query.message,
        )
        return

    try:
        await callback_query.message.edit_reply_markup(
            _buttons(state)
        )
    except Exception:
        pass


# ============================================================
# OPTIONAL NOW PLAYING WITH BUTTONS
# ============================================================

async def _send_control_message(
    message: Message,
    state: PlayerState,
):
    try:
        await message.reply_text(
            "🎛 <b>Player Controls</b>",
            reply_markup=_buttons(state),
        )
    except Exception as exc:
        print("control message error:", repr(exc))


# ============================================================
# REGISTER COMMANDS
# ============================================================

def register_media_play(app) -> None:

    # --------------------------------------------------------
    # PLAY
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            play_handler,
            filters.command(
                [
                    "play",
                    "شغل",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # AUDIO
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            audio_handler,
            filters.command(
                [
                    "audio",
                    "شغل_صوت",
                    "شغل_صوتي",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            video_handler,
            filters.command(
                [
                    "video",
                    "فيديو",
                    "شغل_فيديو",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            pause_handler,
            filters.command(
                [
                    "pause",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            resume_handler,
            filters.command(
                [
                    "resume",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            skip_handler,
            filters.command(
                [
                    "skip",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            stop_handler,
            filters.command(
                [
                    "stop",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # LEAVE
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            leave_handler,
            filters.command(
                [
                    "leave",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            queue_handler,
            filters.command(
                [
                    "queue",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # NOW
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            now_handler,
            filters.command(
                [
                    "now",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # CLEAR
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            clear_handler,
            filters.command(
                [
                    "clear",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # SHUFFLE
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            shuffle_handler,
            filters.command(
                [
                    "shuffle",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # REPEAT
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["MessageHandler"],
        ).MessageHandler(
            repeat_handler,
            filters.command(
                [
                    "repeat",
                ]
            ),
        )
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    app.add_handler(
        __import__(
            "pyrogram.handlers",
            fromlist=["CallbackQueryHandler"],
        ).CallbackQueryHandler(
            _callback_handler,
            filters.regex(
                r"^media_"
            ),
        )
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown_media_player() -> None:
    global USER_CLIENT
    global CALLS
    global _ENGINE_STARTED

    for state in STATES.values():

        _cleanup_item(state.current)

        for item in state.queue:
            _cleanup_item(item)

        state.current = None
        state.queue.clear()

    STATES.clear()
    STATE_LOCKS.clear()

    if CALLS:
        chats = list(STATES.keys())

        for chat_id in chats:
            try:
                await CALLS.leave_call(chat_id)
            except Exception:
                pass

    CALLS = None

    if USER_CLIENT:
        try:
            await USER_CLIENT.stop()
        except Exception:
            pass

    USER_CLIENT = None

    _ENGINE_STARTED = False
