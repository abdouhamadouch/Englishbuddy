[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: import os
import json
import re
import random
import tempfile
import threading
import asyncio
import time
from pathlib import Path

from flask import Flask
from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
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

app = Flask(name)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

json_lock = threading.Lock()
talk_mode_users = set()

# نحفظ username المالك إذا ظهر لنا في رسالة
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


def save_vocab(user_id, word):
    uid_str = str(user_id)

    if uid_str not in vocab_bank:
        vocab_bank[uid_str] = []

    if word not in vocab_bank[uid_str]:
        vocab_bank[uid_str].append(word)
        save_json(VOCAB_FILE, vocab_bank)


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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: if len(parts) == 2:
        return parts[1].strip()

    return ""


def get_target_text(message):
    reply = get_reply_text(message)

    if reply:
        return reply

    return get_arguments(message)


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


async def send_long_reply(update, text):
    message = update.effective_message

    if not message:
        return

    for part in split_long_text(text):
        await message.reply_text(part)


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
            "Never reveal internal reasoning."
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

Text:
{text}
""",
        1200,
    )


# =========================================================
# AI FUNCTIONS — NATURAL & ORGANIZED ANSWERS
# =========================================================

async def correct_text(text):
    return await ask_groq(
        f"""
Correct this English text for an Arabic-speaking learner.
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: Focus mainly on:
- spelling mistakes
- typing mistakes
- incorrect or missing words
- important grammar mistakes that affect correctness or meaning

Do not rewrite correct English just to make it sound more advanced.

Use this clean structure:

✍️ Correction
━━━━━━━━━━━━

✅ Correct:
[corrected text]

📝 Changes:
• [mistake → correction] — [very short explanation]
• [mistake → correction] — [very short explanation]

🇩🇿 Meaning:
[natural Arabic meaning]

If there are no important mistakes, return only:

✅ No important mistakes found.

Rules:
- Do not invent mistakes.
- Do not criticize the learner.
- Do not give a long grammar lesson.
- Mention only useful corrections.
- If a word is obviously mistyped, infer the intended word when the context is clear.
- Keep the answer concise and well organized.
- Do not ask a follow-up question.

Text:
{text}
""",
        1200,
    )


async def explain_text(text):
    return await ask_groq(
        f"""
Explain this English word or expression to an Arabic-speaking learner.

The answer should be natural, clear, concise, and useful for learning.

Use the following structure when relevant:

📖 Explanation
━━━━━━━━━━━━

🔤 Word:
[word]

🇩🇿 Meaning:
[main Arabic meaning]

🏷️ Part of speech:
[verb / noun / adjective / adverb / expression, etc.]

📚 Meanings & Use:

1️⃣ [first common meaning]
[short Arabic explanation]

2️⃣ [second common meaning, only if genuinely common]
[short Arabic explanation]

If different parts of speech have important meanings, separate them clearly:

🔹 Verb:
[Arabic meaning + short explanation]

🔹 Noun:
[Arabic meaning + short explanation]

🔹 Adjective:
[Arabic meaning + short explanation]

🔗 Common patterns:
[common preposition, collocation, fixed expression, or sentence pattern]

📝 Examples:

1️⃣ [natural English example]
🇩🇿 [Arabic translation]

2️⃣ [natural English example]
🇩🇿 [Arabic translation]

Rules:
- Put the most common meaning first.
- Include other common meanings when they are genuinely useful.
- If the word is commonly used as both a verb and noun, explain both separately.
- Also separate adjective or adverb uses when they are common and useful.
- Include common prepositions such as "depend on", "interested in", or "aware of" when they genuinely belong with the word.
- Include useful common collocations and fixed expressions when relevant.
- Do not invent meanings, prepositions, collocations, or examples.
- Do not include rare dictionary meanings unless they are important for understanding common usage.
- Keep explanations short and easy to understand.
- Use Arabic for meanings and explanations.
- Use natural English examples.
- Make the Arabic translation natural, not word-for-word.
- Do not repeat the same information in multiple sections.
- Do not force a section if it is not relevant.
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

Organize the answer clearly:

🔤 Word:
{text}

🇩🇿 Main meaning:
[main Arabic meaning]

🔄 Synonyms:
• [synonym] — [Arabic meaning or short nuance]
• [synonym] — [Arabic meaning or short nuance]
• [synonym] — [Arabic meaning or short nuance]
• [synonym] — [Arabic meaning or short nuance]
• [synonym] — [Arabic meaning or short nuance]

↔️ Antonyms:
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]

📝 Example:
[natural English sentence]

🇩🇿 Translation:
[natural Arabic translation]

Rules:
- Give up to 5 useful synonyms.
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: - Prefer common, natural synonyms.
- Explain an important difference in meaning or usage briefly when needed.
- Give genuine antonyms only.
- If there are fewer natural antonyms, give fewer.
- Never invent words just to fill the list.
- Avoid repetition.
- If the word has different meanings, choose synonyms according to the specific meaning being explained.
- Keep the answer concise.
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

Organize the answer clearly:

🔤 Word:
{text}

🇩🇿 Meaning:
[main Arabic meaning]

↔️ Antonyms:
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]
• [antonym] — [Arabic meaning]

📝 Example:
[natural English sentence]

🇩🇿 Translation:
[natural Arabic translation]

Rules:
- Give genuine antonyms only.
- Prefer common and useful words.
- If only one or two natural antonyms exist, give only those.
- Do not invent an opposite.
- Avoid repetition.
- Consider the specific meaning of the word.
- Keep the answer concise.
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

Organize the answer clearly:

🧩 Word Usage
━━━━━━━━━━━━

🔤 Word:
{text}

🇩🇿 Meaning:
[main Arabic meaning]

📚 How to use it:
[short, practical explanation]

🔗 Common patterns:
[common preposition, collocation, sentence pattern, or fixed expression]

If there are different important meanings or different parts of speech, separate them briefly.

📝 Examples:
1️⃣ [natural English sentence]
🇩🇿 [Arabic translation]

2️⃣ [natural English sentence]
🇩🇿 [Arabic translation]

Rules:
- Focus on real, common usage.
- Mention important prepositions and patterns.
- Mention different common meanings when they affect usage.
- If noun and verb usage differ, distinguish them.
- Do not invent expressions.
- Do not over-explain grammar.
- Do not force sections that are not useful.
- Keep the answer concise and practical.
- Do not ask a follow-up question.
- Do not add a long introduction.

Word:
{text}
""",
        1100,
    )


async def free_ai(text):
    return await ask_groq(
        f"""
Answer the user's request directly and naturally.

The user may ask about English, Arabic, vocabulary, grammar,
translation, pronunciation, or another topic.

Requirements:
- Understand exactly what the user is asking.
- Give the useful answer first.
- Use Arabic when it helps an Arabic-speaking learner.
- For English-learning questions, organize the explanation clearly.
- Use headings and bullets only when they improve readability.
- If there are several meanings, cases, or uses, separate them clearly.
- Give examples only when they genuinely help.
- Do not repeat the user's question.
- Do not give a long introduction.
- Do not add unrelated information.
- Do not ask "Would you like me to...?"
- Do not end with an unnecessary question.
- Do not offer random topics or unrelated suggestions.
- If the user's message is short or casual, answer naturally and briefly.
- Do not force a lesson when the user is simply chatting.
- Keep the response proportional to the request.
- Never sound like a customer-service script.

User request:
{text}
""",
        1000,
    )


async def talk_with_ai(text, user_name):
    sys_prompt = (
        f"You are FixMyEnglish, a natural and friendly English conversation "
        f"companion chatting with {user_name}. "

        "Your conversation should feel like a normal human conversation, "
        "not like a customer-service bot, tutor script, or interview. "
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: "Respond to what the user actually says. "
        "Do not use scripted greetings. "
        "Do not repeat greetings. "
        "Do not ask a question after every message. "
        "Do not ask questions merely to keep the conversation alive. "
        "Do not constantly suggest random topics. "
        "Do not say things like 'What's on your mind?', "
        "'What would you like to talk about?', or "
        "'Would you like to practice English?' unless the user actually "
        "asks for that. "

        "If the user gives a short casual reply such as "
        "'Nothing', 'Nothing much', 'I'm tired', 'Yeah', 'No', or 'Okay', "
        "respond naturally and briefly. "
        "Do not force another question or topic. "

        "If the user says something funny, respond naturally to the joke. "
        "If the user shares something, react to what they shared instead "
        "of immediately turning it into a question. "

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
        "Do not sound repetitive or robotic."
    )

    return await ask_groq(
        text,
        800,
        system_prompt=sys_prompt,
    )


async def auto_correct_chat(text):
    prompt = (
        "Check if this English message has an important grammar, spelling, "
        "or word-choice mistake. "
        "Focus on actual mistakes, not stylistic preferences. "
        "If there is a mistake, briefly show the correction and a short "
        "explanation. If it is correct, reply with 'OK'. "
        "Do not add unnecessary advice or questions.\n"
        f"Text: {text}"
    )

    return await ask_groq(
        prompt,
        300,
    )


async def pronunciation_info(word, dialect):

    if dialect == "US":
        name = "American English"
        flag = "🇺🇸"
    else:
        name = "British English"
        flag = "🇬🇧"

    return await ask_groq(
        f"""
Give accurate pronunciation information for this English word
in {name}.

Use EXACTLY this format:

🔊 Pronunciation
━━━━━━━━━━━━

🔤 Word:
{word}

{flag} IPA:
[IPA]

🇩🇿 Meaning:
[short Arabic meaning]

━━━━━━━━━━━━

Rules:
- Give standard IPA.
- Give pronunciation for this word only.
- Keep it concise.

Word:
{word}
""",
        700,
    )


async def both_pronunciation(word):
    return await ask_groq(
        f"""
Give accurate pronunciation information for this English word.

Use EXACTLY this format:

🔊 Pronunciation
━━━━━━━━━━━━

🔤 Word:
{word}

🇺🇸 American:
[IPA]

🇬🇧 British:
[IPA]

🇩🇿 Meaning:
[short Arabic meaning]

━━━━━━━━━━━━

Rules:
- Give standard IPA.
- Give pronunciation for this word only.
- Keep it concise.

Word:
{word}
""",
        700,
    )


# =========================================================
# TEXT TO SPEECH
# =========================================================

async def make_audio(text, voice):

    filename = None

    try:
        fd, filename = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: if os.path.getsize(filename) == 0:
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


async def send_pronunciation(update, word, dialect):

    message = update.effective_message

    if not message:
        return

    word = (word or "").strip()

    if not word:
        return

    word_count = len(word.split())

    if word_count > 700:
        await message.reply_text(
            "❌ The text is too long for pronunciation.\n"
            "The maximum is 700 words."
        )
        return

    if dialect == "US":
        voice = US_VOICE
    elif dialect == "UK":
        voice = UK_VOICE
    else:
        voice = US_VOICE

    if word_count <= 4:

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

        await message.reply_text(info)

    audio = await make_audio(
        word,
        voice,
    )

    if not audio:
        await message.reply_text(
            "❌ I couldn't create the pronunciation audio."
        )
        return

    try:
        with open(audio, "rb") as f:
            await message.reply_audio(
                audio=f,
                caption="🔊 Pronunciation",
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: "May Allah increase you in wisdom and guide you to the best choices.",
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: "May Allah make your learning journey enjoyable, beneficial, and full of barakah.",
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: "May Allah make your heart strong, your intentions sincere, and your journey blessed.",
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
    r"\bkarim\b",
    r"\babdelkarim\b",
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

    # -----------------------------------------------------
    # إذا كان الشخص يرد على رسالة المالك
    # -----------------------------------------------------

    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == OWNER_ID
    ):
        return True

    # -----------------------------------------------------
    # إذا كانت هذه الرسالة من المالك، نحفظ username الخاص به
    # -----------------------------------------------------

    sender = update.effective_user

    if (
        sender
        and sender.id == OWNER_ID
        and sender.username
    ):
        OWNER_USERNAME = sender.username.lower()

    # -----------------------------------------------------
    # Telegram text mention
    # -----------------------------------------------------

    entities = []

    if message.entities:
        entities.extend(message.entities)

    if message.caption_entities:
        entities.extend(message.caption_entities)

    text = message.text or message.caption or ""

    for entity in entities:

        if entity.type == "text_mention":
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: if (
                entity.user
                and entity.user.id == OWNER_ID
            ):
                return True

        elif entity.type == "mention":

            if not OWNER_USERNAME:
                continue

            mentioned_username = text[
                entity.offset:
                entity.offset + entity.length
            ].lower().lstrip("@")

            if mentioned_username == OWNER_USERNAME:
                return True

    return False


async def name_reaction(update, context):

    message = update.effective_message

    if not message:
        return

    text = message.text or message.caption or ""

    mentioned = name_is_mentioned(text)

    if not mentioned:
        mentioned = await name_is_tagged(
            update,
            context,
        )

    if not mentioned:
        return

    # -----------------------------------------------------
    # اسم الشخص الذي ذكر المالك
    # -----------------------------------------------------

    sender = update.effective_user

    if sender:
        sender_name = (
            sender.first_name
            or sender.full_name
            or "friend"
        )
    else:
        sender_name = "friend"

    # -----------------------------------------------------
    # ❤️ Reaction
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # دعاء باسم الشخص الذي ذكر المالك
    # -----------------------------------------------------

    dua = random.choice(DUAS).strip()

    await message.reply_text(
        f"🤲 {sender_name}, {dua}"
    )


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

🔄 <b>Synonyms</b>
/syn word
مرادف word

🔻 <b>Antonyms</b>
/ant word
ضد word

🧩 <b>Word Usage</b>
/use word
وظف word

🇺🇸 <b>American</b>
/us word
امريكي word

🇬🇧 <b>British</b>
/uk word
بريطاني word

🗣️ <b>Both</b>
/pr word
انطق word

🤖 <b>AI</b>
/ai your request

💬 <b>Talk Mode</b>
/talk أو تكلم — التحدث مع البوت كصديق

📚 <b>Vocabulary</b>
/vocab — عرض الكلمات المحفوظة

💬 <b>Reply mode</b>

Reply to a message and send:

/tr
/cor
/ex
/syn
/ant
/use
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: "👋 Welcome to <b>FixMyEnglish Pro</b>!\n\n"
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

    action, user_id_text = data.split(
        ":",
        1,
    )

    try:
        user_id = int(user_id_text)

    except ValueError:
        return

    if action == "approve":
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: add_approved(user_id)
        remove_pending(user_id)

        await query.edit_message_text(
            f"✅ User <code>{user_id}</code> approved.",
            parse_mode="HTML",
        )

        try:

            await context.bot.send_message(
                chat_id=user_id,
                text="✅ Your access to FixMyEnglish has been approved!",
            )

        except Exception as e:

            print(
                "Approval message error:",
                repr(e),
                flush=True,
            )

    elif action == "reject":

        remove_pending(user_id)

        await query.edit_message_text(
            f"❌ User <code>{user_id}</code> rejected.",
            parse_mode="HTML",
        )

        try:

            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Your access request was not approved.",
            )

        except Exception as e:

            print(
                "Reject message error:",
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: user_id = extract_user_id(
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: if (
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

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /us word"
        )

        return

    await send_pronunciation(
        update,
        text,
        "US",
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

    text = get_target_text(
        update.effective_message
    )

    if not text:

        await update.effective_message.reply_text(
            "Usage: /uk word"
        )

        return

    await send_pronunciation(
        update,
        text,
        "UK",
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
    "مرادف": "syn",
    "ضد": "ant",
    "وظف": "use",
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

    elif action == "use":

        await send_long_reply(
            update,
            await use_word(argument),
        )

    elif action == "us":

        await send_pronunciation(
            update,
            argument,
            "US",
        )
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: elif action == "uk":

        await send_pronunciation(
            update,
            argument,
            "UK",
        )

    elif action == "pr":

        await send_pronunciation(
            update,
            argument,
            "BOTH",
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

    # في الخاص فقط نتحقق من الاعتماد
    if (
        not is_group
        and not is_approved(user.id)
    ):
        return

    # -----------------------------------------------------
    # 1. الاسم / Tag / Reply
    # -----------------------------------------------------

    await name_reaction(
        update,
        context,
    )

    text = message.text.strip()

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
    # Talk mode
    # -----------------------------------------------------

    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id
        == context.bot.id
    )

    if user.id in talk_mode_users:

        if is_group and not is_reply_to_bot:
            return

        reply = await talk_with_ai(
            text,
            user.first_name,
        )

        await message.reply_text(reply)

        return

    # -----------------------------------------------------
    # Auto correction in groups
    # -----------------------------------------------------

    if is_group:

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

            if (
                correction
                and correction.strip().lower()
                not in {
                    "ok",
                    "ok.",
                }
            ):

                await message.reply_text(
                    f"💡 Correction hint:\n{correction}"
                )


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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: await application.bot.set_my_commands(
            [
                ("start", "Start FixMyEnglish"),
                ("help", "Show commands"),
                ("talk", "Talk mode"),
                ("vocab", "Saved words"),
                ("tr", "Translate"),
                ("cor", "Correct English"),
                ("ex", "Explain"),
                ("syn", "Synonyms"),
                ("ant", "Antonyms"),
                ("use", "Use a word"),
                ("us", "American pronunciation"),
                ("uk", "British pronunciation"),
                ("pr", "Both pronunciations"),
                ("ai", "Ask AI"),
            ],
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

        # Timeouts خاصة بطلبات getUpdates
        # حتى لا يموت polling بسبب مشاكل الشبكة القصيرة
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
            "use",
            use_command,
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
[9/24/2026 1:45 PM] أبو عبد الله عبدالكريم: application.add_handler(
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
    # إذا حدث Bad Gateway / NetworkError
    # يعيد بناء Application ويبدأ polling من جديد
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


if name == "main":
    main()
