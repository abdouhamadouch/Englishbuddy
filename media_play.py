# media_play.py
# FixMyEnglish - Voice Chat Player
#
# Features:
# - Audio / Video queue
# - Arabic + English commands
# - Per-group queues
# - Owner of a queued item can control his own item
# - Group admins can control everything
# - Inline English control buttons
# - Pause / Resume / Skip / Stop
# - Repeat One / Repeat All
# - Shuffle
# - Queue / Now Playing
# - Mute / Unmute
# - Volume
# - Clear queue
# - Leave voice chat
#
# IMPORTANT:
# The actual Telegram Voice Chat connection is intentionally
# isolated in VoiceEngine below. It will be connected to the
# MTProto/PyTgCalls client when bot.py is integrated.

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# Data
# ============================================================

@dataclass
class QueueItem:
    chat_id: int
    message_id: int
    user_id: int
    username: str
    title: str
    media_type: str          # audio / video
    file_id: str
    repeatable: bool = True


@dataclass
class PlayerState:
    queue: list[QueueItem] = field(default_factory=list)
    current: Optional[QueueItem] = None

    paused: bool = False
    muted: bool = False
    volume: int = 100

    # off / one / all
    repeat_mode: str = "off"

    control_message_id: Optional[int] = None

    playing: bool = False


# Every Telegram group has its own player.
PLAYERS: dict[int, PlayerState] = {}


# ============================================================
# Player state
# ============================================================

def get_player(chat_id: int) -> PlayerState:
    if chat_id not in PLAYERS:
        PLAYERS[chat_id] = PlayerState()

    return PLAYERS[chat_id]


def clear_player(chat_id: int):
    PLAYERS.pop(chat_id, None)


# ============================================================
# Voice Engine
# ============================================================

class VoiceEngine:
    """
    Voice Chat abstraction.

    This class is deliberately separated from the queue logic.

    Later bot.py will initialize it with the actual PyTgCalls
    instance.

    Required operations:

        join(chat_id)
        play(chat_id, file_path, media_type)
        pause(chat_id)
        resume(chat_id)
        stop(chat_id)
        mute(chat_id)
        unmute(chat_id)
        set_volume(chat_id, volume)
        leave(chat_id)
    """

    def __init__(self):
        self.client = None

    async def join(self, chat_id: int):
        """
        Join an existing Voice Chat.

        The real PyTgCalls implementation will be connected here.
        """
        raise NotImplementedError(
            "VoiceEngine.join() must be connected to PyTgCalls."
        )

    async def play(
        self,
        chat_id: int,
        file_path: str,
        media_type: str,
    ):
        """
        Start audio/video playback.
        """
        raise NotImplementedError(
            "VoiceEngine.play() must be connected to PyTgCalls."
        )

    async def pause(self, chat_id: int):
        raise NotImplementedError

    async def resume(self, chat_id: int):
        raise NotImplementedError

    async def stop(self, chat_id: int):
        raise NotImplementedError

    async def mute(self, chat_id: int):
        raise NotImplementedError

    async def unmute(self, chat_id: int):
        raise NotImplementedError

    async def set_volume(
        self,
        chat_id: int,
        volume: int,
    ):
        raise NotImplementedError

    async def leave(self, chat_id: int):
        raise NotImplementedError


voice_engine = VoiceEngine()


# ============================================================
# UI
# ============================================================

def player_keyboard(
    state: PlayerState,
) -> InlineKeyboardMarkup:

    repeat_text = {
        "off": "🔁 Repeat",
        "one": "🔂 Repeat One",
        "all": "🔁 Repeat All",
    }.get(state.repeat_mode, "🔁 Repeat")

    mute_text = (
        "🔊 Unmute"
        if state.muted
        else "🔇 Mute"
    )

    pause_text = (
        "▶️ Resume"
        if state.paused
        else "⏸ Pause"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                pause_text,
                callback_data="mp_pause",
            ),
            InlineKeyboardButton(
                "⏭ Skip",
                callback_data="mp_skip",
            ),
        ],
        [
            InlineKeyboardButton(
                repeat_text,
                callback_data="mp_repeat",
            ),
            InlineKeyboardButton(
                "🔀 Shuffle",
                callback_data="mp_shuffle",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Queue",
                callback_data="mp_queue",
            ),
            InlineKeyboardButton(
                "🎵 Now",
                callback_data="mp_now",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔉 -10",
                callback_data="mp_vol_down",
            ),
            InlineKeyboardButton(
                f"🔊 {state.volume}%",
                callback_data="mp_volume",
            ),
            InlineKeyboardButton(
                "🔊 +10",
                callback_data="mp_vol_up",
            ),
        ],
        [
            InlineKeyboardButton(
                mute_text,
                callback_data="mp_mute",
            ),
            InlineKeyboardButton(
                "⏹ Stop",
                callback_data="mp_stop",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑 Clear",
                callback_data="mp_clear",
            ),
            InlineKeyboardButton(
                "🚪 Leave",
                callback_data="mp_leave",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# Display
# ============================================================

def player_text(
    state: PlayerState,
) -> str:

    if state.current is None:
        return (
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       🎵 PLAYER\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "⏹ Nothing is playing.\n"
        )

    current = state.current

    status = "⏸ PAUSED" if state.paused else "▶️ PLAYING"

    queue_count = len(state.queue)

    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🎵 NOW PLAYING\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"{status}\n\n"
        f"{'🎬' if current.media_type == 'video' else '🎵'} "
        f"<b>{escape(current.title)}</b>\n"
        f"👤 {escape(current.username)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 Queue: <b>{queue_count}</b>\n"
        f"🔊 Volume: <b>{state.volume}%</b>\n"
        f"🔁 Repeat: <b>{state.repeat_mode}</b>"
    )


def escape(value: str) -> str:
    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# Permissions
# ============================================================

async def is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:

    chat = update.effective_chat

    if not chat:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat.id,
            user_id,
        )

        return member.status in {
            "administrator",
            "creator",
        }

    except Exception:
        return False


async def can_control_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: Optional[QueueItem],
) -> bool:

    if not item:
        return False

    user = update.effective_user

    if not user:
        return False

    if user.id == item.user_id:
        return True

    return await is_admin(
        update,
        context,
        user.id,
    )


# ============================================================
# Queue
# ============================================================

async def add_to_queue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: QueueItem,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if state.current is None:
        state.current = item
        state.playing = True

        await start_current(
            update,
            context,
        )
        return

    state.queue.append(item)

    await update.effective_message.reply_text(
        "📋 Added to queue:\n\n"
        f"{'🎬' if item.media_type == 'video' else '🎵'} "
        f"<b>{escape(item.title)}</b>\n\n"
        f"Position: <b>{len(state.queue)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def start_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if not state.current:
        return

    current = state.current

    try:
        await voice_engine.join(chat.id)

        await voice_engine.play(
            chat.id,
            current.file_id,
            current.media_type,
        )

    except NotImplementedError:
        await update.effective_message.reply_text(
            "⚠️ Voice Engine is not connected yet.\n\n"
            "PyTgCalls/MTProto must be initialized before "
            "actual Voice Chat playback can start."
        )
        return

    except Exception as exc:
        await update.effective_message.reply_text(
            "⚠️ Could not start playback.\n\n"
            f"<code>{escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await send_or_update_player(
        update,
        context,
    )


# ============================================================
# Next
# ============================================================

async def play_next(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if state.current:
        if state.repeat_mode == "one":
            await start_current(
                update,
                context,
            )
            return

    if state.queue:
        state.current = state.queue.pop(0)
        state.paused = False
        state.muted = False

        await start_current(
            update,
            context,
        )
        return

    if state.repeat_mode == "all" and state.current:
        # Repeat-all with one current item and no queue.
        await start_current(
            update,
            context,
        )
        return

    state.playing = False
    state.current = None

    await update.effective_message.reply_text(
        "✅ Queue finished."
    )


# ============================================================
# Queue display
# ============================================================

def queue_text(
    state: PlayerState,
) -> str:

    if not state.queue:
        return (
            "📋 <b>QUEUE</b>\n\n"
            "Queue is empty."
        )

    lines = [
        "📋 <b>QUEUE</b>",
        "",
    ]

    for index, item in enumerate(
        state.queue,
        start=1,
    ):
        icon = (
            "🎬"
            if item.media_type == "video"
            else "🎵"
        )

        lines.append(
            f"<b>{index}.</b> {icon} "
            f"{escape(item.title)}"
        )

    return "\n".join(lines)


# ============================================================
# Control operations
# ============================================================

async def pause_player(
    update: Update,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if not state.current:
        return

    await voice_engine.pause(chat.id)

    state.paused = True


async def resume_player(
    update: Update,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if not state.current:
        return

    await voice_engine.resume(chat.id)

    state.paused = False


async def stop_player(
    update: Update,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    if not state.current:
        return

    await voice_engine.stop(chat.id)

    state.playing = False
    state.current = None


async def leave_player(
    update: Update,
):
    chat = update.effective_chat

    if not chat:
        return

    try:
        await voice_engine.stop(chat.id)
    except Exception:
        pass

    try:
        await voice_engine.leave(chat.id)
    except Exception:
        pass

    clear_player(chat.id)


def cycle_repeat(
    state: PlayerState,
):
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


# ============================================================
# Control message
# ============================================================

async def send_or_update_player(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    chat = update.effective_chat

    if not chat:
        return

    state = get_player(chat.id)

    text = player_text(state)
    keyboard = player_keyboard(state)

    if state.control_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id,
                message_id=state.control_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception:
            state.control_message_id = None

    message = await context.bot.send_message(
        chat.id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

    state.control_message_id = message.message_id


# ============================================================
# Commands
# ============================================================

COMMANDS = {
    "play",
    "شغل",
    "تشغيل",

    "video",
    "فيديو",

    "pause",
    "وقف",

    "resume",
    "كمل",

    "skip",
    "next",
    "تالي",

    "stop",
    "وقفه",

    "leave",
    "انهاء",

    "queue",
    "قائمة",

    "now",
    "الآن",

    "repeat",
    "كرر",

    "shuffle",
    "خلط",

    "clear",
    "مسح",

    "mute",
    "كتم",

    "unmute",
    "صوت",
}


# ============================================================
# Button handler
# ============================================================

async def player_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    message = query.message

    if not message:
        return

    chat_id = message.chat.id
    state = get_player(chat_id)

    user_id = query.from_user.id

    callback = query.data

    # --------------------------------------------------------
    # Current item owner
    # --------------------------------------------------------

    item_owner = (
        state.current.user_id
        if state.current
        else None
    )

    owner = (
        item_owner == user_id
        if item_owner
        else False
    )

    admin = False

    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
        )

        admin = member.status in {
            "administrator",
            "creator",
        }

    except Exception:
        pass

    # --------------------------------------------------------
    # Pause / Resume
    # --------------------------------------------------------

    if callback == "mp_pause":

        if not owner and not admin:
            await query.answer(
                "You can control only your own track.",
                show_alert=True,
            )
            return

        await pause_player(update)

    elif callback == "mp_resume":

        if not owner and not admin:
            await query.answer(
                "You can control only your own track.",
                show_alert=True,
            )
            return

        await resume_player(update)

    # --------------------------------------------------------
    # Skip
    # --------------------------------------------------------

    elif callback == "mp_skip":

        if not admin:
            await query.answer(
                "Only group admins can skip tracks.",
                show_alert=True,
            )
            return

        await play_next(
            update,
            context,
        )

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    elif callback == "mp_stop":

        if not owner and not admin:
            await query.answer(
                "You can stop only your own track.",
                show_alert=True,
            )
            return

        await stop_player(update)

    # --------------------------------------------------------
    # Repeat
    # --------------------------------------------------------

    elif callback == "mp_repeat":

        if not owner and not admin:
            await query.answer(
                "You can control only your own track.",
                show_alert=True,
            )
            return

        cycle_repeat(state)

    # --------------------------------------------------------
    # Shuffle
    # --------------------------------------------------------

    elif callback == "mp_shuffle":

        if not admin:
            await query.answer(
                "Only group admins can shuffle the queue.",
                show_alert=True,
            )
            return

        random.shuffle(state.queue)

    # --------------------------------------------------------
    # Queue
    # --------------------------------------------------------

    elif callback == "mp_queue":

        await query.answer()

        await query.message.reply_text(
            queue_text(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # Now
    # --------------------------------------------------------

    elif callback == "mp_now":

        await query.answer()

        await query.message.reply_text(
            player_text(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # Mute
    # --------------------------------------------------------

    elif callback == "mp_mute":

        if not admin:
            await query.answer(
                "Only group admins can mute the player.",
                show_alert=True,
            )
            return

        if state.muted:
            await voice_engine.unmute(chat_id)
            state.muted = False
        else:
            await voice_engine.mute(chat_id)
            state.muted = True

    # --------------------------------------------------------
    # Volume down
    # --------------------------------------------------------

    elif callback == "mp_vol_down":

        if not admin:
            await query.answer(
                "Only group admins can change volume.",
                show_alert=True,
            )
            return

        state.volume = max(
            0,
            state.volume - 10,
        )

        await voice_engine.set_volume(
            chat_id,
            state.volume,
        )

    # --------------------------------------------------------
    # Volume up
    # --------------------------------------------------------

    elif callback == "mp_vol_up":

        if not admin:
            await query.answer(
                "Only group admins can change volume.",
                show_alert=True,
            )
            return

        state.volume = min(
            200,
            state.volume + 10,
        )

        await voice_engine.set_volume(
            chat_id,
            state.volume,
        )

    # --------------------------------------------------------
    # Clear
    # --------------------------------------------------------

    elif callback == "mp_clear":

        if not admin:
            await query.answer(
                "Only group admins can clear the queue.",
                show_alert=True,
            )
            return

        state.queue.clear()

    # --------------------------------------------------------
    # Leave
    # --------------------------------------------------------

    elif callback == "mp_leave":

        if not admin:
            await query.answer(
                "Only group admins can make the player leave.",
                show_alert=True,
            )
            return

        await leave_player(update)

        await query.message.edit_text(
            "🚪 Player left the Voice Chat."
        )
        return

    # --------------------------------------------------------
    # Update control message
    # --------------------------------------------------------

    try:
        await query.message.edit_text(
            player_text(state),
            parse_mode=ParseMode.HTML,
            reply_markup=player_keyboard(state),
        )

        state.control_message_id = (
            query.message.message_id
        )

    except Exception:
        pass


# ============================================================
# Text command handler
# ============================================================

async def player_text_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    text = (
        message.text
        or ""
    ).strip().lower()

    if text not in COMMANDS:
        return

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    state = get_player(chat.id)

    # --------------------------------------------------------
    # PLAY
    #
    # The actual media extraction/download is intentionally
    # outside this file.
    #
    # Here we expect play to be used as a reply to an audio
    # or video Telegram message.
    # --------------------------------------------------------

    if text in {
        "play",
        "شغل",
        "تشغيل",
        "video",
        "فيديو",
    }:

        reply = message.reply_to_message

        if not reply:
            await message.reply_text(
                "🎵 Reply to an audio or video and use "
                "<b>play</b>.",
                parse_mode=ParseMode.HTML,
            )
            return

        file_id = None
        media_type = None
        title = "Unknown"

        if reply.audio:
            file_id = reply.audio.file_id
            media_type = "audio"
            title = (
                reply.audio.title
                or reply.audio.file_name
                or "Audio"
            )

        elif reply.voice:
            file_id = reply.voice.file_id
            media_type = "audio"
            title = "Voice"

        elif reply.video:
            file_id = reply.video.file_id
            media_type = "video"
            title = (
                reply.video.file_name
                or "Video"
            )

        elif reply.document:
            mime = (
                reply.document.mime_type
                or ""
            )

            if mime.startswith("audio/"):
                file_id = reply.document.file_id
                media_type = "audio"
                title = (
                    reply.document.file_name
                    or "Audio"
                )

            elif mime.startswith("video/"):
                file_id = reply.document.file_id
                media_type = "video"
                title = (
                    reply.document.file_name
                    or "Video"
                )

        if not file_id:
            await message.reply_text(
                "⚠️ Reply to an audio or video file."
            )
            return

        # "video" forces video playback.
        if text in {"video", "فيديو"}:
            media_type = "video"

        item = QueueItem(
            chat_id=chat.id,
            message_id=reply.message_id,
            user_id=user.id,
            username=(
                user.username
                or user.first_name
                or "User"
            ),
            title=title,
            media_type=media_type,
            file_id=file_id,
        )

        await add_to_queue(
            update,
            context,
            item,
        )
        return

    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    if text in {"pause", "وقف"}:

        if not state.current:
            return

        if not await can_control_item(
            update,
            context,
            state.current,
        ):
            await message.reply_text(
                "⛔ You can control only your own track."
            )
            return

        await pause_player(update)

        await send_or_update_player(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    if text in {"resume", "كمل"}:

        if not state.current:
            return

        if not await can_control_item(
            update,
            context,
            state.current,
        ):
            await message.reply_text(
                "⛔ You can control only your own track."
            )
            return

        await resume_player(update)

        await send_or_update_player(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # SKIP
    # --------------------------------------------------------

    if text in {
        "skip",
        "next",
        "تالي",
    }:

        if not await is_admin(
            update,
            context,
            user.id,
        ):
            await message.reply_text(
                "🛡️ Only group admins can skip tracks."
            )
            return

        await play_next(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    if text in {
        "stop",
        "وقفه",
    }:

        if not state.current:
            return

        if not await can_control_item(
            update,
            context,
            state.current,
        ):
            await message.reply_text(
                "⛔ You can stop only your own track."
            )
            return

        await stop_player(update)

        await message.reply_text(
            "⏹ Stopped."
        )
        return

    # --------------------------------------------------------
    # LEAVE
    # --------------------------------------------------------

    if text in {
        "leave",
        "انهاء",
    }:

        if not await is_admin(
            update,
            context,
            user.id,
        ):
            await message.reply_text(
                "🛡️ Only group admins can use Leave."
            )
            return

        await leave_player(update)

        await message.reply_text(
            "🚪 Left the Voice Chat."
        )
        return

    # --------------------------------------------------------
    # QUEUE
    # --------------------------------------------------------

    if text in {
        "queue",
        "قائمة",
    }:

        await message.reply_text(
            queue_text(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # NOW
    # --------------------------------------------------------

    if text in {
        "now",
        "الآن",
    }:

        await message.reply_text(
            player_text(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # REPEAT
    # --------------------------------------------------------

    if text in {
        "repeat",
        "كرر",
    }:

        if not state.current:
            return

        if not await can_control_item(
            update,
            context,
            state.current,
        ):
            await message.reply_text(
                "⛔ You can control only your own track."
            )
            return

        cycle_repeat(state)

        await send_or_update_player(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # SHUFFLE
    # --------------------------------------------------------

    if text in {
        "shuffle",
        "خلط",
    }:

        if not await is_admin(
            update,
            context,
            user.id,
        ):
            await message.reply_text(
                "🛡️ Only group admins can shuffle."
            )
            return

        random.shuffle(state.queue)

        await send_or_update_player(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # CLEAR
    # --------------------------------------------------------

    if text in {
        "clear",
        "مسح",
    }:

        if not await is_admin(
            update,
            context,
            user.id,
        ):
            await message.reply_text(
                "🛡️ Only group admins can clear the queue."
            )
            return

        state.queue.clear()

        await send_or_update_player(
            update,
            context,
        )
        return


# ============================================================
# Registration
# ============================================================

def register_media_play(
    application,
):
    """
    Add all Media Player handlers.

    bot.py will eventually only need:

        from media_play import register_media_play

        register_media_play(application)
    """

    application.add_handler(
        CommandHandler(
            [
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
            ],
            player_text_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            player_text_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            player_button,
            pattern=r"^mp_",
        )
  )
