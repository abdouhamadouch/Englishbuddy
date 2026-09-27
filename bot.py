import os
import json
import re
import random
import tempfile
import threading
import asyncio
import time
import html
from urllib.parse import quote
from pathlib import Path

from flask import Flask
from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import edge_tts
import analyze


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

MODEL = "openai/gpt-oss-20b"

US_VOICE = "en-US-AriaNeural"
UK_VOICE = "en-GB-SoniaNeural"

USERS_FILE = Path("users.json")
PENDING_FILE = Path("pending_users.json")
VOCAB_FILE = Path("vocab_bank.json")
AUTOCORRECT_FILE = Path("autocorrect_groups.json")

app = Flask(__name__)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

json_lock = threading.Lock()
talk_mode_users = set()

OWNER_USERNAME = None


# =========================================================
# JSON STORAGE
# =========================================================

def load_json(path, default):
    with json_lock:
        try:
            if not path.exists():
                return default

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return data

        except Exception as e:
            print("Load JSON error:", repr(e), flush=True)
            return default


def save_json(path, data):
    with json_lock:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print("Save JSON error:", repr(e), flush=True)


# Groups where automatic correction has been turned OFF
disabled_autocorrect_groups = load_json(
    AUTOCORRECT_FILE,
    [],
) if AUTOCORRECT_FILE.exists() else []

disabled_autocorrect_groups = {
    int(x)
    for x in disabled_autocorrect_groups
    if str(x).lstrip("-").isdigit()
}


approved_users = load_json(USERS_FILE, [])
pending_users = load_json(PENDING_FILE, [])
vocab_bank = load_json(VOCAB_FILE, {})

approved_users = [
    int(x)
    for x in approved_users
    if str(x).lstrip("-").isdigit()
]

pending_users = [
    int(x)
    for x in pending_users
    if str(x).lstrip("-").isdigit()
]


# =========================================================
# ACCESS
# =========================================================

def is_owner(user_id):
    return OWNER_ID != 0 and user_id == OWNER_ID


# =========================================================
# USERS
# =========================================================

def is_approved(user_id):
    return is_owner(user_id) or user_id in approved_users


def add_approved(user_id):
    if user_id not in approved_users:
        approved_users.append(user_id)
        save_json(USERS_FILE, approved_users)


def remove_approved(user_id):
    if user_id in approved_users:
        approved_users.remove(user_id)
        save_json(USERS_FILE, approved_users)


def add_pending(user_id):
    if user_id not in pending_users:
        pending_users.append(user_id)
        save_json(PENDING_FILE, pending_users)


def remove_pending(user_id):
    if user_id in pending_users:
        pending_users.remove(user_id)
        save_json(PENDING_FILE, pending_users)


# =========================================================
# GROUPS
# =========================================================

GROUPS_FILE = Path("groups.json")

approved_groups = load_json(
    GROUPS_FILE,
    [],
)

approved_groups = [
    int(x)
    for x in approved_groups
    if str(x).lstrip("-").isdigit()
]


def is_group_approved(chat_id):
    return chat_id in approved_groups


def add_approved_group(chat_id):
    if chat_id not in approved_groups:
        approved_groups.append(chat_id)
        save_json(
            GROUPS_FILE,
            approved_groups,
        )


def remove_approved_group(chat_id):
    if chat_id in approved_groups:
        approved_groups.remove(chat_id)
        save_json(
            GROUPS_FILE,
            approved_groups,
        )


# =========================================================
# AUTO CORRECTION GROUP SETTINGS
# =========================================================

def is_autocorrect_enabled(chat_id):
    return chat_id not in disabled_autocorrect_groups


def set_autocorrect_enabled(chat_id, enabled):
    if enabled:
        disabled_autocorrect_groups.discard(chat_id)
    else:
        disabled_autocorrect_groups.add(chat_id)

    save_json(
        AUTOCORRECT_FILE,
        list(disabled_autocorrect_groups),
    )


# =========================================================
# VOCABULARY
# =========================================================

def save_vocab(user_id, word):
    uid_str = str(user_id)

    if uid_str not in vocab_bank:
        vocab_bank[uid_str] = []

    if word not in vocab_bank[uid_str]:
        vocab_bank[uid_str].append(word)
        save_json(
            VOCAB_FILE,
            vocab_bank,
        )


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(text):
    return (text or "").strip()


def get_reply_text(message):
    if not message or not message.reply_to_message:
        return ""

    replied = message.reply_to_message

    if replied.text:
        return replied.text.strip()

    if replied.caption:
        return replied.caption.strip()

    return ""


def get_arguments(message):
    if not message or not message.text:
        return ""

    parts = message.text.strip().split(maxsplit=1)

    if len(parts) == 2:
        return parts[1].strip()

    return ""


def get_target_text(message):
    reply = get_reply_text(message)

    if reply:
        return reply

    return get_arguments(message)


def get_pronunciation_target(message):
    """
    Supports:

    /us hello
    /us slowly hello

    /uk hello
    /uk slowly hello

    Also supports reply mode:

    Reply to a message + /us
    Reply to a message + /us slowly
    """

    arguments = get_arguments(message)
    reply = get_reply_text(message)

    slow = False
    text = ""

    if arguments:

        parts = arguments.split(maxsplit=1)

        if parts[0].lower() == "slowly":
            slow = True

            if len(parts) == 2:
                text = parts[1].strip()
            elif reply:
                text = reply

        else:
            text = arguments

    elif reply:
        text = reply

    return text, slow


def is_short_input(text):
    words = re.findall(r"[A-Za-z'-]+", text or "")
    return len(words) <= 3 and len(text or "") <= 80


def split_long_text(text, max_length=3900):
    result = []

    while len(text) > max_length:
        cut = text.rfind("\n", 0, max_length)

        if cut < 500:
            cut = text.rfind(" ", 0, max_length)

        if cut < 500:
            cut = max_length

        result.append(text[:cut])
        text = text[cut:].lstrip()

    if text:
        result.append(text)

    return result


def format_ai_response(text):
    """
    Convert AI markdown-style bold into real Telegram bold,
    remove unwanted stars, and keep bullet points clean.
    """

    if not text:
        return ""

    text = html.escape(str(text))

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(
        r"(?m)^\s*\*\s+",
        "• ",
        text,
    )

    text = text.replace("*", "")

    return text.strip()


async def send_long_reply(update, text, reply_markup=None):
    message = update.effective_message

    if not message:
        return

    formatted = format_ai_response(text)
    parts = split_long_text(formatted)

    for i, part in enumerate(parts):
        try:
            kwargs = {"parse_mode": "HTML"}
            # إضافة الزر فقط في الجزء الأخير من الرسالة
            if reply_markup and i == len(parts) - 1:
                kwargs["reply_markup"] = reply_markup
                
            await message.reply_text(
                part,
                **kwargs
            )

        except Exception as e:
            print(
                "Formatted AI reply error:",
                repr(e),
                flush=True,
            )
            
            kwargs = {}
            if reply_markup and i == len(parts) - 1:
                kwargs["reply_markup"] = reply_markup
                
            await message.reply_text(
                re.sub(
                    r"<[^>]+>",
                    "",
                    part,
                ),
                **kwargs
            )


# =========================================================
# GROQ ASYNC WRAPPER
# =========================================================

def _ask_groq_sync(prompt, max_tokens=1200, system_prompt=None):

    if not groq_client:
        return "❌ GROQ_API_KEY is not configured."

    try:
        sys_msg = system_prompt or (
            "You are FixMyEnglish, an accurate English-learning "
            "assistant for Arabic-speaking learners. "
            "Be natural, clear, concise, and useful. "
            "Do not sound robotic. "
            "Do not ask unnecessary follow-up questions. "
            "When explaining English to an Arabic speaker, use Arabic "
            "where it makes the explanation clearer. "
            "Never reveal internal reasoning. "
            "Keep answers organized with clear line breaks. "
            "Do not use decorative stars."
        )

        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": sys_msg,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0.3,
            max_completion_tokens=max_tokens,
            include_reasoning=False,
        )

        result = response.choices[0].message.content

        if not result:
            return "❌ Empty AI response."

        return result.strip()

    except Exception as e:
        print("Groq error:", repr(e), flush=True)
        return "❌ AI request failed. Please try again."


async def ask_groq(prompt, max_tokens=1200, system_prompt=None):

    try:

        return await asyncio.wait_for(
            asyncio.to_thread(
                _ask_groq_sync,
                prompt,
                max_tokens,
                system_prompt,
            ),
            timeout=45,
        )

    except asyncio.TimeoutError:

        print(
            "Groq TIMEOUT: request took too long.",
            flush=True,
        )

        return "❌ AI request timed out. Please try again."

    except Exception as e:

        print(
            "Groq wrapper error:",
            repr(e),
            flush=True,
        )

        return "❌ AI request failed. Please try again."


# =========================================================
# LANGUAGE FUNCTIONS
# =========================================================

async def free_ai(text):
    return await ask_groq(
        prompt=text,
        max_tokens=1500,
        system_prompt=(
            "You are the Free AI assistant of FixMyEnglish.\n\n"

            "Understand the user's text carefully before answering. "
            "Analyze the meaning, context, and intention, then give a clear "
            "and accurate answer.\n\n"

            "RESPONSE FORMAT AND STYLE:\n"
            "- Keep the answer clean, organized, and easy to read on a phone.\n"
            "- Put each separate piece of information on its own line.\n"
            "- Use short paragraphs instead of large blocks of text.\n"
            "- Use clear section headings when useful.\n"
            "- Separate major sections with this line:\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "- Put important words, key ideas, conclusions, and essential information "
            "in Telegram bold using double underscores, like __Important__.\n"
            "- Use numbered lists or hyphens (-) for lists.\n"
            "- NEVER use asterisks (*) anywhere in the response.\n"
            "- NEVER use decorative stars.\n"
            "- NEVER use * for bullet points or formatting.\n"
            "- Keep different information clearly separated.\n"
            "- Highlight only the most important information in bold.\n"
            "- When explaining something, give the main answer first, then details.\n"
            "- When comparing things, separate each side clearly.\n"
            "- Put each example on a separate line.\n"
            "- Avoid unnecessary repetition.\n"
            "- Do not ask unnecessary follow-up questions.\n"
            "- If the request is clear, answer it directly.\n"
            "- If information is uncertain, do not invent it.\n\n"

            "Make the final response look like a well-organized human-written "
            "answer, with clear spacing, separate sections, and important "
            "information highlighted."
        )
    )

async def pronunciation_info(text, dialect):
    country = "American" if dialect == "US" else "British"
    flag = "🇺🇸" if dialect == "US" else "🇬🇧"
    
    prompt = f"""
Provide the {country} English pronunciation (IPA) and the Arabic meaning for the following word/phrase: "{text}"

You must return ONLY the filled structure below. Do not use decorative stars.

🗣️ {country} Pronunciation
━━━━━━━━━━━━━━━━━━

🔤 Word:
{text}

{flag} IPA:
[write the real IPA here]

🇩🇿 Meaning:
[write the short Arabic meaning here]
━━━━━━━━━━━━━━━━━━
"""
    return await ask_groq(prompt, 350)


async def both_pronunciation(text):
    prompt = f"""
Provide both the American and British English pronunciation (IPA) and the Arabic meaning for the following word/phrase: "{text}"

You must return ONLY the filled structure below. Do not use decorative stars.

🗣️ Pronunciation
━━━━━━━━━━━━━━━━━━

🔤 Word:
{text}

🇺🇸 US:
[write the real American IPA here]

🇬🇧 UK:
[write the real British IPA here]

🇩🇿 Meaning:
[write the short Arabic meaning here]
━━━━━━━━━━━━━━━━━━
"""
    return await ask_groq(prompt, 350)


async def translate_text(text):
    return await ask_groq(
        f"""
Translate the following text.

Requirements:
- English → natural Arabic.
- Arabic → natural English.
- Preserve the exact meaning and tone.
- Do not explain the translation unless the text itself requires clarification.
- Do not add introductions such as "Here is the translation".
- Return the translation directly.
- Do not ask a question at the end.
- Do not use decorative stars.

Text:
{text}
""",
        1200,
    )


# =========================================================
# AI FUNCTIONS — NATURAL & ORGANIZED ANSWERS
# =========================================================

async def get_ipa_transcription(text, dialect):
    words_count = len(text.split())
    is_us = (dialect == "US")
    country = "American" if is_us else "British"
    flag = "🇺🇸" if is_us else "🇬🇧"
    
    # إذا كانت كلمة أو كلمتين: نعطي الفونيتيك + المعنى
    if words_count <= 2:
        prompt = f"""
Provide the {country} English pronunciation (IPA) and the Arabic meaning for the following: "{text}"

You must return ONLY the filled structure below. Do not use decorative stars.

🗣️ {country} Pronunciation
━━━━━━━━━━━━━━━━━━

🔤 Word:
{text}

{flag} IPA:
[write the real IPA here]

🇩🇿 Meaning:
[write the short Arabic meaning here]
━━━━━━━━━━━━━━━━━━
"""
    # إذا كانت الجملة أطول (3 كلمات فأكثر): نعطي الفونيتيك فقط
    else:
        prompt = f"""
Convert the following English text into {country} English IPA transcription.
Do NOT provide Arabic translations or explanations.

You must return ONLY the filled structure below. Do not use decorative stars.

🗣️ {country} Transcription
━━━━━━━━━━━━━━━━━━

🔤 Text:
{text}

{flag} IPA:
[write the IPA transcription of the whole text here]
━━━━━━━━━━━━━━━━━━
"""

    for attempt in range(4):
        result = await ask_groq(prompt, 600)
        
        if result and not result.startswith("❌"):
            return result.strip()
            
        if attempt < 3:
            await asyncio.sleep(1)
            
    return f"⚠️ I couldn't get the {country} IPA right now. Please try again."


async def syn_levels_text(text):
    prompt = f"""
Give synonyms and antonyms graded by CEFR levels (A1, A2, B1, B2, C1, C2) for this English word.

Word:
{text}

Use this EXACT structure. Do not use decorative stars.

📊 SYNONYMS & ANTONYMS BY LEVEL
━━━━━━━━━━━━━━━━━━

🔤 {text} — [main Arabic meaning]

━━━━━━━━━━━━━━━━━━

🟢 A1
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

🟡 A2
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

🔵 B1
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

🟣 B2
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

🔴 C1
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

⚫ C2
Syn: [synonym] — [Arabic meaning]
Ant: [antonym] — [Arabic meaning]

━━━━━━━━━━━━━━━━━━

Rules:
- Provide exactly one natural synonym and one natural antonym for each level if possible.
- If a level genuinely does not have a natural synonym or antonym, skip that specific Syn/Ant line, but try your best to find accurate graded words.
- Keep the Arabic translations very short and precise.
- Do not add introductions or follow-up questions.
- Maintain the color emojis for the levels as shown.
"""

    for attempt in range(4):
        result = await ask_groq(prompt, 1200)
        
        if result and not result.startswith("❌"):
            return result.strip()
            
        if attempt < 3:
            await asyncio.sleep(1)
            
    return "⚠️ I couldn't get the CEFR levels right now. Please try again."


async def correct_text(text):

    prompt = f"""
Correct and evaluate this English text for an Arabic-speaking learner.

Your job is to identify REAL and IMPORTANT mistakes only.

There are two possible cases:

CASE 1 — The text is correct:
- Clearly say that the text is correct.
- Then suggest ONE more natural or fluent way to say it, if a natural alternative exists.
- Make it clear that the alternative is a STYLE suggestion, not a correction.

Use:

✍️ CORRECTION
━━━━━━━━━━━━━━━━━━

✅ No important mistakes found.

💡 More natural:
[more natural version]

━━━━━━━━━━━━━━━━━━

🇩🇿 Meaning:
[natural Arabic meaning]

CASE 2 — The text contains mistakes:
- Identify only genuine mistakes.
- Show the original mistake → correction.
- Give a very short Arabic explanation.
- Then provide the corrected text.
- If useful, provide a more natural version separately.
- Do not treat a style preference as a mistake.

Use:

✍️ CORRECTION
━━━━━━━━━━━━━━━━━━

❌ Changes:

1️⃣ [mistake] → [correction]
🇩🇿 [very short explanation in Arabic]

2️⃣ [mistake] → [correction]
🇩🇿 [very short explanation in Arabic]

Only include changes that actually exist.

✅ Corrected:
[corrected text]

💡 More natural:
[more natural version only if it is genuinely useful]

━━━━━━━━━━━━━━━━━━

🇩🇿 Meaning:
[natural Arabic meaning]

IMPORTANT FOR LONG TEXT:
- Find the real mistakes throughout the whole text.
- List each useful correction briefly.
- Then give the complete corrected text.
- Do not rewrite the whole text merely for style.
- A natural version may be given separately if it adds real value.

Rules:
- Never invent a mistake.
- Do not criticize the learner.
- Do not give a long grammar lesson.
- Do not change correct wording just because you prefer another expression.
- Do not replace a correct word with a synonym just because it sounds more natural.
- If a word is obviously mistyped, infer the intended word when the context is clear.
- Distinguish clearly between CORRECTION and STYLE.
- If the text is already correct, do NOT say there is an error.
- Keep explanations short.
- Do not use decorative stars.
- Do not ask a follow-up question.
- Do not add unnecessary information.
- Always return a non-empty response.
- If the text is not actually English, do not invent corrections.

Text:
{text}
"""

    for attempt in range(4):
        try:
            result = await ask_groq(prompt, 1200)

            if result and result.strip():
                return result.strip()

        except Exception:
            pass

        if attempt < 3:
            await asyncio.sleep(1)

    return "⚠️ I couldn't check the text right now. Please try again."


async def explain_text(text):
    return await ask_groq(
        f"""
Explain this English word or expression to an Arabic-speaking learner.

The answer must be beautiful, organized, compact, and practical.

Use this structure:

📖 WORD EXPLANATION
━━━━━━━━━━━━━━━━━━

🔤 Word: [word] /[pronunciation]/
🏷️ Part of Speech: [verb / noun / adjective / adverb / expression]
🇩🇿 Main Meaning: [main Arabic meaning]

━━━━━━━━━━━━━━━━━━

📚 MEANINGS & USE

1️⃣ [Meaning 1]
[short, clear English definition]
🇩🇿 [Arabic meaning/explanation]

📝 Example: [natural English example]
🇩🇿 [natural Arabic translation]

🔄 Synonyms: [synonym] — [synonym] — [synonym]
🇩🇿 [Arabic meanings]

━━━━━━━━━━━━━━━━━━

2️⃣ [Meaning 2]
[short, clear English definition]
🇩🇿 [Arabic meaning/explanation]

📝 Example: [natural English example]
🇩🇿 [natural Arabic translation]

🔄 Synonyms: [synonym] — [synonym] — [synonym]
🇩🇿 [Arabic meanings]

━━━━━━━━━━━━━━━━━━

🔗 COMMON PATTERNS

[pattern / preposition / collocation]
🇩🇿 [Arabic meaning]

📝 Example: [natural English example]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

Rules:
- Always give examples.
- Every important meaning must have its own example.
- If there is only one common meaning, do not invent another.
- Put the most common meaning first.
- If the word has important verb and noun meanings, separate them clearly.
- Also separate adjective or adverb meanings when they are common and useful.
- Include common prepositions such as depend on, interested in, or aware of when genuinely relevant.
- Include useful common collocations and fixed expressions when relevant.
- Give synonyms that match the specific meaning.
- Do not invent meanings, synonyms, prepositions, collocations, or examples.
- Do not include rare dictionary meanings unless important.
- Keep explanations short and easy to understand.
- Use Arabic for meanings and explanations.
- Use natural English examples.
- Make Arabic translations natural, not word-for-word.
- Keep each item compact: English line + Arabic line.
- Do not use decorative stars.
- Do not repeat the same information.
- Do not force sections that are not relevant.
- Do not ask a follow-up question.
- Do not add introductions such as "Sure, let's break it down."
- Do not end with "Would you like me to...?"

Word/expression:
{text}
""",
        1100,
    )


async def synonyms_text(text):
    return await ask_groq(
        f"""
Give useful synonyms and genuine antonyms for this English word or expression.

The answer must be beautiful, organized, compact, and always include examples.

Use this structure:

🔄 SYNONYMS
━━━━━━━━━━━━━━━━━━

🔤 Word: [word]
🇩🇿 Meaning: [main Arabic meaning]

1️⃣ [Synonym] — [Arabic meaning]
📝 Example: [natural English sentence using the synonym]
🇩🇿 [Arabic translation]

2️⃣ [Synonym] — [Arabic meaning]
📝 Example: [natural English sentence using the synonym]
🇩🇿 [Arabic translation]

3️⃣ [Synonym] — [Arabic meaning]
📝 Example: [natural English sentence using the synonym]
🇩🇿 [Arabic translation]

4️⃣ [Synonym] — [Arabic meaning]
📝 Example: [natural English sentence using the synonym]
🇩🇿 [Arabic translation]

5️⃣ [Synonym] — [Arabic meaning]
📝 Example: [natural English sentence using the synonym]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

↔️ ANTONYMS
━━━━━━━━━━━━━━━━━━

1️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

2️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

Only include genuine and natural antonyms.

Rules:
- Give up to 5 useful synonyms.
- Always give an example for each useful synonym.
- Give genuine antonyms when they naturally exist.
- Always give an example for each antonym.
- Prefer common, natural words.
- Explain an important difference in meaning or usage briefly when needed.
- If fewer natural synonyms or antonyms exist, give fewer.
- Never invent words just to fill the list.
- If the word has different meanings, choose synonyms according to the specific meaning.
- Keep the answer compact.
- Do not use decorative stars.
- Do not ask a follow-up question.
- Do not add unnecessary introduction.

Word:
{text}
""",
        1100,
    )


async def antonyms_text(text):
    return await ask_groq(
        f"""
Give genuine and useful antonyms for this English word or expression.

The answer must be beautiful, organized, compact, and always include examples.

Use this structure:

↔️ ANTONYMS
━━━━━━━━━━━━━━━━━━

🔤 Word: [word]
🇩🇿 Meaning: [main Arabic meaning]

1️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

2️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

3️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

4️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

5️⃣ [Antonym] — [Arabic meaning]
📝 Example: [natural English sentence using the antonym]
🇩🇿 [Arabic translation]

Rules:
- Give genuine antonyms only.
- Always give an example for every antonym.
- Prefer common and useful words.
- If only one or two natural antonyms exist, give only those.
- Do not invent an opposite.
- Avoid repetition.
- Consider the specific meaning of the word.
- If the word has several meanings, choose antonyms for the relevant meaning.
- Keep the answer concise.
- Do not use decorative stars.
- Keep each item compact: English line + Arabic line.
- Do not ask a follow-up question.

Word:
{text}
""",
        900,
    )


async def use_word(text):
    return await ask_groq(
        f"""
Explain how to use this English word or expression naturally.

The answer must be beautiful, organized, compact, and practical.

Use this structure:

🧩 WORD USAGE
━━━━━━━━━━━━━━━━━━

🔤 Word: [word]
🇩🇿 Meaning: [main Arabic meaning]

━━━━━━━━━━━━━━━━━━

📚 HOW TO USE IT

[short practical explanation]
🇩🇿 [Arabic explanation]

━━━━━━━━━━━━━━━━━━

🔗 COMMON PATTERNS

[preposition / collocation / sentence pattern]
🇩🇿 [Arabic meaning]

📝 Example: [natural English example]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

📝 EXAMPLES

1️⃣ [natural English example with the target word]
🇩🇿 [Arabic translation]

2️⃣ [natural English example with the target word]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

Rules:
- Always give examples.
- Focus on real, common usage.
- Mention important prepositions and patterns.
- Mention different common meanings when they affect usage.
- If noun and verb usage differ, distinguish them.
- Give natural examples, not artificial sentences.
- Do not invent expressions.
- Do not over-explain grammar.
- Do not force sections that are not useful.
- Keep the answer concise and practical.
- Do not use decorative stars.
- Keep each item compact: English line + Arabic line.
- Do not ask a follow-up question.
- Do not add a long introduction.

Word:
{text}
""",
        1100,
    )


# =========================================================
# WORD ROOT
# =========================================================

async def root_word(text):
    prompt = f"""
Analyze the English word and explain its underlying classical root
(Latin, Greek, or another important source root) when one genuinely exists.

The purpose is to help an English learner understand how the word
was formed and how the same root appears in other English words.

Word:
{text}

Use this structure:

🌱 **WORD ROOT & MORPHOLOGY**
━━━━━━━━━━━━━━━━━━

🔤 **Word:** [original word]

🌱 **Root:** [root]
🇩🇿 **Core meaning:** [Arabic meaning of the root]

📌 **Root idea:** [short, clear explanation of the original/basic idea
of the root, such as "throw / cast" for JECT]

━━━━━━━━━━━━━━━━━━

🧩 **WORD STRUCTURE**

🔹 **Prefix:** [prefix or None]
🇩🇿 [meaning of the prefix]

🔹 **Root:** [root]

🔹 **Suffix:** [suffix or None]
🇩🇿 [meaning of the suffix]

🔗 **Formation:**
[prefix] + [root] + [suffix] → [word]

━━━━━━━━━━━━━━━━━━

🌿 **WORDS BUILT FROM THE SAME ROOT**

1️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
🧩 [prefix] + [root] → [short explanation of how the root contributes to the meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

2️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
🧩 [prefix] + [root] → [short explanation]
📝 [natural English example]
🇩🇿 [Arabic translation]

3️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
🧩 [prefix] + [root] → [short explanation]
📝 [natural English example]
🇩🇿 [Arabic translation]

4️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
🧩 [prefix] + [root] → [short explanation]
📝 [natural English example]
🇩🇿 [Arabic translation]

5️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
🧩 [prefix] + [root] → [short explanation]
📝 [natural English example]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

💡 **ROOT IN ONE IDEA**
[Give one short sentence explaining the central idea of the root
and how the prefixes change its meaning.]

Rules:
- Focus on the actual classical root, not synonyms.
- A root is NOT the same thing as a word family.
- Prefer important Latin and Greek roots that help learners understand
  many English words.
- For example, JECT means roughly "throw / cast"; show how prefixes
  such as IN-, RE-, E-, PRO-, OB-, SUB-, DE-, or INTER- change the idea.
- Identify the prefix and suffix when they genuinely exist.
- Explain the meaning contributed by the prefix.
- Show how the root contributes to the final meaning.
- Include 4–6 common and useful English words built from the same root.
- Do not include words merely because they are synonyms.
- Do not include unrelated words with similar spelling.
- Do not invent etymological relationships.
- If a word has no useful or reliable classical root, say so briefly
  instead of inventing one.
- Keep etymology accurate but learner-friendly.
- Examples must be natural, short, and useful.
- Every derived word must have an Arabic meaning and an English example
  with Arabic translation.
- Use **bold** for important words and headings.
- Do not use decorative star characters such as * or ** as visible text.
- Markdown bold formatting is allowed.
- Keep the answer organized, beautiful, and concise.
- Do not ask a follow-up question.

Word:
{text}
"""

    for attempt in range(4):
        result = await ask_groq(prompt, 1600)
        
        if result and not result.startswith("❌"):
            return result.strip()
            
        if attempt < 3:
            await asyncio.sleep(1)
            
    return "⚠️ I couldn't get the word root right now. Please try again."


# =========================================================
# WORD FAMILY
# =========================================================

async def word_family(text):
    prompt = f"""
Give the English word family of:

{text}

The goal is to show the different grammatical and derivational forms
that belong to the same English word family.

Use this structure:

🧩 **WORD FAMILY**
━━━━━━━━━━━━━━━━━━

🔤 **Base word:** [base word]
🇩🇿 **Meaning:** [Arabic meaning]

━━━━━━━━━━━━━━━━━━

🌿 **FAMILY MEMBERS**

1️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

2️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

3️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

4️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

5️⃣ **[word]** — [part of speech]
🇩🇿 [Arabic meaning]
📝 [natural English example]
🇩🇿 [Arabic translation]

━━━━━━━━━━━━━━━━━━

💡 **PATTERN**
[Briefly explain how the forms are related:
verb → noun, adjective → adverb, negative form, etc.]

Rules:
- Focus ONLY on the word family.
- Do NOT explain the classical/Latin/Greek root.
- Do NOT give synonyms unless they are genuinely part of the same
  word family.
- Include common and useful members of the family.
- Include noun, verb, adjective, and adverb forms when they genuinely exist.
- Include negative forms with prefixes only when they are genuinely
  established members of the family.
- Do not invent forms.
- Do not include words merely because they have a similar meaning.
- Every word must have its Arabic meaning and a natural English example
  with Arabic translation.
- Show the part of speech clearly.
- Prefer common modern English forms.
- Use **bold** for important words and headings.
- Do not use decorative star characters such as * or ** as visible text.
- Markdown bold formatting is allowed.
- Keep the answer organized, beautiful, and concise.
- Do not ask a follow-up question.

Word:
{text}
"""

    for attempt in range(4):
        result = await ask_groq(prompt, 1500)
        
        if result and not result.startswith("❌"):
            return result.strip()
            
        if attempt < 3:
            await asyncio.sleep(1)
            
    return "⚠️ I couldn't get the word family right now. Please try again."


# =========================================================
# TALK MODE
# =========================================================

async def talk_with_ai(text, user_name):

    sys_prompt = (
        f"You are FixMyEnglish, a natural and friendly conversation "
        f"companion chatting with {user_name}. "

        "Your conversation should feel like a normal human conversation, "
        "not like a customer-service bot, tutor script, or interview. "

        "Respond to what the user actually says. "
        "Do not use scripted greetings. "
        "Do not repeat greetings. "
        "Do not ask a question after every message. "
        "Do not ask questions merely to keep the conversation alive. "
        "Do not constantly suggest random topics. "

        "Never repeatedly say things like "
        "'What's on your mind?', "
        "'What would you like to talk about?', or "
        "'Would you like to practice English?' "

        "If the user says something funny, respond naturally to the joke. "
        "You can laugh, be light, witty, or playful when it fits. "
        "If the user is joking, joke back naturally. "
        "Do not force jokes when the user is serious. "

        "If the user asks a serious question, answer it seriously "
        "and directly. "

        "If the user gives a direct instruction, follow it directly "
        "instead of asking unnecessary questions. "

        "If the user asks you to make a dua for someone, give an "
        "appropriate dua directly. "

        "If the user asks for a joke, give a joke directly. "

        "If the user asks for translation, explanation, correction, "
        "or another clear task, do the task directly. "

        "If the user shares something, react naturally to what they shared "
        "instead of immediately turning it into a question. "

        "If the user is practicing English, help naturally. "
        "Correct English only when there is a useful mistake to correct. "
        "Do not turn every message into a grammar lesson. "
        "Keep corrections brief and practical. "

        "Use mostly English, but use Arabic when it helps the learner "
        "understand something clearly. "

        "Keep responses proportional to the user's message. "
        "Short message = usually short response. "
        "Longer message = respond appropriately to its content. "

        "Do not end every response with a question. "
        "It is completely fine to finish with a natural statement. "

        "Do not offer unrelated help or random suggestions. "
        "Do not sound repetitive or robotic. "

        "For longer answers, organize them with clear line breaks. "
        "Use a short title or separator only when useful. "
        "Mark important words clearly. "
        "Do not use decorative stars. "
        "Do not turn every casual reply into a formatted lesson."
    )

    return await ask_groq(
        text,
        800,
        system_prompt=sys_prompt,
    )


# =========================================================
# AUTOMATIC CORRECTION
# =========================================================

async def auto_correct_chat(text):

    prompt = f"""
Check this English message for REAL and IMPORTANT mistakes only.

Your job is automatic correction, not rewriting.

Rules:

1. If the message is a single English word:
   - If the spelling is correct, return exactly: OK
   - If there is an obvious spelling/typing mistake, give the correct word.
   - Do not replace a correct word with another word just because another
     word sounds more natural.

2. If the message is an English sentence or text:
   - Correct genuine spelling mistakes.
   - Correct genuine grammar mistakes.
   - Correct a wrong or missing word when the meaning clearly requires it.
   - Do not rewrite correct sentences for style.
   - Do not make the sentence more advanced.
   - Do not change correct wording just because you prefer another style.

3. If there is no important mistake:
   return exactly:
   OK

4. If there is a mistake:
   Use this short format:

   Original:
   [original]

   Correct:
   [corrected version]

   Why:
   [very short explanation]

5. Never invent a mistake.
6. Do not give a grammar lesson.
7. Do not ask a question.
8. Keep the response short.
9. If the text is not actually English, return exactly: OK
10. Do not use decorative stars.
11. IMPORTANT: Always return a response. Never return an empty response.

Text:
{text}
"""

    for attempt in range(4):
        try:
            result = await ask_groq(prompt, 350)

            if result and result.strip():
                return result.strip()

        except Exception:
            pass

        if attempt < 3:
            await asyncio.sleep(1)

    return "⚠️ I couldn't check the sentence right now. Please try again."


# =========================================================
# TEXT TO SPEECH
# =========================================================

async def make_audio(text, voice, slow=False):

    filename = None

    try:
        fd, filename = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        rate = "-30%" if slow else "+0%"

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            connect_timeout=10,
            receive_timeout=20,
        )

        await asyncio.wait_for(
            communicate.save(filename),
            timeout=30,
        )

        if not os.path.exists(filename):
            print(
                "TTS error: audio file was not created.",
                flush=True,
            )
            return None

        if os.path.getsize(filename) == 0:
            print(
                "TTS error: audio file is empty.",
                flush=True,
            )

            try:
                os.remove(filename)
            except Exception:
                pass

            return None

        return filename

    except asyncio.TimeoutError:

        print(
            "TTS TIMEOUT: edge_tts took too long.",
            flush=True,
        )

        if filename:
            try:
                os.remove(filename)
            except Exception:
                pass

        return None

    except Exception as e:

        print(
            "TTS error:",
            repr(e),
            flush=True,
        )

        if filename:
            try:
                os.remove(filename)
            except Exception:
                pass

        return None


async def send_pronunciation(update, word, dialect, slow=False):

    message = update.effective_message

    if not message:
        return

    word = (word or "").strip()

    if not word:
        return

    word_count = len(word.split())

    if word_count > 500:
        await message.reply_text(
            "❌ The text is too long for pronunciation.\n"
            "The maximum is 500 words."
        )
        return

    # تجهيز اللهجة ورابط YouGlish
    encoded_word = quote(word)
    if dialect == "US":
        voice = US_VOICE
        yg_url = f"https://youglish.com/pronounce/{encoded_word}/english/us"
    elif dialect == "UK":
        voice = UK_VOICE
        yg_url = f"https://youglish.com/pronounce/{encoded_word}/english/uk"
    else:
        voice = US_VOICE
        yg_url = f"https://youglish.com/pronounce/{encoded_word}/english"

    # إنشاء زر YouGlish
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎧 YouGlish", url=yg_url)]]
    )

    if word_count <= 3:

        if dialect == "US":
            info = await pronunciation_info(
                word,
                "US",
            )

        elif dialect == "UK":
            info = await pronunciation_info(
                word,
                "UK",
            )

        else:
            info = await both_pronunciation(word)

        # إرسال رسالة الـ IPA مع الزر
        await send_long_reply(
            update,
            info,
            reply_markup=keyboard
        )

    audio = await make_audio(
        word,
        voice,
        slow=slow,
    )

    if not audio:
        await message.reply_text(
            "❌ I couldn't create the pronunciation audio."
        )
        return

    try:
        with open(audio, "rb") as f:
            caption = (
                "🔊 Slow pronunciation"
                if slow
                else "🔊 Pronunciation"
            )

            await message.reply_audio(
                audio=f,
                caption=caption,
            )

    except Exception as e:
        print(
            "Telegram audio error:",
            repr(e),
            flush=True,
        )

        await message.reply_text(
            "❌ I couldn't send the pronunciation audio."
        )

    finally:
        try:
            if os.path.exists(audio):
                os.remove(audio)

        except Exception as e:
            print(
                "Audio cleanup error:",
                repr(e),
                flush=True,
            )


# =========================================================
# DUAS
# =========================================================

DUAS = [
    "May Allah bless you with beneficial knowledge, wisdom, and success.",
    "May Allah increase you in knowledge, understanding, and goodness.",
    "May Allah open the doors of beneficial knowledge for you.",
    "May Allah bless your time, your efforts, and everything you learn.",
    "May Allah guide you to what is good and make your path easy.",
    "May Allah grant you knowledge that benefits you and benefits others.",
    "May Allah increase you in faith, knowledge, wisdom, and good character.",
    "May Allah make your journey toward knowledge full of blessings and success.",
    "May Allah make every difficulty easy for you and every good effort fruitful.",
    "May Allah bless your mind with understanding and your heart with peace.",
    "May Allah grant you clarity, patience, and success in all that is good.",
    "May Allah make your knowledge a source of benefit in this life and the Hereafter.",
    "May Allah bless you with sincere intentions and beneficial actions.",
    "May Allah increase you in wisdom and guide you to the best choices.",
    "May Allah make learning easy for you and put barakah in your efforts.",
    "May Allah grant you success beyond what you expect and goodness beyond what you imagine.",
    "May Allah protect you, guide you, and surround you with His mercy.",
    "May Allah grant you a heart full of gratitude, patience, and peace.",
    "May Allah bless every step you take toward knowledge and righteousness.",
    "May Allah make your efforts sincere, your knowledge beneficial, and your path blessed.",
    "May Allah open for you doors of understanding that you never expected.",
    "May Allah grant you strength when learning is difficult and patience when progress is slow.",
    "May Allah put light in your heart, clarity in your mind, and barakah in your time.",
    "May Allah make you a source of benefit and goodness wherever you go.",
    "May Allah grant you success in your studies and bless you with lasting knowledge.",
    "May Allah make your pursuit of knowledge a means of drawing closer to Him.",
    "May Allah reward your efforts, forgive your shortcomings, and increase you in goodness.",
    "May Allah grant you beneficial knowledge, lawful provision, good health, and a peaceful heart.",
    "May Allah guide you whenever you are uncertain and strengthen you whenever you struggle.",
    "May Allah bless your future and make it better than you hope.",
    "May Allah grant you excellence in what you learn and wisdom in how you use it.",
    "May Allah make every page you read and every word you learn a source of benefit.",
    "May Allah bless your memory, strengthen your understanding, and make learning easy for you.",
    "May Allah grant you patience with yourself and consistency in seeking knowledge.",
    "May Allah give you the ability to understand, remember, and apply beneficial knowledge.",
    "May Allah make your knowledge a light for you and a benefit to those around you.",
    "May Allah bless your dreams, guide your steps, and grant you what is best for you.",
    "May Allah replace every difficulty with ease and every worry with peace.",
    "May Allah grant you sincerity in your intentions and excellence in your actions.",
    "May Allah keep you steadfast upon goodness and guide you to what pleases Him.",
    "May Allah bless your journey, protect you from harm, and grant you a beautiful future.",
    "May Allah grant you courage to continue, patience to persevere, and wisdom to learn.",
    "May Allah make your efforts today a reason for greater blessings tomorrow.",
    "May Allah grant you success in this world and lasting success in the Hereafter.",
    "May Allah fill your life with beneficial knowledge, righteous deeds, and peaceful moments.",
    "May Allah guide your heart, enlighten your mind, and bless your endeavors.",
    "May Allah grant you the best of what you seek and protect you from what harms you.",
    "May Allah make you among those who learn, understand, practice, and teach what is good.",
    "May Allah bless you with good companions, beneficial knowledge, and a righteous path.",
    "May Allah make your future bright with faith, knowledge, goodness, and success.",
    "May Allah give you strength to overcome every obstacle and wisdom to learn from every experience.",
    "May Allah grant you peace in your heart, clarity in your thoughts, and blessings in your life.",
    "May Allah make every sincere effort you make a reason for reward and goodness.",
    "May Allah grant you a beautiful character, beneficial knowledge, and a heart attached to goodness.",
    "May Allah protect your heart from despair and fill it with hope, patience, and trust in Him.",
    "May Allah bless you with opportunities that bring you closer to what is good.",
    "May Allah make your learning journey enjoyable, beneficial, and full of barakah.",
    "May Allah grant you understanding deeper than memorization and wisdom greater than information.",
    "May Allah bless what you know, teach you what you do not know, and benefit you through both.",
    "May Allah make your knowledge a means of helping yourself, your family, and your community.",
    "May Allah grant you steadfastness when the road is difficult and gratitude when it becomes easy.",
    "May Allah open your heart to knowledge and make you among those who act upon what they learn.",
    "May Allah bless your days with purpose, your nights with peace, and your efforts with success.",
    "May Allah grant you what is good for you, even when you do not know what is best for yourself.",
    "May Allah guide you toward people and opportunities that bring goodness into your life.",
    "May Allah make your knowledge increase your humility, your wisdom, and your kindness.",
    "May Allah grant you success in every beneficial pursuit and protect you from wasted effort.",
    "May Allah bless your path with knowledge, patience, sincerity, and beautiful results.",
    "May Allah make you better with every day and closer to Him with every step.",
    "May Allah grant you a life filled with beneficial knowledge, righteous deeds, and sincere friendships.",
    "May Allah give you the strength to keep learning even when progress seems slow.",
    "May Allah reward your patience and make the fruits of your efforts greater than you expect.",
    "May Allah grant you wisdom to know what matters, courage to pursue it, and patience to continue.",
    "May Allah put barakah in everything beneficial that you learn and teach.",
    "May Allah make your words beneficial, your actions sincere, and your intentions pure.",
    "May Allah protect you from harmful knowledge and guide you toward knowledge that brings benefit.",
    "May Allah grant you success with humility and knowledge with wisdom.",
    "May Allah make your journey of learning a journey of growth, goodness, and closeness to Him.",
    "May Allah bless you with a peaceful heart and a mind eager to learn what is beneficial.",
    "May Allah grant you opportunities to use your knowledge in ways that benefit others.",
    "May Allah make your efforts a source of goodness for you in this life and the next.",
    "May Allah grant you patience during hardship and gratitude during ease.",
    "May Allah guide you to the best path and grant you the strength to remain upon it.",
    "May Allah bless you with knowledge that changes your life for the better.",
    "May Allah make your future filled with goodness, growth, peace, and success.",
    "May Allah increase you in every kind of goodness and protect you from every kind of harm.",
    "May Allah bless your heart with faith, your mind with understanding, and your life with barakah.",
    "May Allah grant you success in your studies, your work, your relationships, and your worship.",
    "May Allah make you a person whose knowledge benefits others long after you learn it.",
    "May Allah accept your sincere efforts and multiply the goodness that comes from them.",
    "May Allah grant you a clear mind, a strong heart, and the patience to keep moving forward.",
    "May Allah make every beneficial thing you learn a lasting part of your character.",
    "May Allah guide you toward what is best and keep you away from what would harm you.",
    "May Allah grant you wisdom in speech, kindness in action, and sincerity in your heart.",
    "May Allah make your pursuit of knowledge a source of light, benefit, and reward.",
    "May Allah bless your efforts today and allow their goodness to continue into tomorrow.",
    "May Allah grant you success without arrogance, knowledge without pride, and goodness without showing off.",
    "May Allah make your heart strong, your intentions sincere, and your journey blessed.",
    "May Allah grant you the patience to learn, the wisdom to understand, and the courage to apply what you learn.",
    "May Allah fill your life with moments that increase you in faith, knowledge, gratitude, and peace.",
    "May Allah grant you beneficial knowledge and make you a means through which others benefit.",
    "May Allah bless your future with opportunities that bring you closer to goodness.",
    "May Allah make every sincere step you take toward knowledge a step toward greater goodness.",
    "May Allah grant you a heart that loves goodness and a mind that seeks beneficial knowledge.",
    "May Allah protect you wherever you go and guide you wherever you turn.",
    "May Allah grant you ease after hardship, hope after difficulty, and success after sincere effort.",
    "May Allah bless your life with people who encourage you toward goodness and knowledge.",
    "May Allah make you grateful for what you have, patient with what you lack, and hopeful for what is to come.",
    "May Allah grant you strength, wisdom, and sincerity in every beneficial thing you pursue.",
    "May Allah make your learning a source of confidence, humility, and positive change.",
    "May Allah bless you with knowledge that benefits your heart, your mind, and your actions.",
    "May Allah grant you success in ways that bring you closer to Him and benefit those around you.",
    "May Allah make your efforts meaningful, your progress steady, and your future blessed.",
    "May Allah grant you a peaceful heart and a purposeful life filled with beneficial deeds.",
    "May Allah bless every good intention in your heart and every sincere effort you make.",
    "May Allah make your knowledge a source of guidance, your character a source of goodness, and your life a source of benefit."
]


# =========================================================
# NAME DETECTION
# =========================================================

NAME_PATTERNS = [
    r"عبد\s*الكريم",
    r"كريمو",
    r"كريم",
    r"\bkarim\b",
    r"\bkarimo\b",
    r"\babdelkarim\b",
    r"\babdel\s*karim\b",
    r"\babd\s*el\s*karim\b",
    r"\babdlkrim\b",
]


def name_is_mentioned(text):
    if not text:
        return False

    normalized = text.lower().strip()

    normalized = re.sub(
        r"[إأآا]",
        "ا",
        normalized,
    )

    normalized = re.sub(
        r"ى",
        "ي",
        normalized,
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    for pattern in NAME_PATTERNS:
        if re.search(
            pattern,
            normalized,
            re.IGNORECASE,
        ):
            return True

    return False


async def name_is_tagged(update, context):
    global OWNER_USERNAME

    message = update.effective_message

    if not message:
        return False

    sender = update.effective_user

    if (
        sender
        and sender.id == OWNER_ID
        and sender.username
    ):
        OWNER_USERNAME = sender.username.lower()

    entities = []

    if message.entities:
        entities.extend(message.entities)

    if message.caption_entities:
        entities.extend(message.caption_entities)

    text = message.text or message.caption or ""

    for entity in entities:

        if entity.type == "mention":

            if not OWNER_USERNAME:
                continue

            mentioned_username = text[
                entity.offset:
                entity.offset + entity.length
            ].lower().lstrip("@")

            if mentioned_username == OWNER_USERNAME:
                return True

        elif entity.type == "text_mention":

            if (
                entity.user
                and entity.user.id == OWNER_ID
            ):
                return True

    return False


async def name_reaction(update, context):

    message = update.effective_message

    if not message:
        return False

    text = message.text or message.caption or ""

    mentioned = name_is_mentioned(text)

    if not mentioned:
        mentioned = await name_is_tagged(
            update,
            context,
        )

    if not mentioned:
        return False

    sender = update.effective_user

    if sender:
        sender_name = (
            sender.first_name
            or sender.full_name
            or "friend"
        )
    else:
        sender_name = "friend"

    try:
        await context.bot.set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.message_id,
            reaction=[
                ReactionTypeEmoji("❤️")
            ],
        )

    except Exception as e:
        print(
            "Reaction error:",
            repr(e),
            flush=True,
        )

    dua = random.choice(DUAS).strip()

    await message.reply_text(
        f"🤲 {sender_name}, {dua}"
    )

    return True


# =========================================================
# HELP
# =========================================================

HELP_TEXT = """
📚 <b>FixMyEnglish — Commands</b>

🌍 <b>Translation</b>
/tr text
ترجم text

✍️ <b>Correction</b>
/cor text
صحح text

📖 <b>Explanation</b>
/ex word
اشرح word

🌱 <b>Word Root</b>
/root word
جذر word

🧩 <b>Word Family</b>
/fw word
عائلة word

🔄 <b>Synonyms</b>
/syn word
مرادف word

🔻 <b>Antonyms</b>
/ant word
ضد word

📊 <b>Levels (CEFR)</b>
/levels word
مستويات word

🧩 <b>Word Usage</b>
/use word
وظف word

📝 <b>IPA Transcription (Text Only)</b>
/ipaus text
فوناتيك_امريكي text
/ipauk text
فوناتيك_بريطاني text

🇺🇸 <b>American TTS</b>
/us word
/us slowly word
امريكي word
امريكي بطيء word

🇬🇧 <b>British TTS</b>
/uk word
/uk slowly word
بريطاني word
بريطاني بطيء word

🗣️ <b>Both TTS</b>
/pr word
انطق word
انطق بطيء word

🤖 <b>AI</b>
/ai your request

💬 <b>Talk Mode</b>
/talk أو تكلم — التحدث مع البوت كصديق

📚 <b>Vocabulary</b>
/vocab — عرض الكلمات المحفوظة

⚙️ <b>Group Auto Correction</b>
/on — تشغيل التصحيح التلقائي
/off — إيقاف التصحيح التلقائي

💬 <b>Reply mode</b>

Reply to a message and send:

/tr
/cor
/ex
/root
/fw
/syn
/ant
/levels
/use
/ipaus
/ipauk
/us
/uk
/pr

The command will be applied to the message you replied to.
"""


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if chat.type in [
        "group",
        "supergroup",
    ]:

        await update.effective_message.reply_text(
            "👋 Hello! <b>FixMyEnglish Pro</b> is active in this group and ready to help everyone.",
            parse_mode="HTML",
        )

        return

    if not is_approved(user.id):

        add_pending(user.id)

        await update.effective_message.reply_text(
            "👋 Welcome to FixMyEnglish!\n\n"
            "🔐 Your access request has been sent to the owner.\n"
            "Please wait for approval."
        )

        await notify_owner(
            update,
            user,
        )

        return

    await update.effective_message.reply_text(
        "👋 Welcome to <b>FixMyEnglish Pro</b>!\n\n"
        "🌍 Translation\n"
        "✍️ English correction\n"
        "📖 Word explanations\n"
        "🔄 Synonyms & antonyms\n"
        "🧩 Word usage\n"
        "🇺🇸 American pronunciation\n"
        "🇬🇧 British pronunciation\n"
        "🔊 Pronunciation audio\n"
        "🤖 AI assistant\n"
        "💬 Talk mode (/talk)\n\n"
        "📚 Send /help to see all commands.",
        parse_mode="HTML",
    )


async def help_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    await update.effective_message.reply_text(
        HELP_TEXT,
        parse_mode="HTML",
    )


async def talk_command(update, context):

    user = update.effective_user
    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(user.id)
    ):
        return

    if user.id in talk_mode_users:

        talk_mode_users.remove(user.id)

        await update.effective_message.reply_text(
            "💤 Talk mode disabled."
        )

    else:

        talk_mode_users.add(user.id)

        if chat.type in [
            "group",
            "supergroup",
        ]:
            await update.effective_message.reply_text(
                "💬 Talk mode enabled.\n"
                "Reply to one of my messages to talk with me."
            )
        else:
            await update.effective_message.reply_text(
                "💬 Talk mode enabled.\n"
                "You can chat naturally with me."
            )


async def vocab_command(update, context):

    user = update.effective_user
    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(user.id)
    ):
        return

    uid_str = str(user.id)

    words = vocab_bank.get(
        uid_str,
        [],
    )

    if not words:

        await update.effective_message.reply_text(
            "📭 Your vocabulary bank is empty."
        )

        return

    text = (
        "📚 <b>Your Saved Words:</b>\n\n"
        + "\n".join(
            f"• {w}"
            for w in words
        )
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# AUTO CORRECTION COMMANDS
# =========================================================

async def on_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in [
        "group",
        "supergroup",
    ]:
        await update.effective_message.reply_text(
            "⚙️ This command is for groups only."
        )
        return

    if not is_group_approved(chat.id):
        return

    set_autocorrect_enabled(
        chat.id,
        True,
    )

    await update.effective_message.reply_text(
        "✅ Automatic correction is now ON in this group."
    )


async def off_command(update, context):

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in [
        "group",
        "supergroup",
    ]:
        await update.effective_message.reply_text(
            "⚙️ This command is for groups only."
        )
        return

    if not is_group_approved(chat.id):
        return

    set_autocorrect_enabled(
        chat.id,
        False,
    )

    await update.effective_message.reply_text(
        "💤 Automatic correction is now OFF in this group."
    )


# =========================================================
# ACCESS REQUEST
# =========================================================

async def notify_owner(update, user):

    if OWNER_ID == 0:
        return

    try:

        name = user.full_name or "Unknown"

        username = (
            f"@{user.username}"
            if user.username
            else "No username"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"approve:{user.id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=f"reject:{user.id}",
                    ),
                ]
            ]
        )

        await context_bot_send(
            update,
            (
                "🔔 <b>New access request</b>\n\n"
                f"👤 Name: {name}\n"
                f"🔗 Username: {username}\n"
                f"🆔 ID: <code>{user.id}</code>"
            ),
            keyboard,
        )

    except Exception as e:

        print(
            "Notify owner error:",
            repr(e),
            flush=True,
        )


async def notify_owner_group(update, chat):

    if OWNER_ID == 0:
        return

    try:

        group_name = chat.title or "Unknown group"

        username = (
            f"@{chat.username}"
            if chat.username
            else "No username"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"approve_group:{chat.id}",
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=f"reject_group:{chat.id}",
                    ),
                ]
            ]
        )

        await context_bot_send(
            update,
            (
                "👥 <b>New group access request</b>\n\n"
                f"📌 Name: {group_name}\n"
                f"🔗 Username: {username}\n"
                f"🆔 ID: <code>{chat.id}</code>"
            ),
            keyboard,
        )

    except Exception as e:

        print(
            "Notify group owner error:",
            repr(e),
            flush=True,
        )


async def context_bot_send(
    update,
    text,
    keyboard,
):

    await update.get_bot().send_message(
        chat_id=OWNER_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def group_access_request(update, context):

    chat = update.effective_chat

    if not chat:
        return

    if chat.type not in [
        "group",
        "supergroup",
    ]:
        return

    if is_group_approved(chat.id):
        return

    await notify_owner_group(
        update,
        chat,
    )


async def access_callback(update, context):

    query = update.callback_query

    if not query:
        return

    if not is_owner(query.from_user.id):

        await query.answer(
            "Owner only.",
            show_alert=True,
        )

        return

    await query.answer()

    data = query.data or ""

    if ":" not in data:
        return

    action, target_id_text = data.split(
        ":",
        1,
    )

    try:
        target_id = int(target_id_text)

    except ValueError:
        return

    # =====================================================
    # USER APPROVAL
    # =====================================================

    if action == "approve":

        add_approved(target_id)
        remove_pending(target_id)

        await query.edit_message_text(
            f"✅ User <code>{target_id}</code> approved.",
            parse_mode="HTML",
        )

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "✅ Your access to FixMyEnglish "
                    "has been approved!"
                ),
            )

        except Exception as e:

            print(
                "Approval message error:",
                repr(e),
                flush=True,
            )

    elif action == "reject":

        remove_pending(target_id)

        await query.edit_message_text(
            f"❌ User <code>{target_id}</code> rejected.",
            parse_mode="HTML",
        )

        try:

            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "❌ Your access request "
                    "was not approved."
                ),
            )

        except Exception as e:

            print(
                "Reject message error:",
                repr(e),
                flush=True,
            )

    # =====================================================
    # GROUP APPROVAL
    # =====================================================

    elif action == "approve_group":

        add_approved_group(target_id)

        await query.edit_message_text(
            (
                "✅ <b>Group approved.</b>\n\n"
                f"🆔 ID: <code>{target_id}</code>"
            ),
            parse_mode="HTML",
        )

        try:

            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "✅ <b>FixMyEnglish has been approved "
                    "for this group.</b>"
                ),
                parse_mode="HTML",
            )

        except Exception as e:

            print(
                "Group approval message error:",
                repr(e),
                flush=True,
            )

    elif action == "reject_group":

        remove_approved_group(target_id)

        await query.edit_message_text(
            (
                "❌ <b>Group rejected.</b>\n\n"
                f"🆔 ID: <code>{target_id}</code>"
            ),
            parse_mode="HTML",
        )

        try:

            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "❌ <b>FixMyEnglish access was "
                    "not approved for this group.</b>"
                ),
                parse_mode="HTML",
            )

        except Exception as e:

            print(
                "Group rejection message error:",
                repr(e),
                flush=True,
            )


# =========================================================
# OWNER COMMANDS
# =========================================================

def extract_user_id(message):

    target = get_target_text(message)

    if not target:
        return None

    match = re.search(
        r"-?\d+",
        target,
    )

    if not match:
        return None

    try:
        return int(match.group())

    except ValueError:
        return None


async def add_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    user_id = extract_user_id(
        update.effective_message
    )

    if user_id is None:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/add USER_ID\n\n"
            "Or reply to the user's message with /add"
        )

        return

    add_approved(user_id)
    remove_pending(user_id)

    await update.effective_message.reply_text(
        f"✅ User {user_id} approved."
    )


async def del_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    user_id = extract_user_id(
        update.effective_message
    )

    if user_id is None:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/del USER_ID"
        )

        return

    remove_approved(user_id)

    await update.effective_message.reply_text(
        f"🗑️ User {user_id} removed."
    )


async def list_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    approved = (
        "\n".join(
            f"• <code>{uid}</code>"
            for uid in approved_users
        )
        if approved_users
        else "None"
    )

    pending = (
        "\n".join(
            f"• <code>{uid}</code>"
            for uid in pending_users
        )
        if pending_users
        else "None"
    )

    await update.effective_message.reply_text(
        "👑 <b>Users</b>\n\n"
        f"✅ <b>Approved:</b>\n{approved}\n\n"
        f"⏳ <b>Pending:</b>\n{pending}",
        parse_mode="HTML",
    )


async def approve_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    user_id = extract_user_id(
        update.effective_message
    )

    if user_id is None:

        await update.effective_message.reply_text(
            "Usage: /approve USER_ID"
        )

        return

    add_approved(user_id)
    remove_pending(user_id)

    await update.effective_message.reply_text(
        f"✅ User {user_id} approved."
    )

    try:

        await context.bot.send_message(
            chat_id=user_id,
            text="✅ Your access has been approved!",
        )

    except Exception as e:

        print(
            "Approval message error:",
            repr(e),
            flush=True,
        )


async def reject_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    user_id = extract_user_id(
        update.effective_message
    )

    if user_id is None:

        await update.effective_message.reply_text(
            "Usage: /reject USER_ID"
        )

        return

    remove_pending(user_id)

    await update.effective_message.reply_text(
        f"❌ User {user_id} rejected."
    )


async def stats_command(update, context):

    if not is_owner(
        update.effective_user.id
    ):
        return

    await update.effective_message.reply_text(
        "📊 <b>Statistics</b>\n\n"
        f"👥 Approved users: {len(approved_users)}\n"
        f"⏳ Pending users: {len(pending_users)}",
        parse_mode="HTML",
    )


# =========================================================
# SLASH COMMANDS
# =========================================================

async def tr_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /tr text\n\n"
            "Or reply to a message with /tr"
        )

        return

    await send_long_reply(
        update,
        await translate_text(text),
    )


async def cor_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /cor text"
        )

        return

    await send_long_reply(
        update,
        await correct_text(text),
    )


async def ex_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /ex word"
        )

        return

    save_vocab(
        update.effective_user.id,
        text,
    )

    await send_long_reply(
        update,
        await explain_text(text),
    )


async def root_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /root word\n\n"
            "Or reply to a message with /root"
        )

        return

    await send_long_reply(
        update,
        await root_word(text),
    )


async def fw_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /fw word\n\n"
            "Or reply to a message with /fw"
        )

        return

    await send_long_reply(
        update,
        await word_family(text),
    )


async def syn_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /syn word"
        )

        return

    await send_long_reply(
        update,
        await synonyms_text(text),
    )


async def ant_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /ant word"
        )

        return

    await send_long_reply(
        update,
        await antonyms_text(text),
    )


async def levels_command(update, context):
    chat = update.effective_chat  

    if (  
        chat.type == "private"  
        and not is_approved(update.effective_user.id)  
    ):  
        return  

    text = get_target_text(update.effective_message)  

    if not text:  
        await update.effective_message.reply_text(  
            "Usage: /levels word\n\n"  
            "Or reply to a message with /levels"  
        )  
        return  

    await send_long_reply(  
        update,  
        await syn_levels_text(text),  
    )


async def ipaus_command(update, context):
    chat = update.effective_chat  

    if chat.type == "private" and not is_approved(update.effective_user.id):  
        return  

    text = get_target_text(update.effective_message)  

    if not text:  
        await update.effective_message.reply_text(  
            "Usage: /ipaus text\n\n"  
            "Or reply to a message with /ipaus"  
        )  
        return  

    await send_long_reply(update, await get_ipa_transcription(text, "US"))


async def ipauk_command(update, context):
    chat = update.effective_chat  

    if chat.type == "private" and not is_approved(update.effective_user.id):  
        return  

    text = get_target_text(update.effective_message)  

    if not text:  
        await update.effective_message.reply_text(  
            "Usage: /ipauk text\n\n"  
            "Or reply to a message with /ipauk"  
        )  
        return  

    await send_long_reply(update, await get_ipa_transcription(text, "UK"))


async def use_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /use word"
        )

        return

    await send_long_reply(
        update,
        await use_word(text),
    )


async def ai_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage:\n/ai your request"
        )

        return

    await send_long_reply(
        update,
        await free_ai(text),
    )


async def us_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text, slow = get_pronunciation_target(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/us word\n"
            "/us slowly word"
        )

        return

    await send_pronunciation(
        update,
        text,
        "US",
        slow=slow,
    )


async def uk_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text, slow = get_pronunciation_target(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/uk word\n"
            "/uk slowly word"
        )

        return

    await send_pronunciation(
        update,
        text,
        "UK",
        slow=slow,
    )


async def pr_command(update, context):

    chat = update.effective_chat

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /pr word"
        )

        return

    await send_pronunciation(
        update,
        text,
        "BOTH",
    )


# =========================================================
# ARABIC COMMANDS
# =========================================================

ARABIC_COMMANDS = {
    "ترجم": "tr",
    "صحح": "cor",
    "اشرح": "ex",
    "جذر": "root",
    "عائلة": "fw",
    "مرادف": "syn",
    "ضد": "ant",
    "مستويات": "levels",
    "وظف": "use",
    "فوناتيك_امريكي": "ipaus",
    "فوناتيك_بريطاني": "ipauk",
    "امريكي": "us",
    "بريطاني": "uk",
    "انطق": "pr",
    "تكلم": "talk",
}


async def arabic_command_handler(update, context):

    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text:
        return

    if (
        chat.type == "private"
        and not is_approved(
            update.effective_user.id
        )
    ):
        return

    text = message.text.strip()

    parts = text.split(
        maxsplit=1
    )

    command = parts[0].lower()

    if command not in ARABIC_COMMANDS:
        return

    action = ARABIC_COMMANDS[command]

    argument = (
        parts[1].strip()
        if len(parts) == 2
        else ""
    )

    if action == "talk":

        await talk_command(
            update,
            context,
        )

        return

    slow = False

    if action in {
        "us",
        "uk",
        "pr",
    }:

        if argument:

            arg_parts = argument.split(
                maxsplit=1
            )

            if arg_parts[0].strip() == "بطيء":

                slow = True

                if len(arg_parts) == 2:
                    argument = arg_parts[1].strip()
                else:
                    argument = get_reply_text(message)

    if not argument:
        argument = get_reply_text(message)

    if not argument:
        return

    if action == "tr":

        await send_long_reply(
            update,
            await translate_text(argument),
        )

    elif action == "cor":

        await send_long_reply(
            update,
            await correct_text(argument),
        )

    elif action == "ex":

        save_vocab(
            update.effective_user.id,
            argument,
        )

        await send_long_reply(
            update,
            await explain_text(argument),
        )

    elif action == "root":

        await send_long_reply(
            update,
            await root_word(argument),
        )

    elif action == "fw":

        await send_long_reply(
            update,
            await word_family(argument),
        )

    elif action == "syn":

        await send_long_reply(
            update,
            await synonyms_text(argument),
        )

    elif action == "ant":

        await send_long_reply(
            update,
            await antonyms_text(argument),
        )

    elif action == "levels":
        
        await send_long_reply(
            update,
            await syn_levels_text(argument),
        )

    elif action == "use":

        await send_long_reply(
            update,
            await use_word(argument),
        )

    elif action == "ipaus":
        
        await send_long_reply(
            update, 
            await get_ipa_transcription(argument, "US")
        )
        
    elif action == "ipauk":
        
        await send_long_reply(
            update, 
            await get_ipa_transcription(argument, "UK")
        )

    elif action == "us":

        await send_pronunciation(
            update,
            argument,
            "US",
            slow=slow,
        )

    elif action == "uk":

        await send_pronunciation(
            update,
            argument,
            "UK",
            slow=slow,
        )

    elif action == "pr":

        await send_pronunciation(
            update,
            argument,
            "BOTH",
            slow=slow,
        )


# =========================================================
# NORMAL MESSAGE HANDLER
# =========================================================

async def normal_message_handler(update, context):

    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text:
        return

    user = update.effective_user

    if not user:
        return

    is_group = chat.type in [
        "group",
        "supergroup",
    ]

    # -----------------------------------------------------
    # في الخاص فقط نتحقق من اعتماد المستخدم
    # -----------------------------------------------------

    if (
        not is_group
        and not is_approved(user.id)
    ):
        return

    # -----------------------------------------------------
    # في المجموعات:
    # لا يعمل البوت إلا إذا كانت المجموعة معتمدة.
    # -----------------------------------------------------

    if (
        is_group
        and not is_group_approved(chat.id)
    ):
        return

    text = message.text.strip()

    # -----------------------------------------------------
    # 1. الاسم / Tag
    # -----------------------------------------------------

    name_triggered = await name_reaction(
        update,
        context,
    )

    # -----------------------------------------------------
    # 2. Arabic commands
    # -----------------------------------------------------

    first_word = text.split(
        maxsplit=1
    )[0].lower()

    if first_word in ARABIC_COMMANDS:

        await arabic_command_handler(
            update,
            context,
        )

        return

    # -----------------------------------------------------
    # 3. هل الرسالة Reply على البوت؟
    # -----------------------------------------------------

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id
        == context.bot.id
    )

    # -----------------------------------------------------
    # 4. Talk mode
    # -----------------------------------------------------

    if user.id in talk_mode_users:

        if is_group and not is_reply_to_bot:
            return

        reply = await talk_with_ai(
            text,
            user.first_name,
        )

        await send_long_reply(
            update,
            reply,
        )

        return

    # -----------------------------------------------------
    # 5. Automatic correction in groups
    # -----------------------------------------------------

    if is_group:

        if not is_autocorrect_enabled(chat.id):
            return

        words = text.split()

        is_english = (
            bool(
                re.search(
                    r"[A-Za-z]",
                    text,
                )
            )
            and not re.search(
                r"[\u0600-\u06FF]",
                text,
            )
        )

        if is_english and 1 <= len(words) <= 20:

            correction = await auto_correct_chat(
                text
            )

            correction_clean = (
                correction.strip()
                if correction
                else ""
            )

            if (
                correction_clean
                and correction_clean.lower()
                not in {
                    "ok",
                    "ok.",
                    "okay",
                    "okay.",
                }
            ):

                await send_long_reply(
                    update,
                    f"💡 Correction:\n{correction_clean}",
                )

        return

    # -----------------------------------------------------
    # 6. في الخاص:
    # الرسائل العادية لا يتم تصحيحها تلقائيًا.
    # -----------------------------------------------------


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    print(
        "Telegram error:",
        repr(context.error),
        flush=True,
    )


# =========================================================
# FLASK
# =========================================================

@app.route("/")
def home():
    return "FixMyEnglish Pro is alive!"


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": "FixMyEnglish Pro",
    }


def run_flask():

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

async def set_command_menu(application):

    try:

        user_commands = [
            ("start", "Start FixMyEnglish"),
            ("help", "Show commands"),
            ("talk", "Talk mode"),
            ("vocab", "Saved words"),
            ("tr", "Translate"),
            ("cor", "Correct English"),
            ("ex", "Explain"),
            ("root", "Word root"),
            ("fw", "Word family"),
            ("syn", "Synonyms"),
            ("ant", "Antonyms"),
            ("levels", "Synonyms by CEFR levels"),
            ("use", "Use a word"),
            ("ipaus", "US IPA (Text)"),
            ("ipauk", "UK IPA (Text)"),
            ("on", "Turn auto correction on"),
            ("off", "Turn auto correction off"),
            ("us", "American pronunciation"),
            ("uk", "British pronunciation"),
            ("pr", "Both pronunciations"),
            ("ai", "Ask AI"),
        ]

        await application.bot.set_my_commands(
            user_commands,
            read_timeout=10,
            write_timeout=10,
            connect_timeout=10,
            pool_timeout=10,
        )

        if OWNER_ID != 0:

            owner_commands = user_commands + [
                ("add", "Add user"),
                ("del", "Remove user"),
                ("list", "List users"),
                ("approve", "Approve user"),
                ("reject", "Reject user"),
                ("stats", "Show statistics"),
            ]

            await application.bot.set_my_commands(
                owner_commands,
                scope=BotCommandScopeChat(
                    chat_id=OWNER_ID
                ),
                read_timeout=10,
                write_timeout=10,
                connect_timeout=10,
                pool_timeout=10,
            )

        print(
            "Telegram command menu set successfully.",
            flush=True,
        )

    except Exception as e:

        print(
            "Command menu error:",
            repr(e),
            flush=True,
        )


async def post_init(application):

    application.create_task(
        set_command_menu(application)
    )


# =========================================================
# BUILD BOT
# =========================================================

def build_application():

    application = (
        Application.builder()
        .token(BOT_TOKEN)

        .concurrent_updates(4)

        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)

        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "talk",
            talk_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "vocab",
            vocab_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "tr",
            tr_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cor",
            cor_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ex",
            ex_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "root",
            root_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "fw",
            fw_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "syn",
            syn_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ant",
            ant_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "levels",
            levels_command,
        )
    )
    
    application.add_handler(
        CommandHandler(
            "ipaus",
            ipaus_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ipauk",
            ipauk_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "use",
            use_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "on",
            on_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "off",
            off_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "ai",
            ai_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "us",
            us_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "uk",
            uk_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "pr",
            pr_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "add",
            add_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "del",
            del_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "list",
            list_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "approve",
            approve_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "reject",
            reject_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            access_callback
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            normal_message_handler,
        )
    )

    # =====================================================
    # GROUP ACCESS REQUEST
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.ALL,
            group_access_request,
        ),
        group=1,
    )

    application.add_error_handler(
        error_handler
    )

    return application


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "=== FixMyEnglish Pro START ===",
        flush=True,
    )

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is missing."
        )

    print(
        "BOT_TOKEN found:",
        bool(BOT_TOKEN),
        flush=True,
    )

    print(
        "GROQ_API_KEY found:",
        bool(GROQ_API_KEY),
        flush=True,
    )

    # -----------------------------------------------------
    # Flask يبدأ مرة واحدة فقط
    # -----------------------------------------------------

    print(
        "Starting Flask...",
        flush=True,
    )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    print(
        "Flask thread started.",
        flush=True,
    )

    # -----------------------------------------------------
    # Telegram polling
    # -----------------------------------------------------

    first_run = True

    while True:

        application = None

        try:

            print(
                "Building Telegram application...",
                flush=True,
            )

            application = build_application()

            print(
                "Telegram application built successfully.",
                flush=True,
            )

            print(
                "Starting Telegram polling...",
                flush=True,
            )

            application.run_polling(
                drop_pending_updates=first_run,
                allowed_updates=Update.ALL_TYPES,
            )

            first_run = False

            print(
                "Polling stopped.",
                flush=True,
            )

            print(
                "Restarting polling in 5 seconds...",
                flush=True,
            )

            time.sleep(5)

        except KeyboardInterrupt:

            print(
                "Bot stopped by KeyboardInterrupt.",
                flush=True,
            )

            break

        except Exception as e:

            print(
                "POLLING ERROR:",
                repr(e),
                flush=True,
            )

            print(
                "Telegram polling will restart in 10 seconds...",
                flush=True,
            )

            time.sleep(10)

            first_run = False


if __name__ == "__main__":
    main()
