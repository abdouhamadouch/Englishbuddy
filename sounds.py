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

SECOND_ACCENT_TIMEOUT = 40

US_VOICE = "en-US-AriaNeural"
UK_VOICE = "en-GB-SoniaNeural"

# Slow, clear pronunciation
TTS_RATE = "-30%"

MAX_INPUT_LENGTH = 500
MAX_AI_ATTEMPTS = 4

MAX_SOUNDS = 4
MAX_EXAMPLES_PER_SOUND = 4

_ai_function = None

sessions = {}


# ============================================================
# AI INJECTION
# ============================================================

def set_ai_function(function):
    global _ai_function
    _ai_function = function


# ============================================================
# SESSION
# ============================================================

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
    if "created_at" not in data:
        data["created_at"] = time.time()

    sessions[user_id] = data


def _get_session(user_id):
    _cleanup_sessions()

    data = sessions.get(user_id)

    if not data:
        return None

    if time.time() - data.get("created_at", 0) > SESSION_TIMEOUT:
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
            value = parts[1].strip()

            if value:
                return value[:MAX_INPUT_LENGTH]

    reply = message.reply_to_message

    if reply:
        value = (
            reply.text
            or reply.caption
            or ""
        ).strip()

        if value:
            return value[:MAX_INPUT_LENGTH]

    return None


# ============================================================
# JSON
# ============================================================

def _extract_json(raw):
    if not raw:
        return None

    text = str(raw).strip()

    # Remove Markdown code fences
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

    # First attempt: complete JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Second attempt: extract JSON object
    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        try:
            return json.loads(
                text[start:end + 1]
            )
        except Exception:
            pass

    return None


# ============================================================
# AI
# ============================================================

async def _call_ai(prompt):
    if not _ai_function:
        return None

    try:
        try:
            result = await _ai_function(
                prompt,
                max_tokens=1500,
            )
        except TypeError:
            result = await _ai_function(prompt)

        if result is None:
            return None

        return str(result).strip()

    except Exception:
        return None


async def _analyze_with_ai(text, accent):

    prompt = f"""
You are an expert English pronunciation and phonics teacher.

Analyze this English word or short phrase:

{text}

Pronunciation accent:
{accent}

IMPORTANT:
This feature is ONLY about VOWEL SOUNDS.

Do NOT analyze consonants.

Return ONLY valid JSON.
No Markdown.
No explanation outside JSON.

Use exactly this structure:

{{
  "target": "{text}",
  "accent": "{accent}",
  "full_ipa": "...",
  "sounds": [
    {{
      "sound": "...",
      "name": "...",
      "spellings": ["...", "..."],
      "position": "...",
      "target_part": "...",
      "audio_hint": "...",
      "examples": [
        {{"word": "...", "ipa": "..."}},
        {{"word": "...", "ipa": "..."}},
        {{"word": "...", "ipa": "..."}},
        {{"word": "...", "ipa": "..."}}
      ]
    }}
  ]
}}

VERY IMPORTANT RULES:

1. Analyze ONLY vowel sounds.

2. NEVER create a sound entry for a consonant.

3. Ignore all consonants completely.

4. Focus on the actual vowel sounds heard in the target.

5. If there are several important vowel sounds, analyze them separately.

6. Maximum {MAX_SOUNDS} vowel sounds.

7. Give up to {MAX_EXAMPLES_PER_SOUND} common example words for each
   vowel sound.

8. Every example must contain the SAME vowel sound being explained.

9. Give IPA for every example in the selected {accent} accent.

10. Never use the target itself as an example.

11. Use common English words.

12. Distinguish short vowels, long vowels, diphthongs, and schwa
    when relevant.

13. "target_part" must contain ONLY the actual letters in the target
    that represent the vowel sound.

14. "spellings" must contain OTHER common English spelling patterns
    that can represent the same vowel sound.

15. "position" must be:
    "beginning"
    "middle"
    or
    "end"

16. "name" should be a short natural teaching name.

Examples of names:

Short A
Short E
Short I
Short O
Short U
Long A
Long E
Long I
Long O
Long U
Schwa
AY sound
EYE sound
OW sound
OY sound

17. "audio_hint" must NOT contain IPA symbols.

Use simple English-friendly hints such as:

/ɪ/ = ih
/iː/ = ee
/ɛ/ = eh
/æ/ = a
/ʌ/ = uh
/ə/ = uh
/ɑː/ = ah
/ɔː/ = aw
/ʊ/ = oo
/uː/ = oo
/eɪ/ = ay
/aɪ/ = eye
/ɔɪ/ = oy
/aʊ/ = ow
/oʊ/ = oh
/əʊ/ = oh

18. The "sounds" array MUST contain vowel sounds only.

Never include consonants such as:

/p/ /b/ /t/ /d/ /k/ /g/
/f/ /v/ /s/ /z/
/ʃ/ /ʒ/
/tʃ/ /dʒ/
/θ/ /ð/
/h/
/m/ /n/ /ŋ/
/l/ /r/
/w/ /j/

The goal is NOT to analyze every letter.

The goal is to teach the vowel sounds in the target.

Keep the JSON concise and valid.
"""

    for attempt in range(MAX_AI_ATTEMPTS):

        raw = await _call_ai(prompt)

        data = _extract_json(raw)

        if _validate_analysis(data, accent):

            return data

        if attempt < MAX_AI_ATTEMPTS - 1:
            await asyncio.sleep(0.5)

    return None


# ============================================================
# VOWEL CHECK
# ============================================================

VOWEL_IPA_STARTS = (
    "i",
    "ɪ",
    "e",
    "ɛ",
    "æ",
    "a",
    "ɑ",
    "ɒ",
    "ɔ",
    "o",
    "ʊ",
    "u",
    "ʌ",
    "ə",
)

VOWEL_NAMES = (
    "short a",
    "short e",
    "short i",
    "short o",
    "short u",
    "long a",
    "long e",
    "long i",
    "long o",
    "long u",
    "schwa",
    "ay sound",
    "eye sound",
    "ow sound",
    "oy sound",
    "a sound",
    "e sound",
    "i sound",
    "o sound",
    "u sound",
)


def _looks_like_vowel_sound(sound):
    value = str(sound or "").strip()

    if not value:
        return False

    value = value.strip("/[]").lower()

    # Clear consonant IPA symbols
    consonants = {
        "p", "b", "t", "d", "k", "g",
        "f", "v", "s", "z",
        "ʃ", "ʒ", "tʃ", "dʒ",
        "θ", "ð", "h",
        "m", "n", "ŋ",
        "l", "r",
        "w", "j",
    }

    if value in consonants:
        return False

    # If the sound contains a vowel symbol, accept it.
    for char in VOWEL_IPA_STARTS:
        if char in value:
            return True

    return False


# ============================================================
# VALIDATION
# ============================================================

def _validate_analysis(data, accent):

    if not isinstance(data, dict):
        return False

    target = str(
        data.get("target", "")
    ).strip()

    if not target:
        return False

    returned_accent = str(
        data.get("accent", "")
    ).strip().lower()

    if accent.lower() not in returned_accent:
        return False

    full_ipa = str(
        data.get("full_ipa", "")
    ).strip()

    if not full_ipa:
        return False

    sounds = data.get("sounds")

    if not isinstance(sounds, list):
        return False

    if not sounds:
        return False

    valid_sounds = []

    for sound in sounds[:MAX_SOUNDS]:

        if not isinstance(sound, dict):
            continue

        sound_ipa = str(
            sound.get("sound", "")
        ).strip()

        # ----------------------------------------------------
        # Reject consonant entries.
        # ----------------------------------------------------

        if not _looks_like_vowel_sound(sound_ipa):
            continue

        name = str(
            sound.get("name", "")
        ).strip()

        if not name:
            name = "Vowel sound"

        target_part = str(
            sound.get("target_part", "")
        ).strip()

        if not target_part:
            continue

        position = str(
            sound.get("position", "middle")
        ).strip()

        if position not in (
            "beginning",
            "middle",
            "end",
        ):
            position = "middle"

        spellings = sound.get(
            "spellings",
            [],
        )

        if not isinstance(spellings, list):
            spellings = []

        spellings = [
            str(x).strip()
            for x in spellings
            if str(x).strip()
        ]

        audio_hint = str(
            sound.get("audio_hint", "")
        ).strip()

        if not audio_hint:
            audio_hint = _fallback_audio_hint(
                sound_ipa
            )

        if not audio_hint:
            continue

        examples = sound.get(
            "examples",
            [],
        )

        if not isinstance(examples, list):
            examples = []

        clean_examples = []

        for example in examples:

            if not isinstance(example, dict):
                continue

            word = str(
                example.get("word", "")
            ).strip()

            ipa = str(
                example.get("ipa", "")
            ).strip()

            if not word or not ipa:
                continue

            # Do not use the target as an example.
            if word.lower() == target.lower():
                continue

            clean_examples.append(
                {
                    "word": word,
                    "ipa": ipa,
                }
            )

            if len(clean_examples) >= MAX_EXAMPLES_PER_SOUND:
                break

        # At least one useful example is enough
        # to keep the analysis alive.
        if not clean_examples:
            continue

        valid_sounds.append(
            {
                "sound": sound_ipa,
                "name": name,
                "spellings": spellings,
                "position": position,
                "target_part": target_part,
                "audio_hint": audio_hint,
                "examples": clean_examples,
            }
        )

    if not valid_sounds:
        return False

    data["target"] = target
    data["accent"] = accent
    data["full_ipa"] = full_ipa
    data["sounds"] = valid_sounds

    return True


# ============================================================
# HTML
# ============================================================

def _esc(value):
    return html.escape(
        str(value or ""),
        quote=False,
    )


# ============================================================
# ANALYSIS FORMAT
# ============================================================

def _format_analysis(data):

    target = _esc(
        data["target"]
    )

    accent = _esc(
        data["accent"]
    )

    full_ipa = _esc(
        data["full_ipa"]
    )

    lines = [
        "🔊 <b>Vowel Sound Analysis</b>",
        "",
        f"🎯 <b>Word:</b> {target}",
        f"🗣 <b>Accent:</b> {accent}",
        f"🔤 <b>IPA:</b> <code>{full_ipa}</code>",
        "",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for index, sound in enumerate(
        data["sounds"],
        start=1,
    ):

        sound_ipa = _esc(
            sound["sound"]
        )

        name = _esc(
            sound["name"]
        )

        position = _esc(
            sound["position"]
        )

        target_part = _esc(
            sound["target_part"]
        )

        lines.extend(
            [
                f"🔹 <b>Vowel Sound {index}</b>",
                "",
                f"🔊 <b>Sound:</b> "
                f"<code>{sound_ipa}</code>",

                f"📚 <b>Name:</b> "
                f"{name}",

                f"📍 <b>Position:</b> "
                f"{position}",

                f"✏️ <b>In this word:</b> "
                f"<code>{target_part}</code>",

                "",
                "🔤 <b>Other common spellings:</b>",
            ]
        )

        spellings = [
            str(x).strip()
            for x in sound.get(
                "spellings",
                [],
            )
            if str(x).strip()
        ]

        if spellings:

            lines.append(
                " · ".join(
                    f"<code>{_esc(x)}</code>"
                    for x in spellings
                )
            )

        else:

            lines.append(
                "—"
            )

        lines.extend(
            [
                "",
                "🧩 <b>Examples with the same vowel sound:</b>",
            ]
        )

        for example in sound.get(
            "examples",
            [],
        )[:MAX_EXAMPLES_PER_SOUND]:

            word = _esc(
                example["word"]
            )

            ipa = _esc(
                example["ipa"]
            )

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
# KEYBOARDS
# ============================================================

def _accent_keyboard(
    available_accents=None,
):

    if available_accents is None:
        available_accents = [
            "American",
            "British",
        ]

    buttons = []

    if "American" in available_accents:

        buttons.append(
            InlineKeyboardButton(
                "🇺🇸 American",
                callback_data="snd_us",
            )
        )

    if "British" in available_accents:

        buttons.append(
            InlineKeyboardButton(
                "🇬🇧 British",
                callback_data="snd_uk",
            )
        )

    if not buttons:
        return None

    return InlineKeyboardMarkup(
        [buttons]
    )


# ============================================================
# TTS CLEANING
# ============================================================

def _clean_example_for_tts(value):

    value = str(
        value or ""
    )

    value = re.sub(
        r"[^\w\s'-]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def _clean_audio_hint(value):

    value = str(
        value or ""
    )

    value = re.sub(
        r"[^A-Za-z\s'-]",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def _clean_sound_name(value):

    value = str(
        value or ""
    )

    value = re.sub(
        r"[^A-Za-z0-9\s'-]",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


# ============================================================
# FALLBACK SOUND HINTS
# ============================================================

PHONEME_HINTS = {
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


def _fallback_audio_hint(sound):

    value = str(
        sound or ""
    ).strip()

    value = value.strip(
        "/[]"
    )

    if value in PHONEME_HINTS:
        return PHONEME_HINTS[value]

    for ipa, hint in sorted(
        PHONEME_HINTS.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):

        if ipa in value:
            return hint

    return ""


# ============================================================
# BUILD AUDIO
# ============================================================

def _build_audio_text(data):

    parts = []

    for sound in data.get(
        "sounds",
        [],
    ):

        # ----------------------------------------------------
        # Name
        # ----------------------------------------------------

        name = _clean_sound_name(
            sound.get(
                "name",
                "",
            )
        )

        if name:
            parts.append(name)

        # ----------------------------------------------------
        # Actual vowel sound
        # ----------------------------------------------------

        hint = _clean_audio_hint(
            sound.get(
                "audio_hint",
                "",
            )
        )

        if not hint:

            hint = _fallback_audio_hint(
                sound.get(
                    "sound",
                    "",
                )
            )

        if not hint:
            continue

        # Repeat the vowel sound three times.
        parts.append(hint)
        parts.append(hint)
        parts.append(hint)

        # ----------------------------------------------------
        # Teaching phrase
        # ----------------------------------------------------

        parts.append(
            "Listen carefully"
        )

        # ----------------------------------------------------
        # Examples
        # ----------------------------------------------------

        for example in sound.get(
            "examples",
            [],
        )[:MAX_EXAMPLES_PER_SOUND]:

            word = _clean_example_for_tts(
                example.get(
                    "word",
                    "",
                )
            )

            if word:
                parts.append(word)

    return "\n".join(parts)


# ============================================================
# AUDIO GENERATION
# ============================================================

async def _generate_audio(
    data,
    accent,
):

    voice = (
        US_VOICE
        if accent == "American"
        else UK_VOICE
    )

    text = _build_audio_text(
        data
    )

    if not text.strip():
        return None

    fd, path = tempfile.mkstemp(
        suffix=".mp3"
    )

    os.close(fd)

    try:

        communicator = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=TTS_RATE,
        )

        await asyncio.wait_for(
            communicator.save(path),
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
    available_accents=None,
):

    text = _format_analysis(
        data
    )

    markup = _accent_keyboard(
        available_accents
    )

    try:

        return await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

    except Exception:

        plain = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        return await message.reply_text(
            plain,
            reply_markup=markup,
        )


# ============================================================
# SEND AUDIO
# ============================================================

async def _send_audio(
    message,
    data,
    accent,
):

    path = await _generate_audio(
        data,
        accent,
    )

    if not path:
        return False

    try:

        with open(
            path,
            "rb",
        ) as audio_file:

            await message.reply_audio(
                audio=audio_file,
                title=(
                    f"{accent} "
                    "Vowel Sound Training"
                ),
                performer="FixMyEnglish",
            )

        return True

    except Exception:

        return False

    finally:

        try:

            if os.path.exists(path):
                os.remove(path)

        except Exception:
            pass


# ============================================================
# /sounds
# ============================================================

async def sounds_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    text = _extract_text(
        update
    )

    if not text:

        await update.effective_message.reply_text(
            "🔊 Reply to an English word or sentence, "
            "or use /sounds followed by the text."
        )

        return

    # New session replaces old session.
    session = {
        "created_at": time.time(),

        "original_input": text,

        "analyses": {},

        "used_accents": [],

        "choice_message_id": None,

        "choice_chat_id": (
            update.effective_chat.id
            if update.effective_chat
            else None
        ),

        "second_choice_deadline": None,
    }

    _save_session(
        user.id,
        session,
    )

    choice_message = (
        await update.effective_message.reply_text(
            "🔊 <b>Choose the pronunciation accent:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_accent_keyboard(),
        )
    )

    session["choice_message_id"] = (
        choice_message.message_id
    )

    _save_session(
        user.id,
        session,
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
# EXPIRE SECOND CHOICE
# ============================================================

async def _expire_second_choice(
    user_id,
    message_id,
    chat_id,
):

    await asyncio.sleep(
        SECOND_ACCENT_TIMEOUT
    )

    session = _get_session(
        user_id
    )

    if not session:
        return

    deadline = session.get(
        "second_choice_deadline"
    )

    if not deadline:
        return

    if time.time() < deadline:
        return

    if session.get(
        "choice_message_id"
    ) != message_id:
        return

    _end_session(
        user_id
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

    user = update.effective_user

    if not user:
        return

    session = _get_session(
        user.id
    )

    # --------------------------------------------------------
    # No session
    # --------------------------------------------------------

    if not session:

        await query.answer(
            "⏳ This analysis session has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # Validate exact message
    # --------------------------------------------------------

    if not query.message:

        await query.answer(
            "⏳ This button is no longer active.",
            show_alert=True,
        )

        return

    expected_message_id = session.get(
        "choice_message_id"
    )

    expected_chat_id = session.get(
        "choice_chat_id"
    )

    if (
        query.message.message_id
        != expected_message_id
        or (
            expected_chat_id is not None
            and query.message.chat_id
            != expected_chat_id
        )
    ):

        await query.answer(
            "⏳ This button is no longer active.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # Second accent deadline
    # --------------------------------------------------------

    deadline = session.get(
        "second_choice_deadline"
    )

    if deadline and time.time() > deadline:

        _end_session(
            user.id
        )

        await query.answer(
            "⏳ The 40-second choice period has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )

        return

    # --------------------------------------------------------
    # Prevent duplicate accent
    # --------------------------------------------------------

    used_accents = session.setdefault(
        "used_accents",
        [],
    )

    if accent in used_accents:

        await query.answer(
            "This accent has already been used.",
            show_alert=True,
        )

        return

    await query.answer(
        f"Preparing {accent} vowel pronunciation..."
    )

    analyses = session.setdefault(
        "analyses",
        {},
    )

    # --------------------------------------------------------
    # Get cached analysis or create it
    # --------------------------------------------------------

    data = analyses.get(
        accent
    )

    if not data:

        data = await _analyze_with_ai(
            session["original_input"],
            accent,
        )

        if not data:

            await query.message.reply_text(
                f"❌ I couldn't complete the "
                f"{accent} vowel sound analysis.\n\n"
                f"Your session is still active. "
                f"Please try again."
            )

            return

        analyses[accent] = data

    # --------------------------------------------------------
    # Remove clicked buttons
    # --------------------------------------------------------

    try:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # Mark accent as used
    # --------------------------------------------------------

    used_accents.append(
        accent
    )

    session["used_accents"] = (
        used_accents
    )

    _save_session(
        user.id,
        session,
    )

    # --------------------------------------------------------
    # Remaining accent
    # --------------------------------------------------------

    remaining_accents = [
        item
        for item in (
            "American",
            "British",
        )
        if item not in used_accents
    ]

    # --------------------------------------------------------
    # Send analysis
    # --------------------------------------------------------

    analysis_message = await _send_analysis(
        query.message,
        data,
        remaining_accents,
    )

    if not analysis_message:
        return

    # --------------------------------------------------------
    # Send audio
    # --------------------------------------------------------

    audio_sent = await _send_audio(
        query.message,
        data,
        accent,
    )

    if not audio_sent:

        if accent in used_accents:
            used_accents.remove(
                accent
            )

        session["used_accents"] = (
            used_accents
        )

        _save_session(
            user.id,
            session,
        )

        try:

            await analysis_message.edit_reply_markup(
                reply_markup=_accent_keyboard(
                    remaining_accents
                )
            )

        except Exception:
            pass

        await query.message.reply_text(
            "❌ I couldn't send the audio file.\n\n"
            "Your session is still active. "
            "Please try again."
        )

        return

    # --------------------------------------------------------
    # Both accents completed
    # --------------------------------------------------------

    if len(used_accents) >= 2:

        _end_session(
            user.id
        )

        return

    # --------------------------------------------------------
    # One accent remains
    # --------------------------------------------------------

    session["second_choice_deadline"] = (
        time.time()
        + SECOND_ACCENT_TIMEOUT
    )

    session["choice_message_id"] = (
        analysis_message.message_id
    )

    _save_session(
        user.id,
        session,
    )

    asyncio.create_task(
        _expire_second_choice(
            user.id,
            analysis_message.message_id,
            query.message.chat_id,
        )
    )


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


# ============================================================
# REGISTER
# ============================================================

def register_sounds_handlers(application):

    application.add_handler(
        CommandHandler(
            ["sounds", "sound"],
            sounds_command,
        )
    )

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

    application.add_handler(
        CallbackQueryHandler(
            sounds_callback,
            pattern=r"^snd_(us|uk)$",
        )
    )

    print(
        "[SOUNDS] handlers registered successfully."
    )
