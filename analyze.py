import re
import asyncio
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# =========================================================
# CONNECTION WITH bot.py
# =========================================================

_ask_groq = None
_get_target_text = None
_is_approved = None


def configure(ask_groq_func, get_target_text_func, is_approved_func):
    global _ask_groq
    global _get_target_text
    global _is_approved

    _ask_groq = ask_groq_func
    _get_target_text = get_target_text_func
    _is_approved = is_approved_func


# =========================================================
# HELPERS
# =========================================================

def extract_word(text):
    text = (text or "").strip()

    if not text:
        return ""

    text = re.sub(
        r"^(?:/analysis|/analys|تحليل)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    return text


def get_analysis_keyboard(word):
    safe_word = word[:40]

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Synonyms",
                    callback_data=f"analysis:syn:{safe_word}",
                ),
                InlineKeyboardButton(
                    "🇩🇿 Meanings",
                    callback_data=f"analysis:mean:{safe_word}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟰 Homophones",
                    callback_data=f"analysis:homo:{safe_word}",
                ),
                InlineKeyboardButton(
                    "✍️ Similar Spelling",
                    callback_data=f"analysis:spell:{safe_word}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧩 Word Family",
                    callback_data=f"analysis:family:{safe_word}",
                ),
                InlineKeyboardButton(
                    "🌱 Root",
                    callback_data=f"analysis:root:{safe_word}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔊 Pronunciation",
                    callback_data=f"analysis:pron:{safe_word}",
                ),
            ],
        ]
    )


# =========================================================
# MAIN ANALYSIS
# =========================================================

async def analyze_word(text):

    if not _ask_groq:
        return "❌ Analysis system is not configured."

    word = extract_word(text)

    if not word:
        return "❌ Please provide an English word."

    prompt = f"""
Analyze this English word for an Arabic-speaking learner:

{word}

Give a compact but useful overview.

IMPORTANT:
- Focus on the most common meaning and usage.
- Do not invent meanings.
- If the word has several important meanings, mention them briefly.
- Include part of speech.
- Include a short English definition.
- Include the main Arabic meaning.
- Include American and British IPA.
- Include one useful example with Arabic translation.
- Include an important usage pattern or collocation if one exists.
- Mention an important noun/verb/adjective distinction if relevant.
- Do NOT give long dictionary-style explanations.
- Do NOT give synonyms, antonyms, homophones, word family, or root lists here.
  These will be available through buttons.
- Keep the main card around 6–7 short lines.
- Do not use decorative stars.

Use this structure:

🔎 WORD ANALYSIS
━━━━━━━━━━━━━━━━━━

🔤 Word: [word]
🏷️ Part of Speech: [part of speech]
🇺🇸 US: [IPA]
🇬🇧 UK: [IPA]
🇩🇿 Meaning: [main Arabic meaning]
📖 Definition: [short English definition]
📝 Example: [natural English example]
🇩🇿 [Arabic translation]

🔗 Usage: [important pattern/collocation if relevant]

━━━━━━━━━━━━━━━━━━
"""

    for attempt in range(4):

        result = await _ask_groq(
            prompt,
            900,
        )

        if result and not result.startswith("❌"):
            return result.strip()

        if attempt < 3:
            await asyncio.sleep(1)

    return "⚠️ I couldn't analyze this word right now. Please try again."


# =========================================================
# ANALYSIS SECTIONS
# =========================================================

async def analysis_section(word, section):

    if not _ask_groq:
        return "❌ Analysis system is not configured."

    if section == "syn":
        title = "🔄 SYNONYMS"

        instruction = """
Give the most useful and natural synonyms.

For each synonym:
- English word
- short Arabic meaning
- briefly explain an important difference in usage when necessary

Give up to 6.
Do not invent synonyms.
"""

    elif section == "mean":
        title = "🇩🇿 MEANINGS & USAGE"

        instruction = """
Give the important common meanings of the word.

Separate meanings by part of speech when necessary.

For every important meaning:
- short English definition
- Arabic meaning
- one natural English example
- Arabic translation

Do not include rare meanings unless they are genuinely useful.
"""

    elif section == "homo":
        title = "🟰 HOMOPHONES"

        instruction = """
Find genuine English homophones of the word.

For every homophone:
- word
- Arabic meaning
- short example

If there are no common homophones, clearly say so.

Do not confuse homophones with words that merely sound similar.
"""

    elif section == "spell":
        title = "✍️ SIMILAR SPELLING"

        instruction = """
Give useful English words that are easily confused with this word
because of similar spelling or pronunciation.

For every word:
- spelling
- pronunciation if useful
- Arabic meaning
- one short example
- briefly explain the difference from the original word

Do not include random words merely because they share a few letters.
"""

    elif section == "family":
        title = "🧩 WORD FAMILY"

        instruction = """
Give the common English word-family members.

Include noun, verb, adjective and adverb forms when they genuinely exist.

For each:
- word
- part of speech
- Arabic meaning
- short English example
- Arabic translation

Do not invent forms.
"""

    elif section == "root":
        title = "🌱 WORD ROOT"

        instruction = """
Explain the genuine classical or historical root of the word when one exists.

Give:
- root
- original/core meaning
- prefix and suffix when relevant
- useful English words built from the same root
- Arabic meaning for each
- short example for each

Do not invent etymological relationships.

If there is no useful reliable root, say so.
"""

    elif section == "pron":
        title = "🔊 PRONUNCIATION"

        instruction = """
Give:
- American IPA
- British IPA
- syllable breakdown
- stressed syllable
- silent letters if any
- one short pronunciation tip if useful
- mention noun/verb stress differences if they exist

Do not invent pronunciation differences.
"""

    else:
        return "❌ Unknown analysis section."

    prompt = f"""
{instruction}

Word:
{word}

Rules:
- Arabic meanings should be natural and concise.
- Keep the answer organized and mobile-friendly.
- Do not use decorative stars.
- Do not ask a follow-up question.
- Do not add an unnecessary introduction.

Start directly with:

{title}
━━━━━━━━━━━━━━━━━━
"""

    for attempt in range(4):

        result = await _ask_groq(
            prompt,
            1200,
        )

        if result and not result.startswith("❌"):
            return result.strip()

        if attempt < 3:
            await asyncio.sleep(1)

    return "⚠️ I couldn't load this section right now. Please try again."


# =========================================================
# ANALYSIS COMMAND
# =========================================================

async def analysis_command(update, context):

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    if not chat or not user or not message:
        return

    if (
        chat.type == "private"
        and _is_approved
        and not _is_approved(user.id)
    ):
        return

    text = ""

    if _get_target_text:
        text = _get_target_text(message)

    text = extract_word(text)

    if not text:
        await message.reply_text(
            "Usage:\n"
            "/analysis word\n"
            "/analys word\n\n"
            "Or reply to a message containing the word with /analysis."
        )
        return

    result = await analyze_word(text)

    keyboard = get_analysis_keyboard(text)

    try:
        await message.reply_text(
            result,
            reply_markup=keyboard,
        )

    except Exception:
        await message.reply_text(
            result,
            reply_markup=keyboard,
        )


# =========================================================
# CALLBACK
# =========================================================

async def analysis_callback(update, context):

    query = update.callback_query

    if not query:
        return

    user = query.from_user

    if (
        _is_approved
        and not _is_approved(user.id)
    ):
        await query.answer(
            "Access denied.",
            show_alert=True,
        )
        return

    data = query.data or ""

    if not data.startswith("analysis:"):
        return

    parts = data.split(":", 2)

    if len(parts) != 3:
        await query.answer()
        return

    section = parts[1]
    word = parts[2].strip()

    await query.answer()

    result = await analysis_section(
        word,
        section,
    )

    keyboard = get_analysis_keyboard(word)

    try:
        await query.edit_message_text(
            result,
            reply_markup=keyboard,
        )

    except Exception as e:
        print(
            "Analysis callback error:",
            repr(e),
            flush=True,
      )
