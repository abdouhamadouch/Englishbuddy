# grammar.py
# FixMyEnglish - Grammar Analyzer
#
# Features:
# - /grammar
# - /gram
# - قواعد
# - جرامر
# - Words, phrases, sentences, grammar rules
# - Reply-to-message support
# - Detailed grammar teaching
# - Common Mistakes only when relevant
# - Telegram HTML formatting
# - AI retry system
# - Decorative Unicode frames
# - No Markdown stars
# - AI function injected from bot.py


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

    Removes Markdown stars and unsupported HTML tags while
    preserving useful Telegram HTML.
    """

    if not text:
        return ""

    text = str(text).strip()

    # -----------------------------------------------------
    # Remove code fences
    # -----------------------------------------------------

    text = re.sub(
        r"```(?:html|text|markdown)?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = text.replace("```", "")

    # -----------------------------------------------------
    # Remove Markdown emphasis
    # -----------------------------------------------------

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # -----------------------------------------------------
    # Remove Markdown headings
    # -----------------------------------------------------

    text = re.sub(
        r"(?m)^\s{0,3}#{1,6}\s*",
        "",
        text,
    )

    # -----------------------------------------------------
    # Convert Markdown bullets
    # -----------------------------------------------------

    text = re.sub(
        r"(?m)^\s*[-+]\s+",
        "• ",
        text,
    )

    # -----------------------------------------------------
    # Keep only useful Telegram HTML tags
    # -----------------------------------------------------

    allowed_tags = {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "code",
        "pre",
    }

    def clean_tag(match):
        tag = match.group(0)

        name_match = re.match(
            r"</?\s*([a-zA-Z0-9]+)",
            tag,
        )

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

    # -----------------------------------------------------
    # Normalize strong/em
    # -----------------------------------------------------

    text = re.sub(
        r"<strong>",
        "<b>",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"</strong>",
        "</b>",
        text,
        flags=re.IGNORECASE,
    )

    # -----------------------------------------------------
    # Prevent excessive blank lines
    # -----------------------------------------------------

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text,
    )

    return text.strip()


def _remove_all_html(text: str) -> str:
    """
    Plain-text fallback.
    """

    if not text:
        return ""

    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    text = text.replace("**", "")
    text = text.replace("*", "")

    return text.strip()


# =========================================================
# AI RETRY
# =========================================================

async def _ask_ai_with_retry(prompt: str):
    """
    Ask the injected AI function several times.

    The user will not see a failure until all attempts
    have failed.
    """

    if _ai_function is None:
        print("[GRAMMAR] AI function is not configured.")
        return None

    for attempt in range(
        1,
        MAX_AI_ATTEMPTS + 1,
    ):
        try:

            result = await _ai_function(prompt)

            if result:

                result = _clean_ai_output(
                    str(result)
                )

                if result:

                    print(
                        f"[GRAMMAR] AI succeeded "
                        f"on attempt "
                        f"{attempt}/{MAX_AI_ATTEMPTS}"
                    )

                    return result

            print(
                f"[GRAMMAR] Empty AI response "
                f"(attempt "
                f"{attempt}/{MAX_AI_ATTEMPTS})"
            )

        except Exception as e:

            print(
                f"[GRAMMAR] AI error on attempt "
                f"{attempt}/{MAX_AI_ATTEMPTS}: {e}"
            )

    print(
        "[GRAMMAR] All AI attempts failed."
    )

    return None


# =========================================================
# PROMPT
# =========================================================

def build_grammar_prompt(text: str) -> str:

    return f"""
You are the Grammar Teacher inside FixMyEnglish.

The user wants to LEARN grammar, not merely receive a short correction.

Analyze this input:

USER INPUT:
{text}

━━━━━━━━━━━━━━━━━━

CORE INSTRUCTIONS

• Identify the genuinely relevant grammar.
• Teach the grammar clearly and practically.
• Do not invent grammar problems.
• If the sentence is correct, explain why it is grammatically correct.
• If the sentence is incorrect, explain the real grammatical problem.
• Explain WHY the structure is used, not only WHAT it is.
• If the input is a grammar rule/name, teach that rule directly.
• If the input is a word, explain important grammatical patterns
  and constructions related to that word.
• If the input is a phrase, explain the grammar contained in it.
• Focus mainly on A2-B1 learners.
• Explain advanced grammar only when necessary.
• Avoid unnecessary grammar theory.

━━━━━━━━━━━━━━━━━━

LANGUAGE

• Explain mainly in clear Arabic.
• Keep useful English grammar terminology in English.
• Keep English examples in English.
• Give an Arabic translation for every important example.

━━━━━━━━━━━━━━━━━━

VISUAL DESIGN

The answer must look clean and distinctive on Telegram.

Use Telegram HTML.

Use:

<b>important text</b>

NEVER use Markdown formatting.

NEVER use:

*
**
__
_

NEVER use decorative stars.

Use "•" for bullets.

Use these Unicode decorative frames where appropriate:

╔═══╾╼═ ✦ 🧠 GRAMMAR POINT ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

╔═══╾╼═ ❖ 📚 WHAT IS IT? ❖ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

╔═══╾╼═ ✦ 🧩 STRUCTURE ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════╾╼═══╝

╔═══╾╼═ ❖ 🎯 WHEN DO WE USE IT? ❖ ═╾╼═══╗
║                                              ║
╚═══╾╼══════════════════════════════╾╼═══╝

╔═══╾╼═ ✦ 💬 EXAMPLES ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

╔═══╾╼═ ❖ ⚖️ COMPARE ❖ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

╔═══╾╼═ ✦ ⚠️ COMMON MISTAKES ✦ ═╾╼═══╗
║                                            ║
╚═══╾╼════════════════════════════╾╼═══╝

╔═══╾╼═ ❖ 🔎 IN THIS SENTENCE ❖ ═╾╼═══╗
║                                             ║
╚═══╾╼═════════════════════════════╾╼═══╝

You do NOT need to use every frame.

Use only the sections that are genuinely useful.

━━━━━━━━━━━━━━━━━━

POSSIBLE SECTIONS

<b>Grammar Point</b>

Give the main grammar rule.

<b>What is it?</b>

Explain the rule simply.

<b>Structure</b>

Show the grammatical structure.

Examples:

Subject + have/has + past participle

Subject + be + past participle

If + past simple, would + base verb

<b>When do we use it?</b>

Explain the important uses.

<b>Examples</b>

Give at least 3 useful examples when teaching a grammar rule.

Format:

<b>She has lived here for five years.</b>
هي تعيش هنا منذ خمس سنوات.

<b>Compare</b>

Use this only when learners commonly confuse
the target grammar with another important structure.

<b>Common Mistakes</b>

Use this ONLY when genuinely relevant.

Explain:

• Incorrect form
• Correct form
• Why it is wrong
• Additional examples

<b>In this sentence</b>

When the user gives a sentence, explain exactly
how the grammar works inside that sentence.

━━━━━━━━━━━━━━━━━━

IMPORTANT STYLE

• Keep the answer organized.
• Keep paragraphs short.
• Highlight important grammar terms using <b>...</b>.
• Do not make the entire answer bold.
• Do not create empty sections.
• Do not use tables unless a very small comparison
  genuinely requires one.
• Do not repeat the same explanation.
• Do not end every answer with an unnecessary question.
• Do not simply say "correct" or "incorrect".
• Make the response feel like a short useful grammar lesson.

The final answer MUST contain NO Markdown stars.
"""


# =========================================================
# ANALYZE GRAMMAR
# =========================================================

async def analyze_grammar(text: str):

    text = _clean_input(text)

    if not text:
        return None

    prompt = build_grammar_prompt(text)

    return await _ask_ai_with_retry(prompt)


# =========================================================
# SAFE RESULT SENDING
# =========================================================

async def _reply_result(
    message,
    result: str,
):

    result = _clean_ai_output(
        result or ""
    )

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

        print(
            f"[GRAMMAR] HTML send error: {e}"
        )

    # -----------------------------------------------------
    # Plain text fallback
    # -----------------------------------------------------

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

        print(
            f"[GRAMMAR] Plain-text send error: {e}"
        )


# =========================================================
# TYPING
# =========================================================

async def _send_typing(message):

    try:

        await message.chat.send_action(
            "typing"
        )

    except Exception:
        pass


# =========================================================
# /grammar
# =========================================================

async def grammar_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message:
        return

    text = ""

    # -----------------------------------------------------
    # Command arguments
    # -----------------------------------------------------

    if context.args:

        text = " ".join(
            context.args
        ).strip()

    # -----------------------------------------------------
    # Reply-to-message
    # -----------------------------------------------------

    if not text:

        text = (
            _get_reply_text(update)
            or ""
        )

    text = _clean_input(text)

    if not text:

        await message.reply_text(
            "Use /grammar followed by a word, phrase, "
            "sentence, or grammar rule.\n\n"
            "You can also reply to a message with /grammar."
        )

        return

    await _send_typing(message)

    result = await analyze_grammar(
        text
    )

    if not result:

        await message.reply_text(
            "I couldn't generate the grammar explanation "
            "after several attempts. Please try again."
        )

        return

    await _reply_result(
        message,
        result,
    )


# =========================================================
# ARABIC COMMANDS
# =========================================================

async def grammar_reply_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message or not message.text:
        return

    full_text = message.text.strip()

    parts = full_text.split(
        maxsplit=1
    )

    if len(parts) > 1:

        text = parts[1].strip()

    else:

        text = (
            _get_reply_text(update)
            or ""
        )

    text = _clean_input(text)

    if not text:

        await message.reply_text(
            "اكتب كلمة أو جملة أو اسم قاعدة بعد "
            "«قواعد» أو «جرامر»، أو استعمل الأمر "
            "كردّ على رسالة."
        )

        return

    await _send_typing(message)

    result = await analyze_grammar(
        text
    )

    if not result:

        await message.reply_text(
            "تعذر إنشاء شرح القاعدة بعد عدة محاولات. "
            "حاول مرة أخرى."
        )

        return

    await _reply_result(
        message,
        result,
    )


# =========================================================
# REGISTRATION
# =========================================================

def register_grammar_handlers(
    application: Application,
):

    # -----------------------------------------------------
    # English commands
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            [
                "grammar",
                "gram",
            ],
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

    print(
        "[GRAMMAR] handlers registered successfully."
    )
