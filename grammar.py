# grammar.py
# FixMyEnglish - Grammar Analyzer
#
# Features:
# - /grammar
# - /gram
# - قواعد
# - جرامر
# - Works with words, phrases, sentences, or grammar-rule names
# - Can analyze replied messages
# - Explains the actual grammar rule
# - Explains structure, usage, examples, comparisons and common mistakes
# - Common Mistakes appears only when genuinely relevant
# - Telegram HTML formatting
# - Automatic retry when AI fails or returns an empty result
# - Clean phone-friendly formatting
# - AI function is injected from bot.py


import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

MAX_INPUT_LENGTH = 3000
MAX_AI_ATTEMPTS = 4


# =========================================================
# AI FUNCTION
# =========================================================

_ai_function = None


def set_ai_function(ai_function):
    """
    Receive the AI function from bot.py.
    """
    global _ai_function
    _ai_function = ai_function


# =========================================================
# HELPERS
# =========================================================

def _get_reply_text(update: Update):
    """
    Get text/caption from the replied-to message.
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
    """
    Clean and limit user input.
    """
    if not text:
        return ""

    text = str(text).strip()

    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    return text


# =========================================================
# AI OUTPUT CLEANING
# =========================================================

def _clean_ai_output(text: str) -> str:
    """
    Clean AI output before sending it through Telegram HTML.

    The AI is instructed to use HTML, but it may occasionally
    return Markdown. This function removes Markdown emphasis
    and unsupported HTML tags while preserving useful HTML.
    """

    if not text:
        return ""

    text = str(text).strip()

    # Remove code fences.
    text = re.sub(r"```(?:html|text|markdown)?", "", text, flags=re.I)
    text = text.replace("```", "")

    # Remove Markdown bold/italic markers.
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Remove Markdown heading markers.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)

    # Convert common Markdown bullets into Telegram bullets.
    text = re.sub(r"(?m)^\s*[-+]\s+", "• ", text)

    # Remove unsupported HTML tags while keeping Telegram-safe tags.
    allowed_tags = (
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "code",
        "pre",
    )

    def clean_tag(match):
        tag = match.group(0)
        name_match = re.match(r"</?\s*([a-zA-Z0-9]+)", tag)

        if not name_match:
            return ""

        name = name_match.group(1).lower()

        if name in allowed_tags:
            return tag

        return ""

    text = re.sub(
        r"</?[^>]+>",
        clean_tag,
        text,
    )

    # Convert strong to b for consistent Telegram formatting.
    text = re.sub(r"<strong>", "<b>", text, flags=re.I)
    text = re.sub(r"</strong>", "</b>", text, flags=re.I)

    # Prevent repeated excessive blank lines.
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    return text.strip()


def _remove_all_html(text: str) -> str:
    """
    Plain-text fallback.
    """
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "")
    text = text.replace("*", "")

    return text.strip()


# =========================================================
# AI RETRY
# =========================================================

async def _ask_ai_with_retry(prompt: str):
    """
    Ask the injected AI function several times.

    A temporary empty/failed AI response should not immediately
    become a user-facing failure.
    """

    if _ai_function is None:
        print("[GRAMMAR] AI function is not configured.")
        return None

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):
        try:
            result = await _ai_function(prompt)

            if result:
                result = _clean_ai_output(str(result))

                if result:
                    print(
                        f"[GRAMMAR] AI succeeded on attempt "
                        f"{attempt}/{MAX_AI_ATTEMPTS}"
                    )
                    return result

            print(
                f"[GRAMMAR] Empty AI response "
                f"(attempt {attempt}/{MAX_AI_ATTEMPTS})"
            )

        except Exception as e:
            print(
                f"[GRAMMAR] AI error on attempt "
                f"{attempt}/{MAX_AI_ATTEMPTS}: {e}"
            )

    print("[GRAMMAR] All AI attempts failed.")
    return None


# =========================================================
# PROMPT
# =========================================================

def build_grammar_prompt(text: str) -> str:
    """
    Build a detailed grammar-teaching prompt.
    """

    return f"""
You are the Grammar Teacher inside FixMyEnglish.

The user wants to LEARN grammar, not merely receive a short correction.

Analyze this user input:

USER INPUT:
{text}

━━━━━━━━━━━━━━━━━━

CORE INSTRUCTIONS

• Identify the genuinely relevant grammar.
• Teach the grammar clearly and practically.
• Do not invent grammar problems.
• If the sentence is correct, explain why the grammar is correct.
• If the sentence is incorrect, explain the real grammatical problem.
• Explain WHY the structure is used, not only what it is.
• If the input is a grammar rule/name, teach that rule directly.
• If the input is a word, explain important grammatical patterns related to that word.
• If the input is a phrase, explain the grammar contained in the phrase.
• Focus mainly on A2-B1 learners.
• Explain advanced grammar only when the input requires it.
• Avoid unnecessary grammar theory.

LANGUAGE

• Explain mainly in clear Arabic.
• Keep useful English grammar terminology in English.
• English examples must remain in English.
• Give an Arabic translation for every important example.

━━━━━━━━━━━━━━━━━━

FORMATTING RULES

You MUST use Telegram HTML.

Use:
<b>important text</b>

Never use Markdown bold.

NEVER use:
**
*
__
_

Do not use Markdown headings.

Do not use decorative stars.

Use:
• for bullet points.

Use:
━━━━━━━━━━━━━━━━━━

for section separators.

You may use these decorative title frames when appropriate:

╔════════════════════╗
║  🧠 Grammar Point  ║
╚════════════════════╝

╭────────────────────╮
│  📚 What is it?    │
╰────────────────────╯

╔════════════════════╗
║  🧩 Structure      ║
╚════════════════════╝

╭────────────────────╮
│  🎯 When to use it │
╰────────────────────╯

╔════════════════════╗
║  💬 Examples       ║
╚════════════════════╝

╭────────────────────╮
│  ⚖️ Compare        │
╰────────────────────╯

╔════════════════════╗
║  ⚠️ Common Mistakes║
╚════════════════════╝

╭────────────────────╮
│  🔎 In this sentence│
╰────────────────────╯

Do not use every frame automatically.

Choose the frame that fits the section.

━━━━━━━━━━━━━━━━━━

POSSIBLE SECTIONS

Use ONLY the sections genuinely useful for this input.

1. Grammar Point
Give the main grammar rule.

2. What is it?
Explain it simply.

3. Structure
Show the grammatical structure.

Examples:
Subject + have/has + past participle

Subject + be + past participle

If + past simple, would + base verb

4. When do we use it?
Explain the important uses.

5. Examples
Give at least 3 useful examples when teaching a grammar rule.

Each example:
<b>English sentence.</b>
Arabic translation.

6. Compare
Use only when learners commonly confuse the target grammar
with another important structure.

7. Common Mistakes
Use ONLY when genuinely relevant.

For every mistake:
• Incorrect form
• Correct form
• Why it is wrong
• Additional example

8. In this sentence
When the user gives a sentence, explain exactly how the
grammar works inside that sentence.

━━━━━━━━━━━━━━━━━━

IMPORTANT STYLE RULES

• Keep the answer organized.
• Keep paragraphs short.
• Highlight important grammar terms with <b>...</b>.
• Highlight important words in examples with <b>...</b> when useful.
• Do not make the entire answer bold.
• Do not create empty sections.
• Do not use tables unless a very small comparison genuinely
  requires one.
• Do not repeat the same explanation.
• Do not end every answer with an unnecessary question.
• Do not say only "correct" or "incorrect".
• Make the response feel like a short useful grammar lesson.

The final answer must contain NO Markdown stars.
"""


# =========================================================
# ANALYZE
# =========================================================

async def analyze_grammar(text: str):
    """
    Analyze grammar using AI with retry support.
    """

    text = _clean_input(text)

    if not text:
        return None

    prompt = build_grammar_prompt(text)

    return await _ask_ai_with_retry(prompt)


# =========================================================
# SAFE TELEGRAM SENDING
# =========================================================

async def _reply_result(message, result: str):
    """
    Send the grammar result safely.

    First attempt:
        Telegram HTML

    Fallback:
        Plain text without HTML.
    """

    result = _clean_ai_output(result or "")

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation. "
            "Please try again."
        )
        return

    try:
        await message.reply_text(
            result,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        return

    except Exception as e:
        print(f"[GRAMMAR] HTML send error: {e}")

    # Safe plain-text fallback.
    plain = _remove_all_html(result)

    if not plain:
        plain = (
            "I couldn't display the grammar explanation. "
            "Please try again."
        )

    try:
        await message.reply_text(
            plain,
            disable_web_page_preview=True,
        )

    except Exception as e:
        print(f"[GRAMMAR] Plain-text send error: {e}")


# =========================================================
# TYPING
# =========================================================

async def _send_typing(message):
    """
    Safely show typing status.
    """
    try:
        await message.chat.send_action("typing")
    except Exception:
        pass


# =========================================================
# /grammar
# =========================================================

async def grammar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    /grammar
    /gram

    Examples:

    /grammar present perfect
    /grammar I have lived here for five years.

    Or:

    Reply to a message and use /grammar
    """

    message = update.effective_message

    if not message:
        return

    text = ""

    # -----------------------------------------------------
    # 1. Command arguments
    # -----------------------------------------------------

    if context.args:
        text = " ".join(context.args).strip()

    # -----------------------------------------------------
    # 2. Replied message
    # -----------------------------------------------------

    if not text:
        text = _get_reply_text(update) or ""

    text = _clean_input(text)

    if not text:
        await message.reply_text(
            "Use /grammar followed by a word, phrase, sentence, "
            "or grammar rule.\n\n"
            "You can also reply to a message with /grammar."
        )
        return

    await _send_typing(message)

    result = await analyze_grammar(text)

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation "
            "after several attempts. Please try again."
        )
        return

    await _reply_result(message, result)


# =========================================================
# ARABIC COMMANDS
# =========================================================

async def grammar_reply_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Arabic grammar commands:

    قواعد
    جرامر

    Examples:

    قواعد present perfect
    جرامر I have been studying.

    Can also be used as a reply.
    """

    message = update.effective_message

    if not message or not message.text:
        return

    full_text = message.text.strip()

    # -----------------------------------------------------
    # Remove the Arabic command itself
    # -----------------------------------------------------

    parts = full_text.split(maxsplit=1)

    if len(parts) > 1:
        text = parts[1].strip()
    else:
        text = _get_reply_text(update) or ""

    text = _clean_input(text)

    if not text:
        await message.reply_text(
            "اكتب كلمة أو جملة أو اسم قاعدة بعد «قواعد» أو «جرامر»، "
            "أو استعمل الأمر كردّ على رسالة."
        )
        return

    await _send_typing(message)

    result = await analyze_grammar(text)

    if not result:
        await message.reply_text(
            "تعذر إنشاء شرح القاعدة بعد عدة محاولات. "
            "حاول مرة أخرى."
        )
        return

    await _reply_result(message, result)


# =========================================================
# REGISTRATION
# =========================================================

def register_grammar_handlers(application: Application):
    """
    Register all Grammar handlers.

    Call this once from bot.py.
    """

    # -----------------------------------------------------
    # English commands
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            ["grammar", "gram"],
            grammar_command,
        )
    )

    # -----------------------------------------------------
    # Arabic commands
    #
    # قواعد
    # جرامر
    # قواعد present perfect
    # جرامر passive voice
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(
                r"^(?:قواعد|جرامر)(?:\s+.+)?$"
            ),
            grammar_reply_command,
        )
    )

    print("[GRAMMAR] handlers registered successfully.")
