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
# - Slow training audio
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
TTS_RATE = "-30%"

MAX_INPUT_LENGTH = 500

# Maximum attempts for AI analysis
MAX_AI_ATTEMPTS = 4

# Keep AI output reasonably small
SOUNDS_MAX_TOKENS = 1600


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


def _save_session(user_id, data, created_at=None):
    """
    Save session without unnecessarily extending its lifetime.

    If created_at is supplied, keep the original session time.
    """
    _cleanup_sessions()

    if created_at is None:
        created_at = time.time()

    sessions[user_id] = {
        "created_at": created_at,
        "data": data,
    }


def _get_session_record(user_id):
    _cleanup_sessions()

    return sessions.get(user_id)


def _get_session(user_id):
    session = _get_session_record(user_id)

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

    if not isinstance(text, str):
        text = str(text)

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
        flags=re.IGNORECASE,
    )

    text = text.strip()

    # Direct JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Find JSON object inside response
    match = re.search(
        r"\{[\s\S]*\}",
        text,
    )

    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return None


# =========================================================
# AI CALL
# =========================================================

async def _call_ai(prompt):
    if not _ai_function:
        return None

    try:
        # Preferred call
        try:
            return await _ai_function(
                prompt,
                max_tokens=SOUNDS_MAX_TOKENS,
            )
        except TypeError:
            # Compatibility with older bot.py AI functions
            return await _ai_function(prompt)

    except Exception:
        return None


# =========================================================
# AI ANALYSIS
# =========================================================

async def _analyze_with_ai(text, accent="American"):
    """
    Analyze the target specifically for the selected accent.

    American and British analyses are generated separately.
    """

    if not _ai_function:
        return None

    if accent not in ("American", "British"):
        accent = "American"

    if accent == "American":
        accent_description = """
Use standard General American English pronunciation.
Use General American IPA where appropriate.
"""
    else:
        accent_description = """
Use standard modern British English pronunciation.
Use standard British IPA where appropriate.
"""

    prompt = f"""
Analyze the English word or sentence below specifically for
IMPORTANT VOWEL SOUNDS and pronunciation.

Target accent:
{accent}

{accent_description}

Input:
{text}

Your job is to identify the important vowel sounds that actually
occur in the English pronunciation of this target.

For each important vowel sound:
- Give the IPA sound.
- Give its common learning name, such as "short i",
  "long e", "schwa", "short a", etc.
- Say where it occurs: beginning, middle, or end.
- Give the exact part of the target containing that sound.
- Give 2 simple example words containing the same sound.
- Give IPA for each example.
- Give a small list of useful training words.

Important:
- Analyze the selected accent, not the other accent.
- American and British pronunciation can have different vowel
  sounds and IPA. Reflect those differences when they actually
  occur.
- Do not invent sounds.
- Do not list every vowel in the word unnecessarily.
- Focus on the most useful sounds for a learner.
- If the input is a sentence, focus mainly on important words.
- Do not include consonant sounds unless they are necessary
  to identify the vowel sound.
- Use standard English IPA.
- Keep the response concise.

Return ONLY valid JSON.

Use exactly this structure:

{{
  "target": "target word",
  "accent": "{accent}",
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

Do not write explanations outside the JSON.
"""

    for _ in range(MAX_AI_ATTEMPTS):
        result = await _call_ai(prompt)

        data = _extract_json(result)

        if _validate_analysis(data):
            # Force the selected accent if AI returned a wrong label.
            data["accent"] = accent
            return data

        await asyncio.sleep(0.4)

    return None


# =========================================================
# VALIDATION
# =========================================================

def _validate_analysis(data):
    if not isinstance(data, dict):
        return False

    target = data.get("target")

    if not target or not str(target).strip():
        return False

    sounds = data.get("sounds")

    if not isinstance(sounds, list) or not sounds:
        return False

    valid_sound_count = 0

    for item in sounds:
        if not isinstance(item, dict):
            continue

        sound = item.get("sound")

        if not sound or not str(sound).strip():
            continue

        valid_sound_count += 1

        examples = item.get("examples", [])

        if not isinstance(examples, list):
            continue

        # We do not reject the whole analysis if examples are missing.
        # The important requirement is a valid sound.

    return valid_sound_count > 0


# =========================================================
# FORMAT ANALYSIS
# =========================================================

def _format_analysis(data):
    target = escape(str(data.get("target", "")))
    accent = escape(str(data.get("accent", "American")))
    full_ipa = escape(str(data.get("full_ipa", "")))

    lines = []

    lines.append("🔊 <b>Sound Analysis</b>")
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
            lines.append(
                f"🔹 <b>Sound:</b> <code>{sound}</code>"
            )

        if name:
            lines.append(
                f"🔹 <b>Name:</b> {name}"
            )

        if position:
            lines.append(
                f"🔹 <b>Position:</b> {position}"
            )

        if target_part:
            lines.append(
                f"🔹 <b>In the word:</b> "
                f"<code>{target_part}</code>"
            )

        examples = item.get("examples", [])

        if isinstance(examples, list) and examples:
            valid_examples = []

            for example in examples:
                if not isinstance(example, dict):
                    continue

                word = str(
                    example.get("word", "")
                ).strip()

                if not word:
                    continue

                ipa = str(
                    example.get("ipa", "")
                ).strip()

                valid_examples.append(
                    (word, ipa)
                )

            if valid_examples:
                lines.append("")
                lines.append("<b>Examples:</b>")

                for word, ipa in valid_examples[:3]:
                    safe_word = escape(word)
                    safe_ipa = escape(ipa)

                    if safe_ipa:
                        lines.append(
                            f"• <b>{safe_word}</b> "
                            f"<code>{safe_ipa}</code>"
                        )
                    else:
                        lines.append(
                            f"• <b>{safe_word}</b>"
                        )

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")

    training_words = data.get(
        "training_words",
        [],
    )

    if isinstance(training_words, list):
        clean_words = []

        for word in training_words:
            word = str(word).strip()

            if not word:
                continue

            if word.lower() in {
                x.lower()
                for x in clean_words
            }:
                continue

            clean_words.append(
                escape(word)
            )

        if clean_words:
            lines.append("")
            lines.append("<b>🎧 Training Words</b>")
            lines.append("")
            lines.append(
                " • ".join(clean_words[:8])
            )

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

    # Keep first meaningful English word if AI returns a sentence
    match = re.search(
        r"[A-Za-z][A-Za-z'-]*",
        str(target),
    )

    if match:
        return match.group(0)

    return str(target).strip()


# =========================================================
# CLEAN WORD FOR TTS
# =========================================================

def _clean_tts_word(word):
    """
    Keep only the actual word.

    TTS must not receive punctuation such as:
    . , ! ? : ; ( ) [ ]

    Apostrophes and hyphens inside words are preserved.
    """

    word = str(word).strip()

    if not word:
        return ""

    # Remove punctuation from beginning/end
    word = re.sub(
        r"^[^\w'-]+",
        "",
        word,
        flags=re.UNICODE,
    )

    word = re.sub(
        r"[^\w'-]+$",
        "",
        word,
        flags=re.UNICODE,
    )

    # Remove remaining punctuation that is not useful for the word
    word = re.sub(
        r"[^\w\s'-]",
        "",
        word,
        flags=re.UNICODE,
    )

    # Normalize whitespace
    word = re.sub(
        r"\s+",
        " ",
        word,
    ).strip()

    return word


# =========================================================
# AUDIO TEXT
# =========================================================

def _build_audio_text(data):
    """
    Build a TTS sequence containing WORDS ONLY.

    No commas.
    No full stops.
    No "Pause".
    No "Listen carefully".
    No explanatory sentences.

    The TTS engine therefore focuses directly on the words.
    """

    target = _clean_tts_word(
        _get_target_word(data)
    )

    words = []

    # Examples
    for item in data.get("sounds", []):
        if not isinstance(item, dict):
            continue

        examples = item.get("examples", [])

        if not isinstance(examples, list):
            continue

        for example in examples:
            if not isinstance(example, dict):
                continue

            word = _clean_tts_word(
                example.get("word", "")
            )

            if word:
                words.append(word)

    # Training words
    training_words = data.get(
        "training_words",
        [],
    )

    if isinstance(training_words, list):
        for word in training_words:
            word = _clean_tts_word(word)

            if word:
                words.append(word)

    # Remove duplicates
    unique_words = []
    seen = set()

    for word in words:
        normalized = word.lower()

        if normalized in seen:
            continue

        seen.add(normalized)
        unique_words.append(word)

    # Limit training sequence
    unique_words = unique_words[:8]

    # Add target at the end
    if target:
        target_normalized = target.lower()

        # Target can still be repeated intentionally at the end.
        if target_normalized in seen:
            pass

        unique_words.append(target)
        unique_words.append(target)

    # IMPORTANT:
    # Join with spaces only.
    # No commas, periods, colons, "Pause", etc.
    return " ".join(unique_words)


# =========================================================
# GENERATE AUDIO
# =========================================================

async def _generate_audio(data, accent="American"):
    target = _get_target_word(data)

    if not target:
        return None

    text = _build_audio_text(data)

    if not text:
        return None

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

    # Telegram message limit protection
    if len(formatted) > 3900:
        formatted = formatted[:3900].rstrip() + "\n\n…"

    try:
        await message.reply_text(
            formatted,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    except Exception:
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

    data = await _analyze_with_ai(
        text,
        "American",
    )

    if not _validate_analysis(data):
        await update.effective_message.reply_text(
            "I couldn't analyze the vowel sounds right now. "
            "Please try again."
        )
        return

    # Store analyses for both accents.
    # American is generated immediately.
    data["accent"] = "American"

    session_data = {
        "original_input": text,
        "selected_accent": "American",
        "analyses": {
            "American": data,
        },
    }

    _save_session(
        update.effective_user.id,
        session_data,
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

    data = await _analyze_with_ai(
        input_text,
        "American",
    )

    if not _validate_analysis(data):
        await message.reply_text(
            "تعذر تحليل الأصوات الآن. حاول مرة أخرى."
        )
        return

    data["accent"] = "American"

    session_data = {
        "original_input": input_text,
        "selected_accent": "American",
        "analyses": {
            "American": data,
        },
    }

    _save_session(
        update.effective_user.id,
        session_data,
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

    session_record = _get_session_record(
        user_id
    )

    if not session_record:
        await query.answer(
            "⏳ This analysis session has expired.",
            show_alert=True,
        )
        return

    data = session_record.get("data")

    if not isinstance(data, dict):
        await query.answer(
            "⏳ This analysis session has expired.",
            show_alert=True,
        )
        return

    original_input = data.get(
        "original_input",
        "",
    ).strip()

    if not original_input:
        await query.answer(
            "The original word is no longer available.",
            show_alert=True,
        )
        return

    # Answer immediately so Telegram does not keep
    # the button spinner while AI is working.
    await query.answer(
        f"Analyzing {accent} pronunciation..."
    )

    analyses = data.setdefault(
        "analyses",
        {},
    )

    # If this accent has already been analyzed,
    # use the cached result.
    selected_data = analyses.get(accent)

    if not _validate_analysis(selected_data):
        selected_data = await _analyze_with_ai(
            original_input,
            accent,
        )

        if not _validate_analysis(selected_data):
            try:
                await query.message.reply_text(
                    f"I couldn't analyze the {accent} "
                    "vowel sounds right now. Please try again."
                )
            except Exception:
                pass

            return

        selected_data["accent"] = accent

        analyses[accent] = selected_data

    # Update selected accent.
    data["selected_accent"] = accent
    data["analyses"] = analyses

    # IMPORTANT:
    # Preserve the original created_at.
    created_at = session_record.get(
        "created_at",
        time.time(),
    )

    _save_session(
        user_id,
        data,
        created_at=created_at,
    )

    formatted = _format_analysis(
        selected_data
    )

    if len(formatted) > 3900:
        formatted = formatted[:3900].rstrip() + "\n\n…"

    # Update the SAME analysis message.
    try:
        await query.edit_message_text(
            text=formatted,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    except Exception:
        # If Telegram says the message is already identical,
        # there is nothing to change.
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

    session_record = _get_session_record(
        user_id
    )

    if not session_record:
        await query.answer(
            "⏳ This analysis session has expired.",
            show_alert=True,
        )
        return

    data = session_record.get("data")

    if not isinstance(data, dict):
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

    # Use the selected accent's analysis.
    analyses = data.get(
        "analyses",
        {},
    )

    selected_data = analyses.get(accent)

    if not _validate_analysis(selected_data):
        selected_data = data

    audio_path = await _generate_audio(
        selected_data,
        accent,
    )

    if not audio_path:
        try:
            await query.message.reply_text(
                "I couldn't generate the pronunciation audio."
            )
        except Exception:
            pass

        return

    target = _get_target_word(
        selected_data
    )

    try:
        # MP3 should be sent as audio, not voice.
        # This avoids relying on Telegram voice-note
        # conversion for an MP3 file.
        with open(audio_path, "rb") as audio:
            await query.message.reply_audio(
                audio=audio,
                caption=(
                    f"🎧 Slow {accent} pronunciation\n"
                    f"Target: {target}"
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
