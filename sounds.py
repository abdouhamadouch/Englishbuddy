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

# After the first accent is completed, the user has 40 seconds
# to choose the second accent.
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

Accent: {accent}

Return ONLY valid JSON.
No Markdown.
No explanation outside JSON.

The analysis must be genuinely specific to {accent} pronunciation.

Use exactly this structure:

{{
  "target": "...",
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

RULES:

- Analyze the important vowel and/or consonant sounds separately.
- Maximum 4 important sounds.
- Give exactly 4 useful example words for every sound.
- Every example must contain the SAME sound.
- Give IPA for every example in the selected accent.
- Do not use the target itself as an example.

SPELLINGS:

"target_part" means ONLY the exact letters in the target that produce
this sound.

"spellings" means OTHER common English spelling patterns that can
represent this same sound in other words.

For example, for /aɪ/, possible common patterns include:
i_e, igh, y, ie, uy, i

Do NOT confuse "target_part" with "spellings".

Example:
If the target is "time" and the sound is /aɪ/:

"target_part": "i"
"spellings": ["i_e", "igh", "y", "ie", "uy", "i"]

Only include spelling patterns that genuinely can represent the
exact sound. Do not invent patterns.

POSITION:

"position" must be beginning, middle, or end.

NAME:

Give a normal pronunciation-teaching name such as:
Short I
Long I
Long A
Schwa
Voiced TH
Unvoiced TH
SH sound
CH sound

Keep names short and natural.

AUDIO:

"audio_hint" is NOT IPA.

It must be a very short English-friendly representation that Edge TTS
can pronounce approximately as the sound itself.

Examples:

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

Do not put IPA symbols inside audio_hint.

Keep the entire JSON concise.
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

    data["sounds"] = sounds[:MAX_SOUNDS]

    for sound in data["sounds"]:
        if not isinstance(sound, dict):
            return False

        for key in (
            "sound",
            "name",
            "spellings",
            "position",
            "target_part",
            "audio_hint",
            "examples",
        ):
            if key not in sound:
                return False

        if not str(sound["sound"]).strip():
            return False

        if not str(sound["name"]).strip():
            return False

        if not str(sound["target_part"]).strip():
            return False

        if not str(sound["audio_hint"]).strip():
            return False

        if not isinstance(
            sound["spellings"],
            list,
        ):
            return False

        examples = sound["examples"]

        if not isinstance(examples, list):
            return False

        if len(examples) < 4:
            return False

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
    target = _esc(data["target"])
    accent = _esc(data["accent"])
    full_ipa = _esc(data["full_ipa"])

    lines = [
        "🔊 <b>Sound Analysis</b>",
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
        sound_ipa = _esc(sound["sound"])
        name = _esc(sound["name"])
        position = _esc(sound["position"])
        target_part = _esc(
            sound["target_part"]
        )

        lines.extend(
            [
                f"🔹 <b>Sound {index}</b>",
                "",
                f"🔊 <b>Sound:</b> "
                f"<code>{sound_ipa}</code>",
                f"📚 <b>Name:</b> {name}",
                f"📍 <b>Position:</b> {position}",
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

        lines.extend(
            [
                "",
                "🧩 <b>Examples with the same sound:</b>",
            ]
        )

        for example in sound["examples"][:4]:
            word = _esc(example["word"])
            ipa = _esc(example["ipa"])

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
    value = str(value or "")

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
    )

    return value.strip()


def _clean_sound_name(value):
    value = str(value or "")

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
    value = str(sound or "").strip()
    value = value.strip("/[]")

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
    """
    For every important sound:

    Sound name
    Sound
    Sound
    Sound
    Listen carefully
    Example 1
    Example 2
    Example 3
    Example 4
    """

    parts = []

    for sound in data.get("sounds", []):
        # ----------------------------------------------------
        # Say the NAME of the sound first.
        # Example: "Schwa", "Long I", "Short E"
        # ----------------------------------------------------

        name = _clean_sound_name(
            sound.get("name", "")
        )

        if name:
            parts.append(name)

        # ----------------------------------------------------
        # Then pronounce the actual sound 3 times.
        # ----------------------------------------------------

        hint = _clean_audio_hint(
            sound.get("audio_hint", "")
        )

        if not hint:
            hint = _fallback_audio_hint(
                sound.get("sound", "")
            )

        if not hint:
            continue

        parts.append(hint)
        parts.append(hint)
        parts.append(hint)

        # ----------------------------------------------------
        # Then the teaching phrase.
        # ----------------------------------------------------

        parts.append("Listen carefully")

        # ----------------------------------------------------
        # Then exactly four example words.
        # ----------------------------------------------------

        for example in sound.get(
            "examples",
            [],
        )[:MAX_EXAMPLES_PER_SOUND]:

            word = _clean_example_for_tts(
                example.get("word", "")
            )

            if word:
                parts.append(word)

    return "\n".join(parts)


# ============================================================
# AUDIO GENERATION
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
    text = _format_analysis(data)

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
        with open(path, "rb") as audio_file:
            await message.reply_audio(
                audio=audio_file,
                title=f"{accent} Sound Training",
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

    text = _extract_text(update)

    if not text:
        await update.effective_message.reply_text(
            "🔊 Reply to an English word or sentence, "
            "or use /sounds followed by the text."
        )
        return

    # New session replaces an older session for this user.
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

    choice_message = await update.effective_message.reply_text(
        "🔊 <b>Choose the pronunciation accent:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_accent_keyboard(),
    )

    # Bind the buttons to THIS exact message.
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

    session = _get_session(user_id)

    if not session:
        return

    deadline = session.get(
        "second_choice_deadline"
    )

    if not deadline:
        return

    if time.time() < deadline:
        return

    # Only expire if this is still the active
    # second-choice message.
    if session.get(
        "choice_message_id"
    ) != message_id:
        return

    _end_session(user_id)


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

    session = _get_session(user.id)

    # --------------------------------------------------------
    # No session for this user
    # --------------------------------------------------------

    if not session:
        await query.answer(
            "⏳ This analysis session has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )
        return

    # --------------------------------------------------------
    # IMPORTANT:
    # The button must belong to the exact message
    # created for THIS user's session.
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
    # 40-second deadline for second accent
    # --------------------------------------------------------

    deadline = session.get(
        "second_choice_deadline"
    )

    if deadline and time.time() > deadline:
        _end_session(user.id)

        await query.answer(
            "⏳ The 40-second choice period has expired. "
            "Please use /sounds again.",
            show_alert=True,
        )
        return

    # --------------------------------------------------------
    # Prevent selecting the same accent twice.
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
        f"Preparing {accent} pronunciation..."
    )

    analyses = session.setdefault(
        "analyses",
        {},
    )

    # --------------------------------------------------------
    # Get or create accent-specific analysis
    # --------------------------------------------------------

    data = analyses.get(accent)

    if not data:
        data = await _analyze_with_ai(
            session["original_input"],
            accent,
        )

        if not data:
            await query.message.reply_text(
                f"❌ I couldn't complete the "
                f"{accent} sound analysis.\n\n"
                f"Your session is still active. "
                f"Please try again."
            )
            return

        analyses[accent] = data

    # --------------------------------------------------------
    # Temporarily remove the clicked buttons.
    # This prevents duplicate visible keyboards.
    # --------------------------------------------------------

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    # --------------------------------------------------------
    # Mark this accent as used ONLY after analysis succeeds.
    # --------------------------------------------------------

    used_accents.append(accent)

    session["used_accents"] = used_accents

    _save_session(
        user.id,
        session,
    )

    # --------------------------------------------------------
    # Send analysis.
    #
    # If one accent remains, only ONE button is shown:
    # the unused accent.
    # --------------------------------------------------------

    remaining_accents = [
        item
        for item in (
            "American",
            "British",
        )
        if item not in used_accents
    ]

    analysis_message = await _send_analysis(
        query.message,
        data,
        remaining_accents,
    )

    if not analysis_message:
        return

    # --------------------------------------------------------
    # Send the actual MP3 immediately underneath.
    # --------------------------------------------------------

    audio_sent = await _send_audio(
        query.message,
        data,
        accent,
    )

    if not audio_sent:
        # Do not end the session.
        # Also remove this accent so it can be retried.
        if accent in used_accents:
            used_accents.remove(accent)

        session["used_accents"] = used_accents

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
    # Audio succeeded.
    #
    # If BOTH accents are already completed:
    # session ends immediately.
    # --------------------------------------------------------

    if len(used_accents) >= 2:
        _end_session(user.id)
        return

    # --------------------------------------------------------
    # One accent remains.
    #
    # The user now has exactly 40 seconds to choose it.
    # --------------------------------------------------------

    session["second_choice_deadline"] = (
        time.time()
        + SECOND_ACCENT_TIMEOUT
    )

    # The active buttons are now on the NEW analysis message.
    session["choice_message_id"] = (
        analysis_message.message_id
    )

    _save_session(
        user.id,
        session,
    )

    # Start the 40-second expiry task.
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
