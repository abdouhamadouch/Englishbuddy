# sounds.py
# FixMyEnglish - Vowel / Sound Analysis
#
# Commands:
# /sounds
# /sound
# أصوات
# صوت
#
# Supports:
# - Direct word / sentence
# - Reply to a message containing a word or sentence
# - American / British pronunciation
# - Sound analysis with IPA
# - Training audio
# - User-specific sessions
#
# Audio is intentionally slow for learning.

import asyncio
import json
import os
import re
import tempfile
import time
from html import escape

import edge_tts
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

SESSION_TIMEOUT = 20 * 60

DEFAULT_VOICE_US = "en-US-AriaNeural"
DEFAULT_VOICE_UK = "en-GB-SoniaNeural"

# Slow educational pronunciation
TTS_RATE = "-25%"

MAX_INPUT_LENGTH = 500


# =========================================================
# AI FUNCTION
# =========================================================

_ai_function = None


def set_ai_function(ai_function):
    """
    Inject the main AI function from bot.py.
    """
    global _ai_function
    _ai_function = ai_function


# =========================================================
# SESSIONS
# =========================================================

sessions = {}


def _cleanup_sessions():
    now = time.time()

    expired = [
        user_id
        for user_id, session in sessions.items()
        if now - session.get("created_at", 0) > SESSION_TIMEOUT
    ]

    for user_id in expired:
        sessions.pop(user_id, None)


def _save_session(user_id, data):
    _cleanup_sessions()

    sessions[user_id] = {
        "created_at": time.time(),
        "data": data,
    }


def _get_session(user_id):
    _cleanup_sessions()

    session = sessions.get(user_id)

    if not session:
        return None

    return session.get("data")


# =========================================================
# EXTRACT TEXT
# =========================================================

def _extract_text(update: Update):
    """
    Gets text either from command arguments or from
    the replied-to message.
    """

    message = update.effective_message

    if not message:
        return None

    # Direct command argument
    if message.text:
        text = message.text.strip()

        parts = text.split(maxsplit=1)

        if len(parts) == 2:
            return parts[1].strip()

    # Reply
    if message.reply_to_message:
        replied = message.reply_to_message

        if replied.text:
            return replied.text.strip()

        if replied.caption:
            return replied.caption.strip()

    return None


# =========================================================
# CLEAN AI JSON
# =========================================================

def _extract_json(text):
    if not text:
        return None

    text = text.strip()

    # Remove markdown code fences
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    text = text.strip()

    # Direct JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Find JSON object inside response
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)

    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return None


# =========================================================
# AI ANALYSIS
# =========================================================

async def _analyze_with_ai(text):
    if not _ai_function:
        return None

    prompt = f"""
Analyze the English word or sentence below specifically for
IMPORTANT VOWEL SOUNDS and pronunciation.

Input:
{text}

Your job is to identify the important vowel sounds that actually
occur in the English pronunciation.

Focus on useful learning information.

For each important sound:
- Give the IPA sound.
- Give its common sound name, such as "short i", "long e",
  "schwa", etc.
- Say where it occurs in the target word.
- Give the exact part of the target word containing that sound.
- Give 2 or 3 simple example words containing the same sound.
- Give IPA for each example.
- Give a small list of training words.

Do not invent sounds that are not present.

If the input is a sentence, focus mainly on the important vowel
sounds in the meaningful target words.

Return ONLY valid JSON.

Use exactly this structure:

{{
  "target": "target word",
  "accent": "American",
  "full_ipa": "/.../",
  "sounds": [
    {{
      "sound": "/.../",
      "name": "sound name",
      "position": "beginning/middle/end",
      "target_part": "part of word",
      "examples": [
        {{
          "word": "example",
          "ipa": "/.../"
        }},
        {{
          "word": "example",
          "ipa": "/.../"
        }}
      ]
    }}
  ],
  "training_words": [
    "word1",
    "word2",
    "word3"
  ]
}}

Important:
- Keep the number of important sounds reasonable.
- Do not include consonant sounds unless necessary to explain
  the vowel sound.
- Use standard English IPA.
- Do not write explanations outside the JSON.
"""

    try:
        result = await _ai_function(prompt)

    except Exception:
        return None

    return _extract_json(result)


# =========================================================
# VALIDATION
# =========================================================

def _validate_analysis(data):
    if not isinstance(data, dict):
        return False

    if not data.get("target"):
        return False

    if not data.get("sounds"):
        return False

    if not isinstance(data.get("sounds"), list):
        return False

    return True


# =========================================================
# FORMAT ANALYSIS
# =========================================================

def _format_analysis(data):
    target = escape(str(data.get("target", "")))
    accent = escape(str(data.get("accent", "American")))
    full_ipa = escape(str(data.get("full_ipa", "")))

    lines = []

    lines.append(f"🔊 <b>Sound Analysis</b>")
    lines.append("")
    lines.append(f"<b>Word:</b> {target}")

    if full_ipa:
        lines.append(f"<b>IPA:</b> <code>{full_ipa}</code>")

    lines.append(f"<b>Accent:</b> {accent}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")

    sounds = data.get("sounds", [])

    for index, item in enumerate(sounds, start=1):
        if not isinstance(item, dict):
            continue

        sound = escape(str(item.get("sound", "")))
        name = escape(str(item.get("name", "")))
        position = escape(str(item.get("position", "")))
        target_part = escape(str(item.get("target_part", "")))

        lines.append("")
        lines.append(f"<b>Sound {index}</b>")

        if sound:
            lines.append(f"🔹 <b>Sound:</b> <code>{sound}</code>")

        if name:
            lines.append(f"🔹 <b>Name:</b> {name}")

        if position:
            lines.append(f"🔹 <b>Position:</b> {position}")

        if target_part:
            lines.append(
                f"🔹 <b>In the word:</b> "
                f"<code>{target_part}</code>"
            )

        examples = item.get("examples", [])

        if examples:
            lines.append("")
            lines.append("<b>Examples:</b>")

            for example in examples:
                if not isinstance(example, dict):
                    continue

                word = escape(str(example.get("word", "")))
                ipa = escape(str(example.get("ipa", "")))

                if word:
                    if ipa:
                        lines.append(
                            f"• <b>{word}</b> "
                            f"<code>{ipa}</code>"
                        )
                    else:
                        lines.append(f"• <b>{word}</b>")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")

    training_words = data.get("training_words", [])

    if training_words:
        lines.append("")
        lines.append("<b>🎧 Training Words</b>")
        lines.append("")

        clean_words = []

        for word in training_words:
            if word:
                clean_words.append(
                    escape(str(word))
                )

        if clean_words:
            lines.append(" • ".join(clean_words))

    return "\n".join(lines)


# =========================================================
# KEYBOARD
# =========================================================

def _main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🇺🇸 American",
                    callback_data="snd_us",
                ),
                InlineKeyboardButton(
                    "🇬🇧 British",
                    callback_data="snd_uk",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎧 Slow Training Audio",
                    callback_data="snd_audio",
                ),
            ],
        ]
    )


# =========================================================
# TARGET WORD
# =========================================================

def _get_target_word(data):
    target = data.get("target", "")

    if not target:
        return ""

    # Keep first meaningful word if AI accidentally returns sentence
    match = re.search(
        r"[A-Za-z][A-Za-z'-]*",
        str(target),
    )

    if match:
        return match.group(0)

    return str(target)


# =========================================================
# AUDIO TEXT
# =========================================================

def _build_audio_text(data):
    """
    Creates a slow educational pronunciation sequence.

    We intentionally do not send raw IPA symbols to TTS because
    normal TTS engines do not reliably pronounce IPA phonemes.
    Instead, the audio teaches the sound through example words.
    """

    target = _get_target_word(data)

    words = []

    for item in data.get("sounds", []):
        if not isinstance(item, dict):
            continue

        examples = item.get("examples", [])

        for example in examples:
            if not isinstance(example, dict):
                continue

            word = str(example.get("word", "")).strip()

            if word:
                words.append(word)

    training_words = data.get("training_words", [])

    for word in training_words:
        word = str(word).strip()

        if word:
            words.append(word)

    # Remove duplicates while preserving order
    unique_words = []

    for word in words:
        normalized = word.lower()

        if normalized not in {
            x.lower() for x in unique_words
        }:
            unique_words.append(word)

    # Limit training sequence
    unique_words = unique_words[:8]

    parts = []

    parts.append("Listen carefully.")

    if unique_words:
        for word in unique_words:
            parts.append(f"{word}.")
            parts.append("Pause.")

    if target:
        parts.append("Now listen to the target word.")
        parts.append(f"{target}.")
        parts.append("Again.")
        parts.append(f"{target}.")

    return " ".join(parts)


# =========================================================
# GENERATE AUDIO
# =========================================================

async def _generate_audio(data, accent="American"):
    target = _get_target_word(data)

    if not target:
        return None

    text = _build_audio_text(data)

    if accent == "British":
        voice = DEFAULT_VOICE_UK
    else:
        voice = DEFAULT_VOICE_US

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".mp3",
        delete=False,
    )

    temp_path = temp_file.name
    temp_file.close()

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=TTS_RATE,
        )

        await communicate.save(temp_path)

        return temp_path

    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        return None


# =========================================================
# SEND RESULT
# =========================================================

async def _send_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data,
):
    message = update.effective_message

    if not message:
        return

    formatted = _format_analysis(data)

    try:
        await message.reply_text(
            formatted,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    except Exception:
        # Safe fallback without HTML
        plain = re.sub(
            r"<[^>]+>",
            "",
            formatted,
        )

        await message.reply_text(
            plain,
            reply_markup=_main_keyboard(),
        )


# =========================================================
# MAIN COMMAND
# =========================================================

async def sounds_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = _extract_text(update)

    if not text:
        await update.effective_message.reply_text(
            "🔊 Send a word or sentence after /sounds "
            "or reply to a message with /sounds."
        )
        return

    text = text.strip()

    if len(text) > MAX_INPUT_LENGTH:
        await update.effective_message.reply_text(
            "Please send a shorter word or sentence."
        )
        return

    data = await _analyze_with_ai(text)

    if not _validate_analysis(data):
        await update.effective_message.reply_text(
            "I couldn't analyze the vowel sounds right now. "
            "Please try again."
        )
        return

    # Default accent
    data["selected_accent"] = "American"
    data["original_input"] = text

    _save_session(
        update.effective_user.id,
        data,
    )

    await _send_analysis(
        update,
        context,
        data,
    )


# =========================================================
# ARABIC TEXT COMMAND
# =========================================================

async def sounds_text_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    match = re.match(
        r"^(?:أصوات|صوت)(?:\s+(.+))?$",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return

    argument = match.group(1)

    if argument:
        input_text = argument.strip()

    elif message.reply_to_message:
        replied = message.reply_to_message

        input_text = (
            replied.text
            or replied.caption
            or ""
        ).strip()

    else:
        await message.reply_text(
            "🔊 اكتب الكلمة أو الجملة بعد الأمر، "
            "أو استعمل الأمر بالرد على رسالة."
        )
        return

    if not input_text:
        await message.reply_text(
            "لم أجد كلمة أو جملة لتحليلها."
        )
        return

    if len(input_text) > MAX_INPUT_LENGTH:
        await message.reply_text(
            "أرسل كلمة أو جملة أقصر."
        )
        return

    data = await _analyze_with_ai(input_text)

    if not _validate_analysis(data):
        await message.reply_text(
            "تعذر تحليل الأصوات الآن. حاول مرة أخرى."
        )
        return

    data["selected_accent"] = "American"
    data["original_input"] = input_text

    _save_session(
        update.effective_user.id,
        data,
    )

    await _send_analysis(
        update,
        context,
        data,
    )


# =========================================================
# CALLBACK: ACCENT
# =========================================================

async def _accent_callback(
    query,
    context,
    accent,
):
    user_id = query.from_user.id

    data = _get_session(user_id)

    if not data:
        await query.answer(
            "⏳ This analysis session has expired.",
            show_alert=True,
        )
        return

    data["selected_accent"] = accent

    _save_session(
        user_id,
        data,
    )

    await query.answer(
        f"{accent} pronunciation selected."
    )

    # Edit the existing message only.
    try:
        await query.edit_message_reply_markup(
            reply_markup=_main_keyboard(),
        )
    except Exception:
        pass


# =========================================================
# CALLBACK: AUDIO
# =========================================================

async def _audio_callback(
    query,
    context,
):
    user_id = query.from_user.id

    data = _get_session(user_id)

    if not data:
        await query.answer(
            "⏳ This analysis session has expired.",
            show_alert=True,
        )
        return

    await query.answer(
        "🎧 Preparing slow pronunciation..."
    )

    accent = data.get(
        "selected_accent",
        "American",
    )

    audio_path = await _generate_audio(
        data,
        accent,
    )

    if not audio_path:
        await query.message.reply_text(
            "I couldn't generate the pronunciation audio."
        )
        return

    try:
        with open(audio_path, "rb") as audio:
            await query.message.reply_voice(
                voice=audio,
                caption=(
                    f"🎧 Slow {accent} pronunciation\n"
                    f"Target: {_get_target_word(data)}"
                ),
            )

    except Exception:
        try:
            with open(audio_path, "rb") as audio:
                await query.message.reply_audio(
                    audio=audio,
                    caption=(
                        f"🎧 Slow {accent} pronunciation\n"
                        f"Target: {_get_target_word(data)}"
                    ),
                )
        except Exception:
            pass

    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def sounds_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    if data == "snd_us":
        await _accent_callback(
            query,
            context,
            "American",
        )
        return

    if data == "snd_uk":
        await _accent_callback(
            query,
            context,
            "British",
        )
        return

    if data == "snd_audio":
        await _audio_callback(
            query,
            context,
        )
        return


# =========================================================
# REGISTER HANDLERS
# =========================================================

def register_sounds_handlers(application):
    """
    Register all sound-analysis handlers.
    """

    # Slash commands
    application.add_handler(
        CommandHandler(
            ["sounds", "sound"],
            sounds_command,
        )
    )

    # Arabic commands
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(
                r"^(?:أصوات|صوت)(?:\s+.+)?$"
            ),
            sounds_text_command,
        )
    )

    # Buttons
    application.add_handler(
        CallbackQueryHandler(
            sounds_callback,
            pattern=r"^snd_(us|uk|audio)$",
        )
      )
