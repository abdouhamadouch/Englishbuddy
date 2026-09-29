import asyncio
import html
import json
import os
import re
import tempfile
import time

import edge_tts

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
# SETTINGS
# ============================================================

SESSION_TIMEOUT = 20 * 60

US_VOICE = "en-US-AriaNeural"
UK_VOICE = "en-GB-SoniaNeural"

# Slow pronunciation
TTS_RATE = "-30%"

MAX_INPUT_LENGTH = 500
MAX_AI_ATTEMPTS = 4
MAX_SOUNDS = 5
MAX_EXAMPLES_PER_SOUND = 4


# Injected from bot.py
_ai_function = None


def set_ai_function(function):
    global _ai_function
    _ai_function = function


# ============================================================
# SESSIONS
# ============================================================

sessions = {}


def _cleanup_sessions():
    now = time.time()

    expired = []

    for user_id, data in sessions.items():
        created_at = data.get("created_at", 0)

        if now - created_at > SESSION_TIMEOUT:
            expired.append(user_id)

    for user_id in expired:
        sessions.pop(user_id, None)


def _save_session(user_id, data):
    """
    Save session without resetting its original creation time.
    """
    if "created_at" not in data:
        data["created_at"] = time.time()

    sessions[user_id] = data


def _get_session(user_id):
    _cleanup_sessions()

    data = sessions.get(user_id)

    if not data:
        return None

    created_at = data.get("created_at", 0)

    if time.time() - created_at > SESSION_TIMEOUT:
        sessions.pop(user_id, None)
        return None

    return data


def _end_session(user_id):
    sessions.pop(user_id, None)


# ============================================================
# INPUT
# ============================================================

def _extract_text(update: Update):
    message = update.effective_message

    if not message:
        return None

    text = (message.text or "").strip()

    if text:
        parts = text.split(maxsplit=1)

        if len(parts) > 1:
            argument = parts[1].strip()

            if argument:
                return argument[:MAX_INPUT_LENGTH]

    reply = message.reply_to_message

    if reply:
        reply_text = (
            reply.text
            or reply.caption
            or ""
        ).strip()

        if reply_text:
            return reply_text[:MAX_INPUT_LENGTH]

    return None


# ============================================================
# JSON PARSING
# ============================================================

def _extract_json(raw):
    if not raw:
        return None

    text = raw.strip()

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

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        candidate = text[start:end + 1]

        try:
            return json.loads(candidate)
        except Exception:
            pass

    return None


# ============================================================
# AI CALL
# ============================================================

async def _call_ai(prompt):
    if not _ai_function:
        return None

    try:
        try:
            result = await _ai_function(
                prompt,
                max_tokens=1800,
            )
        except TypeError:
            result = await _ai_function(prompt)

        if result is None:
            return None

        return str(result).strip()

    except Exception:
        return None


# ============================================================
# AI SOUND ANALYSIS
# ============================================================

async def _analyze_with_ai(text, accent):
    """
    Creates a genuinely accent-specific sound analysis.

    The AI must identify:
    - important sounds
    - IPA
    - sound name
    - common spellings
    - exact letters producing the sound in the target
    - position
    - four example words
    - TTS-friendly sound hint
    """

    prompt = f"""
You are an expert English pronunciation teacher.

Analyze this English target:

{json.dumps(text, ensure_ascii=False)}

The requested pronunciation accent is:

{accent}

IMPORTANT:
This must be a genuinely {accent} pronunciation analysis.
Do NOT merely change the accent label.
The IPA, sounds, target letters, and examples must match {accent} pronunciation.

Return ONLY valid JSON.

Required JSON structure:

{{
  "target": "the target word or phrase",
  "accent": "{accent}",
  "full_ipa": "accent-specific IPA",
  "sounds": [
    {{
      "sound": "/IPA/",
      "name": "clear English name of the sound",
      "common_spellings": [
        "common spelling pattern 1",
        "common spelling pattern 2"
      ],
      "position": "beginning / middle / end",
      "target_part": "the exact letter or letters in the target producing this sound",
      "audio_hint": "very short TTS-friendly representation of this sound",
      "examples": [
        {{
          "word": "example 1",
          "ipa": "/IPA/"
        }},
        {{
          "word": "example 2",
          "ipa": "/IPA/"
        }},
        {{
          "word": "example 3",
          "ipa": "/IPA/"
        }},
        {{
          "word": "example 4",
          "ipa": "/IPA/"
        }}
      ]
    }}
  ],
  "training_words": [
    "optional useful word",
    "optional useful word"
  ]
}}

RULES:

1. Focus on the important vowel/sound features of the target.
2. Analyze each important sound separately.
3. If there are several important sounds, create separate objects.
4. Do not combine different sounds into one object.
5. common_spellings means spelling patterns that can produce this sound in English.
6. target_part means the exact letters in THIS target that produce the sound.
7. Do not invent letters that are not actually responsible for the sound.
8. position must describe where the sound occurs in the target.
9. Give EXACTLY four useful example words for every sound.
10. Every example must actually contain the same sound in {accent} pronunciation.
11. The IPA of every example must match {accent}.
12. Do not use the target itself as an example.
13. Keep examples common and useful for an English learner.
14. Do not invent pronunciation differences between American and British English.
15. If American and British pronunciation are genuinely different, reflect that difference accurately.
16. audio_hint is ONLY for the text-to-speech engine.
17. audio_hint must NOT contain IPA symbols.
18. audio_hint must be very short, preferably one syllable.
19. Examples:
    /ɪ/ -> "ih"
    /iː/ -> "ee"
    /ɛ/ -> "eh"
    /æ/ -> "a"
    /ʌ/ -> "uh"
    /ə/ -> "uh"
    /ɑː/ -> "ah"
    /ɔː/ -> "aw"
    /ʊ/ -> "oo"
    /uː/ -> "oo"
    /eɪ/ -> "ay"
    /aɪ/ -> "eye"
    /ɔɪ/ -> "oy"
    /aʊ/ -> "ow"
    /oʊ/ -> "oh"
20. Do not put explanations outside the JSON.
"""

    for _ in range(MAX_AI_ATTEMPTS):
        raw = await _call_ai(prompt)

        data = _extract_json(raw)

        if _validate_analysis(data, accent):
            return data

        await asyncio.sleep(0.4)

    return None


# ============================================================
# VALIDATION
# ============================================================

def _validate_analysis(data, accent):
    if not isinstance(data, dict):
        return False

    target = data.get("target")

    if not isinstance(target, str) or not target.strip():
        return False

    returned_accent = str(
        data.get("accent", "")
    ).strip().lower()

    if accent.lower() not in returned_accent:
        return False

    full_ipa = data.get("full_ipa")

    if not isinstance(full_ipa, str) or not full_ipa.strip():
        return False

    sounds = data.get("sounds")

    if not isinstance(sounds, list):
        return False

    if not sounds:
        return False

    if len(sounds) > MAX_SOUNDS:
        data["sounds"] = sounds[:MAX_SOUNDS]
        sounds = data["sounds"]

    for sound in sounds:
        if not isinstance(sound, dict):
            return False

        required = (
            "sound",
            "name",
            "common_spellings",
            "position",
            "target_part",
            "examples",
        )

        for key in required:
            if key not in sound:
                return False

        if not str(sound.get("sound", "")).strip():
            return False

        if not str(sound.get("name", "")).strip():
            return False

        if not str(sound.get("target_part", "")).strip():
            return False

        spellings = sound.get("common_spellings")

        if not isinstance(spellings, list):
            return False

        examples = sound.get("examples")

        if not isinstance(examples, list):
            return False

        if not examples:
            return False

        if len(examples) > MAX_EXAMPLES_PER_SOUND:
            sound["examples"] = examples[
                :MAX_EXAMPLES_PER_SOUND
            ]

        for example in sound["examples"]:
            if not isinstance(example, dict):
                return False

            word = str(
                example.get("word", "")
            ).strip()

            ipa = str(
                example.get("ipa", "")
            ).strip()

            if not word or not ipa:
                return False

    return True


# ============================================================
# HTML HELPERS
# ============================================================

def _esc(value):
    return html.escape(
        str(value or ""),
        quote=False,
    )


# ============================================================
# FORMAT ANALYSIS
# ============================================================

def _format_analysis(data):
    target = _esc(data.get("target"))
    accent = _esc(data.get("accent"))
    full_ipa = _esc(data.get("full_ipa"))

    lines = [
        "🔊 <b>Sound Analysis</b>",
        "",
        f"🎯 <b>Word:</b> {target}",
        f"🗣 <b>Accent:</b> {accent}",
        f"🔤 <b>IPA:</b> <code>{full_ipa}</code>",
        "",
        "━━━━━━━━━━━━━━━━━━",
    ]

    sounds = data.get("sounds", [])

    for index, sound in enumerate(sounds, start=1):
        sound_ipa = _esc(sound.get("sound"))
        name = _esc(sound.get("name"))
        position = _esc(sound.get("position"))
        target_part = _esc(sound.get("target_part"))

        lines.extend(
            [
                f"🔹 <b>Sound {index}</b>",
                "",
                f"🔊 <b>Sound:</b> <code>{sound_ipa}</code>",
                f"📚 <b>Name:</b> {name}",
                "",
                "🔤 <b>Common spellings:</b>",
            ]
        )

        spellings = sound.get(
            "common_spellings",
            [],
        )

        if spellings:
            spelling_text = " · ".join(
                f"<code>{_esc(item)}</code>"
                for item in spellings
                if str(item).strip()
            )

            lines.append(spelling_text)

        lines.extend(
            [
                "",
                f"📍 <b>Position:</b> {position}",
                f"✏️ <b>In this word:</b> <code>{target_part}</code>",
                "",
                "🧩 <b>Examples with the same sound:</b>",
            ]
        )

        examples = sound.get(
            "examples",
            [],
        )

        for example in examples[:MAX_EXAMPLES_PER_SOUND]:
            word = _esc(example.get("word"))
            ipa = _esc(example.get("ipa"))

            lines.append(
                f"• <b>{word}</b> "
                f"<code>{ipa}</code>"
            )

        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━",
            ]
        )

    return "\n".join(lines)


# ============================================================
# KEYBOARD
# ============================================================

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
            ]
        ]
    )


# ============================================================
# TARGET
# ============================================================

def _get_target_word(data):
    analysis = data.get("analyses", {})

    accent = data.get(
        "selected_accent",
        "American",
    )

    selected = analysis.get(accent)

    if selected:
        return str(
            selected.get("target", "")
        ).strip()

    return str(
        data.get("original_input", "")
    ).strip()


# ============================================================
# TTS CLEANING
# ============================================================

def _clean_tts_text(value):
    """
    Remove punctuation and symbols so Edge TTS
    does not read punctuation.
    """

    value = str(value or "")

    value = re.sub(
        r"[^\w\s'-]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = value.replace(
        "_",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def _clean_audio_hint(value):
    """
    audio_hint is deliberately kept simple because
    Edge TTS should not receive raw IPA symbols.
    """

    value = str(value or "")

    value = re.sub(
        r"[^A-Za-z\s'-]",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


# ============================================================
# IPA -> TTS FALLBACK
# ============================================================

_PHONEME_TTS_HINTS = {
    "ɪ": "ih",
    "i": "ee",
    "iː": "ee",
    "ɛ": "eh",
    "e": "eh",
    "æ": "a",
    "ʌ": "uh",
    "ə": "uh",
    "ɐ": "uh",
    "ɑ": "ah",
    "ɑː": "ah",
    "ɒ": "o",
    "ɔ": "aw",
    "ɔː": "aw",
    "ʊ": "oo",
    "u": "oo",
    "uː": "oo",
    "eɪ": "ay",
    "aɪ": "eye",
    "ɔɪ": "oy",
    "aʊ": "ow",
    "oʊ": "oh",
    "əʊ": "oh",
}


def _phoneme_to_tts_hint(sound):
    sound = str(sound or "").strip()

    sound = sound.strip("/[]")

    if sound in _PHONEME_TTS_HINTS:
        return _PHONEME_TTS_HINTS[sound]

    # Try exact substring matches.
    for ipa, hint in sorted(
        _PHONEME_TTS_HINTS.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if ipa in sound:
            return hint

    return ""


# ============================================================
# BUILD AUDIO SCRIPT
# ============================================================

def _build_audio_text(data):
    """
    Audio order for every sound:

    sound x3
    Listen carefully
    example 1
    example 2
    example 3
    example 4

    Then the next sound.

    No punctuation is intentionally inserted.
    """

    lines = []

    for sound in data.get("sounds", []):
        ipa = sound.get("sound", "")

        hint = _clean_audio_hint(
            sound.get("audio_hint", "")
        )

        if not hint:
            hint = _phoneme_to_tts_hint(ipa)

        if not hint:
            continue

        # The sound itself, three times.
        lines.append(hint)
        lines.append(hint)
        lines.append(hint)

        # Deliberate teaching instruction.
        lines.append("Listen carefully")

        examples = sound.get(
            "examples",
            [],
        )

        for example in examples[
            :MAX_EXAMPLES_PER_SOUND
        ]:
            word = _clean_tts_text(
                example.get("word", "")
            )

            if word:
                lines.append(word)

    return "\n".join(lines)


# ============================================================
# GENERATE AUDIO
# ============================================================

async def _generate_audio(data, accent):
    voice = (
        US_VOICE
        if accent == "American"
        else UK_VOICE
    )

    text = _build_audio_text(data)

    if not text.strip():
        return None

    fd, path = tempfile.mkstemp(
        suffix=".mp3"
    )

    os.close(fd)

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=TTS_RATE,
        )

        await asyncio.wait_for(
            communicate.save(path),
            timeout=60,
        )

        if not os.path.exists(path):
            return None

        if os.path.getsize(path) < 1000:
            return None

        return path

    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

        return None


# ============================================================
# SEND ANALYSIS
# ============================================================

async def _send_analysis(
    message,
    data,
):
    text = _format_analysis(data)

    keyboard = _main_keyboard()

    try:
        return await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception:
        # Fallback if Telegram rejects HTML.
        plain = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        return await message.reply_text(
            plain,
            reply_markup=keyboard,
        )


# ============================================================
# /sounds COMMAND
# ============================================================

async def sounds_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    text = _extract_text(update)

    if not text:
        await update.effective_message.reply_text(
            "🔊 Reply to an English word or sentence, "
            "or use /sounds followed by the text."
        )
        return

    session = {
        "created_at": time.time(),
        "original_input": text,
        "selected_accent": "American",
        "analyses": {},
    }

    _save_session(
        user.id,
        session,
    )

    await _start_first_analysis(
        update,
        user.id,
    )


async def _start_first_analysis(
    update,
    user_id,
):
    session = _get_session(user_id)

    if not session:
        return

    text = session["original_input"]

    data = await _analyze_with_ai(
        text,
        "American",
    )

    if not data:
        await update.effective_message.reply_text(
            "❌ I couldn't complete the sound analysis. "
            "Please try again."
        )
        return

    session["analyses"]["American"] = data
    session["selected_accent"] = "American"

    _save_session(
        user_id,
        session,
    )

    await _send_analysis(
        update.effective_message,
        data,
    )


# ============================================================
# ARABIC COMMAND
# ============================================================

async def sounds_text_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await sounds_command(
        update,
        context,
    )


# ============================================================
# ACCENT CALLBACK
# ============================================================

async def _accent_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    accent: str,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = update.effective_user

    if not user:
        return

    session = _get_session(user.id)

    if not session:
        await query.answer(
            "⏳ This analysis session has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )
        return

    session["selected_accent"] = accent

    analyses = session.setdefault(
        "analyses",
        {},
    )

    data = analyses.get(accent)

    # Analyze the requested accent if it
    # has not been analyzed yet.
    if not data:
        data = await _analyze_with_ai(
            session["original_input"],
            accent,
        )

        if not data:
            await query.answer(
                f"❌ I couldn't complete the {accent} analysis.",
                show_alert=True,
            )
            return

        analyses[accent] = data

    _save_session(
        user.id,
        session,
    )

    formatted = _format_analysis(data)

    try:
        await query.edit_message_text(
            formatted,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(),
        )

    except Exception:
        try:
            await query.edit_message_text(
                re.sub(
                    r"<[^>]+>",
                    "",
                    formatted,
                ),
                reply_markup=_main_keyboard(),
            )
        except Exception:
            pass


# ============================================================
# AUDIO CALLBACK
# ============================================================

async def _audio_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user = update.effective_user

    if not user:
        return

    session = _get_session(user.id)

    if not session:
        await query.answer(
            "⏳ This analysis session has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )
        return

    accent = session.get(
        "selected_accent",
        "American",
    )

    data = session.get(
        "analyses",
        {},
    ).get(accent)

    if not data:
        await query.answer(
            "❌ Please select the accent first.",
            show_alert=True,
        )
        return

    await query.answer(
        f"🎧 Preparing {accent} slow audio..."
    )

    path = await _generate_audio(
        data,
        accent,
    )

    if not path:
        await query.message.reply_text(
            "❌ I couldn't generate the audio. "
            "Your session is still active."
        )
        return

    sent_successfully = False

    try:
        with open(
            path,
            "rb",
        ) as audio_file:
            await query.message.reply_audio(
                audio=audio_file,
                title=(
                    f"{accent} Sound Training"
                ),
                performer="FixMyEnglish",
            )

        sent_successfully = True

    except Exception:
        sent_successfully = False

    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # IMPORTANT:
    # The session ends ONLY after the audio was
    # successfully sent.
    if sent_successfully:
        _end_session(user.id)

        try:
            await query.edit_message_reply_markup(
                reply_markup=None,
            )
        except Exception:
            pass

    else:
        try:
            await query.message.reply_text(
                "❌ The audio could not be sent. "
                "Your session is still active."
            )
        except Exception:
            pass


# ============================================================
# CALLBACK ROUTER
# ============================================================

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
            update,
            context,
            "American",
        )
        return

    if data == "snd_uk":
        await _accent_callback(
            update,
            context,
            "British",
        )
        return

    if data == "snd_audio":
        await _audio_callback(
            update,
            context,
        )
        return


# ============================================================
# HANDLERS
# ============================================================

def register_sounds_handlers(application):
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
                r"^\s*(أصوات|صوت)(?:\s+.+)?\s*$"
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

    print(
        "[SOUNDS] handlers registered successfully."
)
