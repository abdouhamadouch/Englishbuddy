# grammar.py
# FixMyEnglish - Grammar Analyzer
#
# Commands:
# /grammar
# /gram
# قواعد
# جرامر
#
# Works with:
# 1. A word
# 2. A phrase
# 3. A full sentence
# 4. Replying to a message
#
# The module analyzes:
# - Grammar rules
# - Tenses
# - Passive / Active voice
# - Sentence structures
# - Conditionals
# - Gerunds / infinitives
# - Modals
# - Articles
# - Prepositions
# - Other grammar actually present in the text
# - Common mistakes
#
# IMPORTANT:
# This file does NOT create a new AI client.
# It receives the existing AI function from bot.py.


from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# SETTINGS
# ============================================================

MAX_INPUT_LENGTH = 3000


# ============================================================
# AI FUNCTION
# ============================================================

_ai_function = None


def set_ai_function(ai_function):
    """
    Connect grammar.py to the existing AI function in bot.py.

    Example in bot.py:

        from grammar import set_ai_function, register_grammar_handlers
        set_ai_function(free_ai)
        register_grammar_handlers(application)
    """
    global _ai_function
    _ai_function = ai_function


# ============================================================
# GET TEXT
# ============================================================

def _get_reply_text(update: Update) -> str | None:
    """
    Get text from the message being replied to.
    """

    message = update.effective_message

    if not message or not message.reply_to_message:
        return None

    replied = message.reply_to_message

    if replied.text:
        return replied.text.strip()

    if replied.caption:
        return replied.caption.strip()

    return None


def _clean_input(text: str) -> str:
    text = text.strip()

    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    return text


# ============================================================
# GRAMMAR PROMPT
# ============================================================

def build_grammar_prompt(text: str) -> str:
    """
    Build the AI prompt for grammar analysis.
    """

    return f"""
You are the Grammar Analyzer of FixMyEnglish.

Analyze the following English input:

{text}

Your job is to teach grammar clearly and accurately.

IMPORTANT RULES:

1. First determine what the user gave you:
   - a single word
   - a short phrase
   - or a complete sentence / group of sentences.

2. Analyze ONLY grammar that is actually relevant to the input.
   Do not invent grammar points that are not present.

3. If it is a word:
   - identify its grammatical form if possible
   - explain its tense/form if relevant
   - identify base form and other forms when useful
   - mention possible grammatical interpretations if the word is ambiguous.

4. If it is a phrase:
   - explain its grammatical structure
   - explain the important grammar pattern
   - explain how the pattern is normally used.

5. If it is a sentence:
   Identify ALL important grammar found in it.

Possible areas include:
- Present Simple
- Present Continuous
- Present Perfect
- Present Perfect Continuous
- Past Simple
- Past Continuous
- Past Perfect
- Past Perfect Continuous
- Future forms
- Passive Voice
- Active Voice
- Modal verbs
- Conditionals
- Comparatives / superlatives
- Articles
- Prepositions
- Gerunds
- Infinitives
- To + verb
- Verb patterns
- Relative clauses
- Question structures
- Negatives
- Reported speech
- Subject-verb agreement
- Countable / uncountable nouns
- Determiners
- Conjunctions
- Adverbs
- Adjectives
- Other relevant grammar.

6. For EVERY important grammar rule you identify:
   - give the rule name
   - show the relevant structure/form
   - explain it briefly in simple Arabic
   - explain why it is used here
   - give AT LEAST 3 new English examples
   - give an Arabic translation for every example.

7. COMMON MISTAKES:
   Include a "Common Mistakes" section ONLY when there is a genuinely
   common mistake related to this word, phrase, structure, or sentence.

   If there is a mistake in the user's sentence:
   - show the incorrect form
   - show the correct form
   - explain why
   - give several correct examples.

   Do NOT invent mistakes merely to fill the section.

8. If the sentence is grammatically correct:
   explicitly say that there are no important grammar mistakes.

9. If a word or sentence has more than one legitimate grammatical
   interpretation, mention the alternatives instead of pretending
   there is only one.

10. Do not turn the response into a long grammar textbook.
    Keep explanations concise but useful.

11. Do not use decorative Markdown stars.
    Use clean headings and separators.

12. Important grammar terms should be bold.

13. Use this general visual structure:

╭━━━━━━━━━━━━━━━━━━━━╮
        📘 GRAMMAR
╰━━━━━━━━━━━━━━━━━━━━╯

📝 INPUT

━━━━━━━━━━━━━━━━━━━━

🔎 GRAMMAR FOUND

1️⃣ ...

💡 Rule:
...

🧩 Structure:
...

📌 Why:
...

✍️ Examples:

• ...
  ...

• ...
  ...

• ...
  ...

━━━━━━━━━━━━━━━━━━━━

⚠️ COMMON MISTAKES

❌ ...
✅ ...

💡 Why:
...

✍️ Examples:

• ...
  ...

━━━━━━━━━━━━━━━━━━━━

📌 SUMMARY

...

Use Arabic for explanations and English for English examples.
Do not translate grammar terms unnecessarily.
"""


# ============================================================
# CALL AI
# ============================================================

async def _ask_ai(prompt: str) -> str:
    """
    Call the AI function supplied by bot.py.

    The existing free_ai function is expected to be async and
    return a string.
    """

    if _ai_function is None:
        return (
            "⚠️ Grammar AI is not connected yet.\n\n"
            "Connect grammar.py to the existing free_ai function "
            "in bot.py."
        )

    try:
        result = await _ai_function(prompt)

        if result:
            return str(result).strip()

    except Exception:
        pass

    return (
        "⚠️ I couldn't analyze the grammar right now.\n"
        "Please try again."
    )


# ============================================================
# GRAMMAR ANALYSIS
# ============================================================

async def analyze_grammar(text: str) -> str:
    """
    Main grammar analysis function.
    """

    text = _clean_input(text)

    if not text:
        return (
            "📘 **Grammar**\n\n"
            "Send me an English word, phrase, or sentence "
            "to analyze its grammar."
        )

    prompt = build_grammar_prompt(text)

    return await _ask_ai(prompt)


# ============================================================
# /grammar COMMAND
# ============================================================

async def grammar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Handles:

    /grammar
    /grammar sentence here
    """

    message = update.effective_message

    if not message:
        return

    text = " ".join(context.args).strip()

    # If there is no text after the command,
    # try the replied message.
    if not text:
        text = _get_reply_text(update) or ""

    if not text:
        await message.reply_text(
            "📘 **Grammar**\n\n"
            "Send me a word, phrase, or sentence after the command.\n\n"
            "Example:\n"
            "`/grammar I have been studying English for two years.`\n\n"
            "You can also reply to an English sentence with `/grammar`.",
            parse_mode="Markdown",
        )
        return

    result = await analyze_grammar(text)

    await message.reply_text(
        result,
        parse_mode="HTML",
    )


# ============================================================
# ARABIC COMMANDS
# ============================================================

async def grammar_arabic_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Handles:

    قواعد
    جرامر

    It can also analyze a replied message.
    """

    message = update.effective_message

    if not message:
        return

    text = " ".join(context.args).strip()

    if not text:
        text = _get_reply_text(update) or ""

    if not text:
        await message.reply_text(
            "📘 Grammar\n\n"
            "أرسل كلمة أو عبارة أو جملة إنجليزية لتحليل قواعدها.\n\n"
            "ويمكنك أيضًا الرد على جملة وكتابة:\n"
            "قواعد"
        )
        return

    result = await analyze_grammar(text)

    await message.reply_text(
        result,
        parse_mode="HTML",
    )


# ============================================================
# REPLY SUPPORT
# ============================================================

async def grammar_reply_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Allows:

    Reply to an English message with:
    قواعد
    جرامر
    """

    message = update.effective_message

    if not message:
        return

    replied_text = _get_reply_text(update)

    if not replied_text:
        await message.reply_text(
            "📘 لم أجد نصًا إنجليزيًا في الرسالة التي رددت عليها."
        )
        return

    result = await analyze_grammar(replied_text)

    await message.reply_text(
        result,
        parse_mode="HTML",
    )


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register_grammar_handlers(application: Application):
    """
    Register all Grammar handlers.

    Call this once from bot.py.
    """

    # English commands
    application.add_handler(
        CommandHandler(
            ["grammar", "gram"],
            grammar_command,
        )
    )

    # Arabic commands
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(r"^(قواعد|جرامر)$"),
            grammar_reply_command,
        )
  )
