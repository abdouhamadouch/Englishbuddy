# =========================================================
# FixMyEnglish - Telegram Voice Chat Media Player
# media_play.py
# =========================================================

import asyncio
import html
import os
import re
import tempfile
import traceback
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate
from pytgcalls.types import StreamEnded


# =========================================================
# CONFIG
# =========================================================

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if API_ID:
    try:
        API_ID = int(API_ID)
    except ValueError:
        API_ID = None


# =========================================================
# DATA
# =========================================================

@dataclass
class QueueItem:
    chat_id: int
    message_id: int
    user_id: int
    username: str
    title: str
    media_type: str
    file_id: str
    temp_path: Optional[str] = None


@dataclass
class PlayerState:
    queue: list[QueueItem] = field(default_factory=list)

    current: Optional[QueueItem] = None

    paused: bool = False
    muted: bool = False

    volume: int = 100
    previous_volume: int = 100

    repeat_mode: str = "off"

    control_message_id: Optional[int] = None

    playing: bool = False
    starting: bool = False


# =========================================================
# GLOBAL STATE
# =========================================================

PLAYERS: dict[int, PlayerState] = {}

# One lock for creating the PyTgCalls engine.
ENGINE_LOCK = asyncio.Lock()

# One lock for each Telegram chat.
PLAYER_LOCKS: dict[int, asyncio.Lock] = {}

APPLICATION = None

USER_CLIENT: Optional[Client] = None
CALLS: Optional[PyTgCalls] = None


# =========================================================
# COMMANDS
# =========================================================

COMMANDS = {
    "play",
    "video",
    "pause",
    "resume",
    "skip",
    "next",
    "stop",
    "leave",
    "queue",
    "now",
    "repeat",
    "shuffle",
    "clear",
    "mute",
    "unmute",

    "شغل",
    "تشغيل",
    "فيديو",
    "وقف",
    "كمل",
    "تالي",
    "وقفه",
    "انهاء",
    "قائمة",
    "الآن",
    "كرر",
    "خلط",
    "مسح",
    "كتم",
    "صوت",
}


# =========================================================
# BASIC HELPERS
# =========================================================

def get_player(chat_id: int) -> PlayerState:
    state = PLAYERS.get(chat_id)

    if state is None:
        state = PlayerState()
        PLAYERS[chat_id] = state

    return state


def get_player_lock(chat_id: int) -> asyncio.Lock:
    lock = PLAYER_LOCKS.get(chat_id)

    if lock is None:
        lock = asyncio.Lock()
        PLAYER_LOCKS[chat_id] = lock

    return lock


def escape(text) -> str:
    return html.escape(str(text))


def display_name(user) -> str:
    if not user:
        return "Unknown"

    username = getattr(user, "username", None)

    if username:
        return f"@{username}"

    first_name = getattr(user, "first_name", None)

    if first_name:
        return first_name

    return str(getattr(user, "id", "Unknown"))


def clean_title(message) -> str:

    if getattr(message, "audio", None):
        return (
            message.audio.title
            or message.audio.file_name
            or "Audio"
        )

    if getattr(message, "video", None):
        return (
            message.video.file_name
            or "Video"
        )

    if getattr(message, "voice", None):
        return "Voice"

    if getattr(message, "document", None):
        return (
            message.document.file_name
            or "Media"
        )

    return "Media"


# =========================================================
# MEDIA DETECTION
# =========================================================

def get_media_info(message):

    if not message:
        return None, None

    if getattr(message, "audio", None):
        return (
            "audio",
            message.audio.file_id,
        )

    if getattr(message, "video", None):
        return (
            "video",
            message.video.file_id,
        )

    if getattr(message, "voice", None):
        return (
            "audio",
            message.voice.file_id,
        )

    document = getattr(message, "document", None)

    if document:

        mime = (
            getattr(document, "mime_type", None)
            or ""
        ).lower()

        if mime.startswith("video/"):
            return (
                "video",
                document.file_id,
            )

        if mime.startswith("audio/"):
            return (
                "audio",
                document.file_id,
            )

    return None, None


# =========================================================
# PERMISSIONS
# =========================================================

async def check_admin(update) -> bool:

    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)

    if not user or not chat:
        return False

    try:

        member = await chat.get_member(user.id)

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as e:

        print(
            f"[MEDIA PLAY] admin check error: {e}"
        )

        return False


async def can_control_current(update) -> bool:

    chat = getattr(
        update,
        "effective_chat",
        None,
    )

    user = getattr(
        update,
        "effective_user",
        None,
    )

    if not chat or not user:
        return False

    state = PLAYERS.get(chat.id)

    if not state or not state.current:
        return False

    if await check_admin(update):
        return True

    return (
        user.id
        == state.current.user_id
    )


# =========================================================
# ENGINE
# =========================================================

async def ensure_engine():

    global USER_CLIENT
    global CALLS

    if CALLS is not None:
        return CALLS

    async with ENGINE_LOCK:

        if CALLS is not None:
            return CALLS

        print(
            "[MEDIA PLAY] Starting Pyrogram/PyTgCalls..."
        )

        if not API_ID:
            raise RuntimeError(
                "API_ID is missing or invalid."
            )

        if not API_HASH:
            raise RuntimeError(
                "API_HASH is missing."
            )

        if not SESSION_STRING:
            raise RuntimeError(
                "SESSION_STRING is missing."
            )

        USER_CLIENT = Client(
            "fixmyenglish_player",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
        )

        try:

            await USER_CLIENT.start()

            print(
                "[MEDIA PLAY] Pyrogram user client started."
            )

        except Exception:

            print(
                "[MEDIA PLAY] Pyrogram start FAILED."
            )

            traceback.print_exc()

            USER_CLIENT = None

            raise

        try:

            CALLS = PyTgCalls(
                USER_CLIENT
            )

            CALLS.start()

            print(
                "[MEDIA PLAY] PyTgCalls started."
            )

            register_stream_events()

            return CALLS

        except Exception:

            print(
                "[MEDIA PLAY] PyTgCalls start FAILED."
            )

            traceback.print_exc()

            try:
                await USER_CLIENT.stop()
            except Exception:
                pass

            USER_CLIENT = None
            CALLS = None

            raise


# =========================================================
# PYTGCALLS EVENTS
# =========================================================

def register_stream_events():

    if CALLS is None:
        return

    @CALLS.on_update(
        fl.stream_end()
    )
    async def stream_end_handler(
        _,
        update: StreamEnded,
    ):

        chat_id = update.chat_id

        print(
            f"[MEDIA PLAY] Stream ended: {chat_id}"
        )

        try:

            await handle_stream_end(
                chat_id
            )

        except Exception:

            print(
                "[MEDIA PLAY] stream_end handler failed."
            )

            traceback.print_exc()


    @CALLS.on_update(
        fl.chat_update(
            ChatUpdate.Status.KICKED
            | ChatUpdate.Status.LEFT_GROUP
        )
    )
    async def chat_left_handler(
        _,
        update: ChatUpdate,
    ):

        chat_id = update.chat_id

        print(
            f"[MEDIA PLAY] User account left/kicked: "
            f"{chat_id}"
        )

        state = PLAYERS.get(chat_id)

        if not state:
            return

        current = state.current

        state.current = None
        state.queue.clear()
        state.playing = False
        state.paused = False
        state.starting = False

        remove_temp_file(current)

        await update_control_message(
            chat_id
        )


# =========================================================
# STREAM END
# =========================================================

async def handle_stream_end(chat_id: int):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state:
            return

        current = state.current

        if not current:
            return

        remove_temp_file(current)

        # ---------------------------------------------
        # REPEAT ONE
        # ---------------------------------------------

        if state.repeat_mode == "one":

            state.playing = False
            state.paused = False

            await start_current_unlocked(
                chat_id
            )

            return

        # ---------------------------------------------
        # REPEAT ALL
        # ---------------------------------------------

        if state.repeat_mode == "all":

            state.queue.append(
                current
            )

        # ---------------------------------------------
        # NEXT
        # ---------------------------------------------

        if state.queue:

            state.current = (
                state.queue.pop(0)
            )

            state.playing = False
            state.paused = False

            await start_current_unlocked(
                chat_id
            )

            return

        # ---------------------------------------------
        # FINISHED
        # ---------------------------------------------

        state.current = None
        state.playing = False
        state.paused = False

        try:

            if CALLS:
                await CALLS.leave_call(
                    chat_id
                )

        except Exception as e:

            print(
                f"[MEDIA PLAY] leave after end "
                f"error: {e}"
            )

        await update_control_message(
            chat_id
        )


# =========================================================
# DOWNLOAD
# =========================================================

async def download_media_for_play(
    item: QueueItem,
) -> str:

    await ensure_engine()

    if USER_CLIENT is None:
        raise RuntimeError(
            "Pyrogram user client is not available."
        )

    print(
        "[MEDIA PLAY] Downloading:"
        f" chat={item.chat_id}"
        f" message={item.message_id}"
    )

    message = await USER_CLIENT.get_messages(
        item.chat_id,
        item.message_id,
    )

    if not message:
        raise RuntimeError(
            "Original Telegram media message "
            "could not be found."
        )

    folder = tempfile.gettempdir()

    prefix = (
        f"fixmyenglish_"
        f"{item.chat_id}_"
        f"{item.message_id}_"
    )

    path = await USER_CLIENT.download_media(
        message,
        file_name=os.path.join(
            folder,
            prefix,
        ),
    )

    if not path:
        raise RuntimeError(
            "Telegram did not return a downloaded file."
        )

    if not os.path.exists(path):
        raise RuntimeError(
            f"Downloaded file does not exist: {path}"
        )

    size = os.path.getsize(path)

    if size <= 0:
        raise RuntimeError(
            "Downloaded media file is empty."
        )

    print(
        "[MEDIA PLAY] Download complete:"
        f" {path}"
        f" ({size} bytes)"
    )

    return path


def remove_temp_file(
    item: Optional[QueueItem],
):

    if not item:
        return

    path = item.temp_path

    if not path:
        return

    try:

        if os.path.exists(path):
            os.remove(path)

            print(
                f"[MEDIA PLAY] Removed temp file: "
                f"{path}"
            )

    except Exception as e:

        print(
            f"[MEDIA PLAY] temp cleanup error: {e}"
        )

    item.temp_path = None


# =========================================================
# START CURRENT
# =========================================================

async def start_current(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        return await start_current_unlocked(
            chat_id
        )


async def start_current_unlocked(
    chat_id: int,
):

    state = PLAYERS.get(chat_id)

    if not state or not state.current:
        return False

    if state.starting:
        print(
            f"[MEDIA PLAY] Already starting: "
            f"{chat_id}"
        )

        return False

    if CALLS is None:
        await ensure_engine()

    item = state.current

    state.starting = True

    try:

        # -----------------------------------------
        # Cleanup old temporary file
        # -----------------------------------------

        remove_temp_file(item)

        # -----------------------------------------
        # Download
        # -----------------------------------------

        path = await download_media_for_play(
            item
        )

        item.temp_path = path

        if CALLS is None:
            raise RuntimeError(
                "PyTgCalls engine is not available."
            )

        print(
            "[MEDIA PLAY] Starting playback:"
            f" chat={chat_id}"
            f" title={item.title}"
            f" type={item.media_type}"
        )

        # -----------------------------------------
        # PLAY
        # -----------------------------------------

        await CALLS.play(
            chat_id,
            path,
        )

        print(
            f"[MEDIA PLAY] PLAY successful: "
            f"{chat_id}"
        )

        state.playing = True
        state.paused = False

        # -----------------------------------------
        # Volume
        # -----------------------------------------

        try:

            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )

        except Exception as e:

            print(
                f"[MEDIA PLAY] volume restore error: "
                f"{e}"
            )

        await update_control_message(
            chat_id
        )

        return True

    except Exception as e:

        print(
            "[MEDIA PLAY] PLAYBACK FAILED:"
        )

        print(
            f"chat_id = {chat_id}"
        )

        print(
            f"title = {item.title}"
        )

        print(
            f"type = {item.media_type}"
        )

        print(
            f"error = {repr(e)}"
        )

        traceback.print_exc()

        remove_temp_file(item)

        state.playing = False
        state.paused = False

        # -----------------------------------------
        # Try next queued item
        # -----------------------------------------

        if state.queue:

            next_item = (
                state.queue.pop(0)
            )

            state.current = next_item

            print(
                "[MEDIA PLAY] Trying next queue item."
            )

            return await start_current_unlocked(
                chat_id
            )

        # -----------------------------------------
        # Nothing else
        # -----------------------------------------

        state.current = None

        try:

            await send_message(
                chat_id,
                (
                    "❌ <b>تعذر تشغيل الملف.</b>\n\n"
                    "تحقق من سجل Render لمعرفة "
                    "الخطأ التفصيلي."
                ),
            )

        except Exception:
            pass

        return False

    finally:

        state.starting = False


# =========================================================
# CONTROL MESSAGE
# =========================================================

def control_keyboard(
    state: PlayerState,
):

    if state.paused:

        pause_button = "▶️ Resume"
        pause_data = "mp_resume"

    else:

        pause_button = "⏸ Pause"
        pause_data = "mp_pause"

    mute_button = (
        "🔊 Unmute"
        if state.muted
        else "🔇 Mute"
    )

    mute_data = (
        "mp_unmute"
        if state.muted
        else "mp_mute"
    )

    return [

        [
            {
                "text": pause_button,
                "callback_data": pause_data,
            },
            {
                "text": "⏭ Skip",
                "callback_data": "mp_skip",
            },
        ],

        [
            {
                "text": "🔁 Repeat",
                "callback_data": "mp_repeat",
            },
            {
                "text": "🔀 Shuffle",
                "callback_data": "mp_shuffle",
            },
        ],

        [
            {
                "text": "📋 Queue",
                "callback_data": "mp_queue",
            },
            {
                "text": "🎵 Now",
                "callback_data": "mp_now",
            },
        ],

        [
            {
                "text": "🔉 -10",
                "callback_data": "mp_vol_down",
            },
            {
                "text": f"🔊 {state.volume}%",
                "callback_data": "mp_volume",
            },
            {
                "text": "🔊 +10",
                "callback_data": "mp_vol_up",
            },
        ],

        [
            {
                "text": mute_button,
                "callback_data": mute_data,
            },
        ],

        [
            {
                "text": "⏹ Stop",
                "callback_data": "mp_stop",
            },
            {
                "text": "🗑 Clear",
                "callback_data": "mp_clear",
            },
            {
                "text": "🚪 Leave",
                "callback_data": "mp_leave",
            },
        ],
    ]


def make_keyboard(state: PlayerState):

    from telegram import InlineKeyboardButton
    from telegram import InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    item["text"],
                    callback_data=item[
                        "callback_data"
                    ],
                )
                for item in row
            ]
            for row in control_keyboard(state)
        ]
    )


def player_text(
    state: PlayerState,
):

    if not state.current:

        return (
            "🎵 <b>Media Player</b>\n\n"
            "No media is playing."
        )

    current = state.current

    repeat_text = {
        "off": "Off",
        "one": "One",
        "all": "All",
    }.get(
        state.repeat_mode,
        "Off",
    )

    status = (
        "⏸ Paused"
        if state.paused
        else "▶️ Playing"
    )

    return (
        "🎵 <b>Media Player</b>\n\n"
        f"<b>Title:</b> "
        f"{escape(current.title)}\n"
        f"<b>Type:</b> "
        f"{escape(current.media_type)}\n"
        f"<b>By:</b> "
        f"{escape(current.username)}\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Volume:</b> {state.volume}%\n"
        f"<b>Repeat:</b> {repeat_text}\n"
        f"<b>Queue:</b> {len(state.queue)}"
    )


async def update_control_message(
    chat_id: int,
):

    if not APPLICATION:
        return

    state = PLAYERS.get(chat_id)

    if not state:
        return

    message_id = (
        state.control_message_id
    )

    if not message_id:
        return

    try:

        # If playback completely finished,
        # keep the message but remove old controls.
        if not state.current:

            await APPLICATION.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=player_text(state),
                parse_mode="HTML",
            )

            return

        await APPLICATION.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=player_text(state),
            parse_mode="HTML",
            reply_markup=make_keyboard(
                state
            ),
        )

    except Exception as e:

        print(
            f"[MEDIA PLAY] control edit error: {e}"
        )


async def send_control_message(
    chat_id: int,
):

    if not APPLICATION:
        return

    state = PLAYERS.get(chat_id)

    if not state:
        return

    try:

        message = await APPLICATION.bot.send_message(
            chat_id=chat_id,
            text=player_text(state),
            parse_mode="HTML",
            reply_markup=make_keyboard(
                state
            ),
        )

        state.control_message_id = (
            message.message_id
        )

    except Exception as e:

        print(
            f"[MEDIA PLAY] control message error: "
            f"{e}"
        )


async def send_message(
    chat_id: int,
    text: str,
):

    if not APPLICATION:
        return

    await APPLICATION.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
    )


# =========================================================
# QUEUE
# =========================================================

async def add_to_queue(
    update,
    force_video=False,
):

    message = update.effective_message

    if not message:
        return

    if not message.reply_to_message:

        await message.reply_text(
            "↩️ ردّ على ملف صوتي أو فيديو ثم اكتب:\n"
            "<code>شغل</code>"
        )

        return

    replied = (
        message.reply_to_message
    )

    media_type, file_id = (
        get_media_info(replied)
    )

    if not file_id:

        await message.reply_text(
            "❌ الرسالة التي رددت عليها ليست "
            "ملفًا صوتيًا أو فيديو قابلًا للتشغيل."
        )

        return

    if (
        force_video
        and media_type != "video"
    ):

        await message.reply_text(
            "❌ أمر <code>فيديو</code> يحتاج إلى فيديو."
        )

        return

    user = update.effective_user

    if not user:
        return

    item = QueueItem(
        chat_id=update.effective_chat.id,
        message_id=replied.message_id,
        user_id=user.id,
        username=display_name(user),
        title=clean_title(replied),
        media_type=media_type,
        file_id=file_id,
    )

    chat_id = update.effective_chat.id

    lock = get_player_lock(chat_id)

    async with lock:

        state = get_player(chat_id)

        # -----------------------------------------
        # Nothing playing
        # -----------------------------------------

        if not state.current:

            state.current = item
            state.playing = False
            state.paused = False

            await message.reply_text(
                (
                    "▶️ <b>Starting:</b> "
                    f"{escape(item.title)}"
                ),
                parse_mode="HTML",
            )

            success = (
                await start_current_unlocked(
                    chat_id
                )
            )

            if success:

                await send_control_message(
                    chat_id
                )

            return

        # -----------------------------------------
        # Add to queue
        # -----------------------------------------

        state.queue.append(item)

        position = len(state.queue)

        await message.reply_text(
            (
                "➕ <b>Added to queue</b>\n\n"
                f"{escape(item.title)}\n"
                f"Position: {position}"
            ),
            parse_mode="HTML",
        )

        await update_control_message(
            chat_id
        )


# =========================================================
# PAUSE / RESUME
# =========================================================

async def pause_player(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        if state.paused:
            return True

        try:

            await ensure_engine()

            await CALLS.pause(
                chat_id
            )

            state.paused = True

            await update_control_message(
                chat_id
            )

            return True

        except Exception as e:

            print(
                f"[MEDIA PLAY] pause error: {e}"
            )

            traceback.print_exc()

            return False


async def resume_player(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        if not state.paused:
            return True

        try:

            await ensure_engine()

            await CALLS.resume(
                chat_id
            )

            state.paused = False

            await update_control_message(
                chat_id
            )

            return True

        except Exception as e:

            print(
                f"[MEDIA PLAY] resume error: {e}"
            )

            traceback.print_exc()

            return False


# =========================================================
# STOP
# =========================================================

async def stop_player(
    chat_id: int,
    clear_queue=False,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state:
            return

        current = state.current

        # Important:
        # Clear current BEFORE leaving the call.
        state.current = None
        state.playing = False
        state.paused = False
        state.starting = False

        if clear_queue:
            state.queue.clear()

        remove_temp_file(current)

        try:

            if CALLS:
                await CALLS.leave_call(
                    chat_id
                )

        except Exception as e:

            print(
                f"[MEDIA PLAY] stop/leave error: "
                f"{e}"
            )

        await update_control_message(
            chat_id
        )


# =========================================================
# SKIP
# =========================================================

async def skip_player(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        current = state.current

        remove_temp_file(current)

        # Prevent stream_end from advancing
        # while we manually skip.
        state.current = None
        state.playing = False
        state.paused = False

        try:

            if CALLS:
                await CALLS.leave_call(
                    chat_id
                )

        except Exception as e:

            print(
                f"[MEDIA PLAY] skip leave error: "
                f"{e}"
            )

        # -----------------------------------------
        # Next item
        # -----------------------------------------

        if state.queue:

            state.current = (
                state.queue.pop(0)
            )

            return await start_current_unlocked(
                chat_id
            )

        await update_control_message(
            chat_id
        )

        return True


# =========================================================
# SHUFFLE
# =========================================================

async def shuffle_queue(
    chat_id: int,
):

    import random

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state:
            return False

        if len(state.queue) < 2:
            return False

        random.shuffle(
            state.queue
        )

        await update_control_message(
            chat_id
        )

        return True


# =========================================================
# REPEAT
# =========================================================

async def cycle_repeat(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = get_player(chat_id)

        modes = [
            "off",
            "one",
            "all",
        ]

        current_index = modes.index(
            state.repeat_mode
        )

        state.repeat_mode = modes[
            (current_index + 1) % len(modes)
        ]

        await update_control_message(
            chat_id
        )

        return state.repeat_mode


# =========================================================
# VOLUME
# =========================================================

async def change_volume(
    chat_id: int,
    volume: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        volume = max(
            0,
            min(200, int(volume)),
        )

        try:

            await ensure_engine()

            await CALLS.change_volume_call(
                chat_id,
                volume,
            )

            state.volume = volume

            if volume > 0:
                state.muted = False

            await update_control_message(
                chat_id
            )

            return True

        except Exception as e:

            print(
                f"[MEDIA PLAY] volume error: {e}"
            )

            traceback.print_exc()

            return False


async def mute_player(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        try:

            await ensure_engine()

            if not state.muted:

                state.previous_volume = max(
                    1,
                    state.volume,
                )

                await CALLS.change_volume_call(
                    chat_id,
                    0,
                )

                state.muted = True

            await update_control_message(
                chat_id
            )

            return True

        except Exception as e:

            print(
                f"[MEDIA PLAY] mute error: {e}"
            )

            traceback.print_exc()

            return False


async def unmute_player(
    chat_id: int,
):

    lock = get_player_lock(chat_id)

    async with lock:

        state = PLAYERS.get(chat_id)

        if not state or not state.current:
            return False

        try:

            await ensure_engine()

            volume = max(
                1,
                state.previous_volume,
            )

            await CALLS.change_volume_call(
                chat_id,
                volume,
            )

            state.volume = volume
            state.muted = False

            await update_control_message(
                chat_id
            )

            return True

        except Exception as e:

            print(
                f"[MEDIA PLAY] unmute error: {e}"
            )

            traceback.print_exc()

            return False


# =========================================================
# QUEUE TEXT
# =========================================================

def queue_text(
    state: PlayerState,
):

    lines = [
        "📋 <b>Queue</b>",
        "",
    ]

    if state.current:

        lines.append(
            "▶️ <b>Now:</b> "
            f"{escape(state.current.title)}"
        )

        lines.append(
            f"   👤 "
            f"{escape(state.current.username)}"
        )

    else:

        lines.append(
            "⏹ Nothing is playing."
        )

    lines.append("")

    if not state.queue:

        lines.append(
            "Queue is empty."
        )

        return "\n".join(lines)

    for index, item in enumerate(
        state.queue,
        start=1,
    ):

        lines.append(
            f"{index}. "
            f"{escape(item.title)}"
        )

        lines.append(
            f"   👤 "
            f"{escape(item.username)}"
        )

    return "\n".join(lines)


# =========================================================
# TEXT COMMANDS
# =========================================================

async def player_text_command(
    update,
    context,
):

    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    if not text:
        return

    command = text.casefold()

    normalized_commands = {
        x.casefold()
        for x in COMMANDS
    }

    if command not in normalized_commands:
        return

    chat_id = update.effective_chat.id

    # =====================================================
    # PLAY
    # =====================================================

    if command in (
        "play",
        "شغل",
        "تشغيل",
    ):

        await add_to_queue(
            update,
            force_video=False,
        )

        return

    # =====================================================
    # VIDEO
    # =====================================================

    if command in (
        "video",
        "فيديو",
    ):

        await add_to_queue(
            update,
            force_video=True,
        )

        return

    # =====================================================
    # QUEUE
    # =====================================================

    if command in (
        "queue",
        "قائمة",
    ):

        state = get_player(chat_id)

        await message.reply_text(
            queue_text(state),
            parse_mode="HTML",
        )

        return

    # =====================================================
    # NOW
    # =====================================================

    if command in (
        "now",
        "الآن",
    ):

        state = get_player(chat_id)

        await message.reply_text(
            player_text(state),
            parse_mode="HTML",
        )

        return

    # =====================================================
    # PAUSE
    # =====================================================

    if command in (
        "pause",
        "وقف",
    ):

        if not await can_control_current(
            update
        ):

            await message.reply_text(
                "⛔ You cannot control this track."
            )

            return

        ok = await pause_player(
            chat_id
        )

        if not ok:

            await message.reply_text(
                "❌ Nothing is playing."
            )

        return

    # =====================================================
    # RESUME
    # =====================================================

    if command in (
        "resume",
        "كمل",
    ):

        if not await can_control_current(
            update
        ):

            await message.reply_text(
                "⛔ You cannot control this track."
            )

            return

        ok = await resume_player(
            chat_id
        )

        if not ok:

            await message.reply_text(
                "❌ Nothing is paused."
            )

        return

    # =====================================================
    # SKIP
    # =====================================================

    if command in (
        "skip",
        "next",
        "تالي",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Skip is admin-only."
            )

            return

        await skip_player(
            chat_id
        )

        return

    # =====================================================
    # STOP
    # =====================================================

    if command in (
        "stop",
        "وقفه",
    ):

        if not await can_control_current(
            update
        ):

            await message.reply_text(
                "⛔ You cannot stop this track."
            )

            return

        await stop_player(
            chat_id
        )

        await message.reply_text(
            "⏹ Playback stopped."
        )

        return

    # =====================================================
    # LEAVE
    # =====================================================

    if command in (
        "leave",
        "انهاء",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Leave is admin-only."
            )

            return

        await stop_player(
            chat_id,
            clear_queue=True,
        )

        await message.reply_text(
            "🚪 Left the voice chat and cleared the queue."
        )

        return

    # =====================================================
    # REPEAT
    # =====================================================

    if command in (
        "repeat",
        "كرر",
    ):

        if not await can_control_current(
            update
        ):

            await message.reply_text(
                "⛔ You cannot control this track."
            )

            return

        mode = await cycle_repeat(
            chat_id
        )

        await message.reply_text(
            f"🔁 Repeat: <b>{escape(mode)}</b>",
            parse_mode="HTML",
        )

        return

    # =====================================================
    # SHUFFLE
    # =====================================================

    if command in (
        "shuffle",
        "خلط",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Shuffle is admin-only."
            )

            return

        await shuffle_queue(
            chat_id
        )

        await message.reply_text(
            "🔀 Queue shuffled."
        )

        return

    # =====================================================
    # CLEAR
    # =====================================================

    if command in (
        "clear",
        "مسح",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Clear is admin-only."
            )

            return

        lock = get_player_lock(
            chat_id
        )

        async with lock:

            state = get_player(
                chat_id
            )

            state.queue.clear()

            await update_control_message(
                chat_id
            )

        await message.reply_text(
            "🗑 Queue cleared."
        )

        return

    # =====================================================
    # MUTE
    # =====================================================

    if command in (
        "mute",
        "كتم",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Mute is admin-only."
            )

            return

        await mute_player(
            chat_id
        )

        return

    # =====================================================
    # UNMUTE
    # =====================================================

    if command in (
        "unmute",
        "صوت",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Unmute is admin-only."
            )

            return

        await unmute_player(
            chat_id
        )

        return


# =========================================================
# CALLBACKS
# =========================================================

async def player_callback(
    update,
    context,
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not query.message:
        return

    chat_id = query.message.chat.id

    state = PLAYERS.get(chat_id)

    if not state:
        return

    action = query.data

    # =====================================================
    # INFORMATION ACTIONS
    # =====================================================

    if action == "mp_queue":

        await query.edit_message_text(
            queue_text(state),
            parse_mode="HTML",
            reply_markup=(
                make_keyboard(state)
                if state.current
                else None
            ),
        )

        return

    if action == "mp_now":

        await query.edit_message_text(
            player_text(state),
            parse_mode="HTML",
            reply_markup=(
                make_keyboard(state)
                if state.current
                else None
            ),
        )

        return

    # =====================================================
    # PERMISSIONS
    # =====================================================

    admin_actions = {
        "mp_skip",
        "mp_shuffle",
        "mp_clear",
        "mp_leave",
        "mp_vol_down",
        "mp_vol_up",
        "mp_mute",
        "mp_unmute",
        "mp_volume",
    }

    if action in admin_actions:

        if not await check_admin(update):

            await query.answer(
                "⛔ Admin only.",
                show_alert=True,
            )

            return

    else:

        if not await can_control_current(
            update
        ):

            await query.answer(
                "⛔ You cannot control this track.",
                show_alert=True,
            )

            return

    # =====================================================
    # PAUSE
    # =====================================================

    if action == "mp_pause":

        await pause_player(
            chat_id
        )

    # =====================================================
    # RESUME
    # =====================================================

    elif action == "mp_resume":

        await resume_player(
            chat_id
        )

    # =====================================================
    # SKIP
    # =====================================================

    elif action == "mp_skip":

        await skip_player(
            chat_id
        )

    # =====================================================
    # REPEAT
    # =====================================================

    elif action == "mp_repeat":

        await cycle_repeat(
            chat_id
        )

    # =====================================================
    # SHUFFLE
    # =====================================================

    elif action == "mp_shuffle":

        await shuffle_queue(
            chat_id
        )

    # =====================================================
    # VOLUME DOWN
    # =====================================================

    elif action == "mp_vol_down":

        state = PLAYERS.get(chat_id)

        if state:

            await change_volume(
                chat_id,
                state.volume - 10,
            )

    # =====================================================
    # VOLUME UP
    # =====================================================

    elif action == "mp_vol_up":

        state = PLAYERS.get(chat_id)

        if state:

            await change_volume(
                chat_id,
                state.volume + 10,
            )

    # =====================================================
    # MUTE
    # =====================================================

    elif action == "mp_mute":

        await mute_player(
            chat_id
        )

    # =====================================================
    # UNMUTE
    # =====================================================

    elif action == "mp_unmute":

        await unmute_player(
            chat_id
        )

    # =====================================================
    # VOLUME INFO
    # =====================================================

    elif action == "mp_volume":

        state = PLAYERS.get(chat_id)

        if state:

            await query.answer(
                f"Volume: {state.volume}%",
                show_alert=True,
            )

            return

    # =====================================================
    # STOP
    # =====================================================

    elif action == "mp_stop":

        await stop_player(
            chat_id
        )

        try:

            await query.edit_message_text(
                "⏹ <b>Playback stopped.</b>",
                parse_mode="HTML",
            )

        except Exception:
            pass

        return

    # =====================================================
    # CLEAR
    # =====================================================

    elif action == "mp_clear":

        lock = get_player_lock(
            chat_id
        )

        async with lock:

            state = PLAYERS.get(
                chat_id
            )

            if state:
                state.queue.clear()

        await update_control_message(
            chat_id
        )

    # =====================================================
    # LEAVE
    # =====================================================

    elif action == "mp_leave":

        await stop_player(
            chat_id,
            clear_queue=True,
        )

        try:

            await query.edit_message_text(
                "🚪 <b>Left the voice chat.</b>",
                parse_mode="HTML",
            )

        except Exception:
            pass

        return

    await update_control_message(
        chat_id
    )


# =========================================================
# SLASH COMMAND WRAPPERS
# =========================================================

async def cmd_play(update, context):
    await add_to_queue(
        update,
        force_video=False,
    )


async def cmd_video(update, context):
    await add_to_queue(
        update,
        force_video=True,
    )


async def cmd_pause(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_resume(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_skip(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_stop(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_leave(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_queue(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_now(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_repeat(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_shuffle(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_clear(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_mute(update, context):
    await player_text_command(
        update,
        context,
    )


async def cmd_unmute(update, context):
    await player_text_command(
        update,
        context,
    )


# =========================================================
# REGISTER
# =========================================================

def register_media_play(application):

    global APPLICATION

    APPLICATION = application

    from telegram.ext import (
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    # =====================================================
    # ENGLISH SLASH COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "play",
            cmd_play,
        )
    )

    application.add_handler(
        CommandHandler(
            "video",
            cmd_video,
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            cmd_pause,
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            cmd_resume,
        )
    )

    application.add_handler(
        CommandHandler(
            "skip",
            cmd_skip,
        )
    )

    application.add_handler(
        CommandHandler(
            "next",
            cmd_skip,
        )
    )

    application.add_handler(
        CommandHandler(
            "stop",
            cmd_stop,
        )
    )

    application.add_handler(
        CommandHandler(
            "leave",
            cmd_leave,
        )
    )

    application.add_handler(
        CommandHandler(
            "queue",
            cmd_queue,
        )
    )

    application.add_handler(
        CommandHandler(
            "now",
            cmd_now,
        )
    )

    application.add_handler(
        CommandHandler(
            "repeat",
            cmd_repeat,
        )
    )

    application.add_handler(
        CommandHandler(
            "shuffle",
            cmd_shuffle,
        )
    )

    application.add_handler(
        CommandHandler(
            "clear",
            cmd_clear,
        )
    )

    application.add_handler(
        CommandHandler(
            "mute",
            cmd_mute,
        )
    )

    application.add_handler(
        CommandHandler(
            "unmute",
            cmd_unmute,
        )
    )

    # =====================================================
    # ARABIC / PLAIN TEXT COMMANDS
    # =====================================================

    pattern = (
        r"^\s*(?:"
        + "|".join(
            re.escape(x)
            for x in COMMANDS
        )
        + r")\s*$"
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(
                re.compile(
                    pattern,
                    re.IGNORECASE,
                )
            ),
            player_text_command,
        )
    )

    # =====================================================
    # CALLBACKS
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            player_callback,
            pattern=r"^mp_",
        )
    )

    print(
        "[MEDIA PLAY] registered successfully."
    )


# =========================================================
# OPTIONAL SHUTDOWN
# =========================================================

async def shutdown_media_play():

    global USER_CLIENT
    global CALLS

    print(
        "[MEDIA PLAY] Shutting down..."
    )

    # Leave active voice chats.
    if CALLS:

        for chat_id in list(
            PLAYERS.keys()
        ):

            try:

                await CALLS.leave_call(
                    chat_id
                )

            except Exception:
                pass

    # Remove temp files.
    for state in PLAYERS.values():

        remove_temp_file(
            state.current
        )

        for item in state.queue:
            remove_temp_file(item)

    PLAYERS.clear()
    PLAYER_LOCKS.clear()

    # Stop Pyrogram client.
    if USER_CLIENT:

        try:

            await USER_CLIENT.stop()

        except Exception as e:

            print(
                f"[MEDIA PLAY] Pyrogram shutdown error: "
                f"{e}"
            )

    USER_CLIENT = None
    CALLS = None

    print(
        "[MEDIA PLAY] Shutdown complete."
            )
