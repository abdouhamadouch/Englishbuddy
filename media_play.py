# media_play.py
# FixMyEnglish - Telegram Voice Chat Media Player

import asyncio
import html
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client
from pytgcalls import PyTgCalls, filters as fl
from pytgcalls.types import StreamEnded


# =========================================================
# CONFIG
# =========================================================

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")

if API_ID:
    API_ID = int(API_ID)


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
    repeatable: bool = True
    temp_path: Optional[str] = None


@dataclass
class PlayerState:
    queue: list = field(default_factory=list)
    current: Optional[QueueItem] = None

    paused: bool = False
    muted: bool = False

    volume: int = 100
    previous_volume: int = 100

    repeat_mode: str = "off"
    control_message_id: Optional[int] = None

    playing: bool = False
    starting: bool = False


PLAYERS = {}

APPLICATION = None

USER_CLIENT = None
CALLS = None

ENGINE_LOCK = asyncio.Lock()


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
# HELPERS
# =========================================================

def get_player(chat_id: int) -> PlayerState:
    if chat_id not in PLAYERS:
        PLAYERS[chat_id] = PlayerState()

    return PLAYERS[chat_id]


def is_admin(user) -> bool:
    if not user:
        return False

    return (
        getattr(user, "status", None)
        in ("administrator", "creator")
    )


async def check_admin(update) -> bool:
    try:
        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception:
        return False


def display_name(user) -> str:
    if not user:
        return "Unknown"

    if getattr(user, "username", None):
        return f"@{user.username}"

    name = getattr(user, "first_name", None)

    if name:
        return name

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


def get_media_info(message):
    if message.audio:
        return (
            "audio",
            message.audio.file_id,
        )

    if message.video:
        return (
            "video",
            message.video.file_id,
        )

    if message.voice:
        return (
            "audio",
            message.voice.file_id,
        )

    if message.document:
        mime = (
            message.document.mime_type
            or ""
        ).lower()

        if mime.startswith("video/"):
            return (
                "video",
                message.document.file_id,
            )

        if mime.startswith("audio/"):
            return (
                "audio",
                message.document.file_id,
            )

    return None, None


def escape(text) -> str:
    return html.escape(str(text))


# =========================================================
# USER CLIENT / PYTGCALLS
# =========================================================

async def ensure_engine():
    global USER_CLIENT
    global CALLS

    if CALLS is not None:
        return CALLS

    async with ENGINE_LOCK:

        if CALLS is not None:
            return CALLS

        if not API_ID:
            raise RuntimeError(
                "API_ID is missing."
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

        await USER_CLIENT.start()

        CALLS = PyTgCalls(USER_CLIENT)

        CALLS.start()

        register_stream_events()

    return CALLS


# =========================================================
# STREAM END
# =========================================================

def register_stream_events():

    @CALLS.on_update(fl.stream_end())
    async def stream_end_handler(
        _,
        update: StreamEnded,
    ):
        chat_id = update.chat_id

        try:
            await handle_stream_end(chat_id)
        except Exception as e:
            print(
                f"[MEDIA PLAY] stream_end error: {e}"
            )


async def handle_stream_end(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state:
        return

    current = state.current

    if not current:
        return

    # Remove temporary file
    remove_temp_file(current)

    # Repeat ONE
    if state.repeat_mode == "one":
        state.playing = False
        state.paused = False

        await start_current(chat_id)
        return

    # Repeat ALL
    if state.repeat_mode == "all":
        state.queue.append(current)

    # Next item
    if state.queue:

        state.current = state.queue.pop(0)
        state.playing = False
        state.paused = False

        await start_current(chat_id)
        return

    # Finished
    state.current = None
    state.playing = False
    state.paused = False

    try:
        await CALLS.leave_call(chat_id)
    except Exception:
        pass

    await update_control_message(chat_id)


# =========================================================
# FILE DOWNLOAD
# =========================================================

async def download_media_for_play(
    item: QueueItem,
) -> str:

    await ensure_engine()

    # The MTProto user account downloads the message.
    # This avoids relying only on Bot API file downloads.
    message = await USER_CLIENT.get_messages(
        item.chat_id,
        item.message_id,
    )

    if not message:
        raise RuntimeError(
            "The original media message could not be found."
        )

    folder = tempfile.gettempdir()

    prefix = (
        f"fixmyenglish_{item.chat_id}_"
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
            "Telegram did not return a media file."
        )

    return path


def remove_temp_file(item: Optional[QueueItem]):

    if not item:
        return

    path = item.temp_path

    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(
            f"[MEDIA PLAY] temp cleanup error: {e}"
        )

    item.temp_path = None


# =========================================================
# START CURRENT
# =========================================================

async def start_current(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state or not state.current:
        return

    if state.starting:
        return

    state.starting = True

    item = state.current

    try:

        await ensure_engine()

        # Remove previous temporary file
        remove_temp_file(item)

        path = await download_media_for_play(item)

        item.temp_path = path

        state.playing = True
        state.paused = False

        await CALLS.play(
            chat_id,
            path,
        )

        # Restore volume
        try:
            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )
        except Exception:
            pass

        await update_control_message(chat_id)

    except Exception as e:

        print(
            f"[MEDIA PLAY] playback error: {e}"
        )

        remove_temp_file(item)

        state.playing = False
        state.paused = False

        if state.queue:
            state.current = state.queue.pop(0)

            await start_current(chat_id)

        else:
            state.current = None

            try:
                await send_message(
                    chat_id,
                    "❌ تعذر تشغيل الملف."
                )
            except Exception:
                pass

        return

    finally:
        state.starting = False


# =========================================================
# CONTROL MESSAGE
# =========================================================

def control_keyboard(state: PlayerState):

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

    rows = []

    for row in control_keyboard(state):

        rows.append([
            InlineKeyboardButton(
                x["text"],
                callback_data=x["callback_data"],
            )
            for x in row
        ])

    return InlineKeyboardMarkup(rows)


def player_text(state: PlayerState):

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

    queue_count = len(state.queue)

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
        f"<b>Queue:</b> {queue_count}"
    )


async def update_control_message(chat_id: int):

    if not APPLICATION:
        return

    state = PLAYERS.get(chat_id)

    if not state:
        return

    message_id = state.control_message_id

    if not message_id:
        return

    try:

        await APPLICATION.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=player_text(state),
            parse_mode="HTML",
            reply_markup=make_keyboard(state),
        )

    except Exception as e:

        print(
            f"[MEDIA PLAY] control edit error: {e}"
        )


async def send_control_message(chat_id: int):

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
            reply_markup=make_keyboard(state),
        )

        state.control_message_id = message.message_id

    except Exception as e:

        print(
            f"[MEDIA PLAY] control message error: {e}"
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

    if not message.reply_to_message:

        await message.reply_text(
            "↩️ ردّ على ملف صوتي أو فيديو ثم اكتب:\n"
            "<code>شغل</code>"
        )

        return

    replied = message.reply_to_message

    media_type, file_id = get_media_info(
        replied
    )

    if not file_id:

        await message.reply_text(
            "❌ الرسالة التي رددت عليها ليست ملفًا "
            "صوتيًا أو فيديو قابلًا للتشغيل."
        )

        return

    if force_video and media_type != "video":

        await message.reply_text(
            "❌ أمر <code>فيديو</code> يحتاج إلى "
            "فيديو."
        )

        return

    user = update.effective_user

    username = display_name(user)

    item = QueueItem(
        chat_id=update.effective_chat.id,
        message_id=replied.message_id,
        user_id=user.id,
        username=username,
        title=clean_title(replied),
        media_type=media_type,
        file_id=file_id,
    )

    chat_id = update.effective_chat.id

    state = get_player(chat_id)

    # Nothing playing
    if not state.current:

        state.current = item
        state.playing = False

        await message.reply_text(
            f"▶️ <b>Starting:</b> "
            f"{escape(item.title)}"
        )

        await start_current(chat_id)

        if state.current:
            await send_control_message(chat_id)

        return

    # Queue
    state.queue.append(item)

    position = len(state.queue)

    await message.reply_text(
        "➕ <b>Added to queue</b>\n\n"
        f"{escape(item.title)}\n"
        f"Position: {position}"
    )

    await update_control_message(chat_id)


# =========================================================
# PAUSE / RESUME
# =========================================================

async def pause_player(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state or not state.current:
        return False

    if state.paused:
        return True

    try:

        await ensure_engine()

        await CALLS.pause(chat_id)

        state.paused = True

        await update_control_message(chat_id)

        return True

    except Exception as e:

        print(
            f"[MEDIA PLAY] pause error: {e}"
        )

        return False


async def resume_player(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state or not state.current:
        return False

    if not state.paused:
        return True

    try:

        await ensure_engine()

        await CALLS.resume(chat_id)

        state.paused = False

        await update_control_message(chat_id)

        return True

    except Exception as e:

        print(
            f"[MEDIA PLAY] resume error: {e}"
        )

        return False


# =========================================================
# STOP / LEAVE
# =========================================================

async def stop_player(
    chat_id: int,
    clear_queue=False,
):

    state = PLAYERS.get(chat_id)

    if not state:
        return

    current = state.current

    # Clear current first so stream_end won't
    # automatically start another item.
    state.current = None
    state.playing = False
    state.paused = False

    if clear_queue:
        state.queue.clear()

    try:

        if CALLS:
            await CALLS.leave_call(chat_id)

    except Exception:
        pass

    remove_temp_file(current)

    state.control_message_id = None


# =========================================================
# SKIP
# =========================================================

async def skip_player(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state or not state.current:
        return False

    current = state.current

    remove_temp_file(current)

    try:

        if CALLS:
            await CALLS.leave_call(chat_id)

    except Exception:
        pass

    if state.queue:

        state.current = state.queue.pop(0)
        state.playing = False
        state.paused = False

        await start_current(chat_id)

        return True

    state.current = None
    state.playing = False
    state.paused = False

    return True


# =========================================================
# SHUFFLE
# =========================================================

async def shuffle_queue(chat_id: int):

    import random

    state = PLAYERS.get(chat_id)

    if not state:
        return

    if len(state.queue) < 2:
        return

    random.shuffle(state.queue)


# =========================================================
# REPEAT
# =========================================================

async def cycle_repeat(chat_id: int):

    state = PLAYERS.get(chat_id)

    if not state:
        return "off"

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

    await update_control_message(chat_id)

    return state.repeat_mode


# =========================================================
# QUEUE TEXT
# =========================================================

def queue_text(state: PlayerState):

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
            f"   👤 {escape(state.current.username)}"
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
            f"   👤 {escape(item.username)}"
        )

    return "\n".join(lines)


# =========================================================
# PERMISSION
# =========================================================

async def can_control_current(
    update,
) -> bool:

    state = PLAYERS.get(
        update.effective_chat.id
    )

    if not state or not state.current:
        return False

    if await check_admin(update):
        return True

    return (
        update.effective_user.id
        == state.current.user_id
    )


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

    # Exact command only
    if text.lower() not in {
        x.lower()
        for x in COMMANDS
    }:
        return

    command = text.lower()

    chat_id = update.effective_chat.id

    # -----------------------------------------------------
    # PLAY
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    if command in (
        "video",
        "فيديو",
    ):

        await add_to_queue(
            update,
            force_video=True,
        )

        return

    # -----------------------------------------------------
    # QUEUE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # NOW
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # PAUSE
    # -----------------------------------------------------

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

        ok = await pause_player(chat_id)

        if not ok:

            await message.reply_text(
                "❌ Nothing is playing."
            )

        return

    # -----------------------------------------------------
    # RESUME
    # -----------------------------------------------------

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

        ok = await resume_player(chat_id)

        if not ok:

            await message.reply_text(
                "❌ Nothing is paused."
            )

        return

    # -----------------------------------------------------
    # SKIP
    # -----------------------------------------------------

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

        await skip_player(chat_id)

        await update_control_message(
            chat_id
        )

        return

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

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

        await stop_player(chat_id)

        await message.reply_text(
            "⏹ Playback stopped."
        )

        return

    # -----------------------------------------------------
    # LEAVE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # REPEAT
    # -----------------------------------------------------

    if command in (
        "repeat",
        "كرر",
    ):

        state = get_player(chat_id)

        mode = await cycle_repeat(chat_id)

        await message.reply_text(
            f"🔁 Repeat: <b>{mode}</b>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # SHUFFLE
    # -----------------------------------------------------

    if command in (
        "shuffle",
        "خلط",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Shuffle is admin-only."
            )

            return

        await shuffle_queue(chat_id)

        await update_control_message(
            chat_id
        )

        await message.reply_text(
            "🔀 Queue shuffled."
        )

        return

    # -----------------------------------------------------
    # CLEAR
    # -----------------------------------------------------

    if command in (
        "clear",
        "مسح",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Clear is admin-only."
            )

            return

        state = get_player(chat_id)

        state.queue.clear()

        await update_control_message(
            chat_id
        )

        await message.reply_text(
            "🗑 Queue cleared."
        )

        return

    # -----------------------------------------------------
    # MUTE
    # -----------------------------------------------------

    if command in (
        "mute",
        "كتم",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Mute is admin-only."
            )

            return

        state = get_player(chat_id)

        if not state.current:
            return

        try:

            await ensure_engine()

            if not state.muted:

                state.previous_volume = (
                    state.volume
                )

                await CALLS.change_volume_call(
                    chat_id,
                    0,
                )

                state.muted = True

            await update_control_message(
                chat_id
            )

        except Exception as e:

            print(
                f"[MEDIA PLAY] mute error: {e}"
            )

        return

    # -----------------------------------------------------
    # UNMUTE
    # -----------------------------------------------------

    if command in (
        "unmute",
        "صوت",
    ):

        if not await check_admin(update):

            await message.reply_text(
                "⛔ Unmute is admin-only."
            )

            return

        state = get_player(chat_id)

        if not state.current:
            return

        try:

            await ensure_engine()

            state.volume = max(
                1,
                state.previous_volume,
            )

            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )

            state.muted = False

            await update_control_message(
                chat_id
            )

        except Exception as e:

            print(
                f"[MEDIA PLAY] unmute error: {e}"
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

    await query.answer()

    chat_id = query.message.chat.id

    state = PLAYERS.get(chat_id)

    if not state:
        return

    action = query.data

    # -----------------------------------------------------
    # QUEUE
    # -----------------------------------------------------

    if action == "mp_queue":

        await query.edit_message_text(
            queue_text(state),
            parse_mode="HTML",
            reply_markup=make_keyboard(state),
        )

        return

    # -----------------------------------------------------
    # NOW
    # -----------------------------------------------------

    if action == "mp_now":

        await query.edit_message_text(
            player_text(state),
            parse_mode="HTML",
            reply_markup=make_keyboard(state),
        )

        return

    # -----------------------------------------------------
    # PERMISSIONS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # PAUSE
    # -----------------------------------------------------

    if action == "mp_pause":

        await pause_player(chat_id)

    # -----------------------------------------------------
    # RESUME
    # -----------------------------------------------------

    elif action == "mp_resume":

        await resume_player(chat_id)

    # -----------------------------------------------------
    # SKIP
    # -----------------------------------------------------

    elif action == "mp_skip":

        await skip_player(chat_id)

    # -----------------------------------------------------
    # REPEAT
    # -----------------------------------------------------

    elif action == "mp_repeat":

        await cycle_repeat(chat_id)

    # -----------------------------------------------------
    # SHUFFLE
    # -----------------------------------------------------

    elif action == "mp_shuffle":

        await shuffle_queue(chat_id)

    # -----------------------------------------------------
    # VOLUME DOWN
    # -----------------------------------------------------

    elif action == "mp_vol_down":

        state.volume = max(
            0,
            state.volume - 10,
        )

        state.muted = False

        try:

            await ensure_engine()

            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )

        except Exception as e:

            print(
                f"[MEDIA PLAY] volume error: {e}"
            )

    # -----------------------------------------------------
    # VOLUME UP
    # -----------------------------------------------------

    elif action == "mp_vol_up":

        state.volume = min(
            200,
            state.volume + 10,
        )

        state.muted = False

        try:

            await ensure_engine()

            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )

        except Exception as e:

            print(
                f"[MEDIA PLAY] volume error: {e}"
            )

    # -----------------------------------------------------
    # MUTE
    # -----------------------------------------------------

    elif action == "mp_mute":

        state.previous_volume = state.volume

        try:

            await ensure_engine()

            await CALLS.change_volume_call(
                chat_id,
                0,
            )

            state.muted = True

        except Exception as e:

            print(
                f"[MEDIA PLAY] mute error: {e}"
            )

    # -----------------------------------------------------
    # UNMUTE
    # -----------------------------------------------------

    elif action == "mp_unmute":

        state.volume = max(
            1,
            state.previous_volume,
        )

        try:

            await ensure_engine()

            await CALLS.change_volume_call(
                chat_id,
                state.volume,
            )

            state.muted = False

        except Exception as e:

            print(
                f"[MEDIA PLAY] unmute error: {e}"
            )

    # -----------------------------------------------------
    # VOLUME
    # -----------------------------------------------------

    elif action == "mp_volume":

        await query.answer(
            f"Volume: {state.volume}%",
            show_alert=True,
        )

    # -----------------------------------------------------
    # STOP
    # -----------------------------------------------------

    elif action == "mp_stop":

        await stop_player(chat_id)

        try:
            await query.edit_message_text(
                "⏹ <b>Playback stopped.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # CLEAR
    # -----------------------------------------------------

    elif action == "mp_clear":

        state.queue.clear()

    # -----------------------------------------------------
    # LEAVE
    # -----------------------------------------------------

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

    await update_control_message(chat_id)


# =========================================================
# COMMAND HANDLERS
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
    await player_text_command(update, context)


async def cmd_resume(update, context):
    await player_text_command(update, context)


async def cmd_skip(update, context):
    await player_text_command(update, context)


async def cmd_stop(update, context):
    await player_text_command(update, context)


async def cmd_leave(update, context):
    await player_text_command(update, context)


async def cmd_queue(update, context):
    await player_text_command(update, context)


async def cmd_now(update, context):
    await player_text_command(update, context)


async def cmd_repeat(update, context):
    await player_text_command(update, context)


async def cmd_shuffle(update, context):
    await player_text_command(update, context)


async def cmd_clear(update, context):
    await player_text_command(update, context)


async def cmd_mute(update, context):
    await player_text_command(update, context)


async def cmd_unmute(update, context):
    await player_text_command(update, context)


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

    # English slash commands
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

    # Arabic aliases / plain-text commands.
    #
    # IMPORTANT:
    # Do NOT use a handler for every text message.
    # It would interfere with the bot's other text handlers.
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
                pattern,
                flags=re.IGNORECASE,
            ),
            player_text_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            player_callback,
            pattern=r"^mp_",
        )
    )

    print(
        "[MEDIA PLAY] registered successfully."
    )
