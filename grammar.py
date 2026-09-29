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
# - Bold section titles
# - Beautiful numbered points
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
    # Normalize strong tags
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

def build_grammar_prompt(text: str):

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
• If the input is a phrase, explain the grammar contained in the phrase.
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

The answer must look clean, strong, organized and distinctive
on Telegram.

Use Telegram HTML.

Use:

<b>important text</b>

All main section titles MUST be bold.

NEVER use Markdown formatting.

NEVER use:

*
**
__
_

NEVER use decorative Markdown stars.

Use the following beautiful numbering styles:

❶
❷
❸
❹
❺

For secondary numbered items:

①
②
③
④
⑤

For important steps and uses:

➊
➋
➌
➍
➎

Do NOT use ordinary numbering such as:

1.
2.
3.

when one of the numbered symbols above is suitable.

━━━━━━━━━━━━━━━━━━

DECORATIVE SECTION FRAMES

Use the following decorative Unicode frames.

MAIN GRAMMAR POINT:

╔═══╾╼═ ✦ 🧠 <b>GRAMMAR POINT</b> ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

WHAT IS IT:

╔═══╾╼═ ❖ 📚 <b>WHAT IS IT?</b> ❖ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

STRUCTURE:

╔═══╾╼═ ✦ 🧩 <b>STRUCTURE</b> ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════╾╼═══╝

WHEN DO WE USE IT:

╔═══╾╼═ ❖ 🎯 <b>WHEN DO WE USE IT?</b> ❖ ═╾╼═══╗
║                                              ║
╚═══╾╼══════════════════════════════╾╼═══╝

EXAMPLES:

╔═══╾╼═ ✦ 💬 <b>EXAMPLES</b> ✦ ═╾╼═══╗
║                                      ║
╚═══╾╼══════════════════════╾╼═══╝

COMPARE:

╔═══╾╼═ ❖ ⚖️ <b>COMPARE</b> ❖ ═╾╼═══╗
║                                      ║
╚═══╾╼════════════════════╾╼═══╝

COMMON MISTAKES:

╔═══╾╼═ ✦ ⚠️ <b>COMMON MISTAKES</b> ✦ ═╾╼═══╗
║                                            ║
╚═══╾╼════════════════════════════╾╼═══╝

IN THIS SENTENCE:

╔═══╾╼═ ❖ 🔎 <b>IN THIS SENTENCE</b> ❖ ═╾╼═══╗
║                                             ║
╚═══╾╼═════════════════════════════╾╼═══╝

Do NOT use every frame automatically.

Use only the sections that are genuinely useful.

━━━━━━━━━━━━━━━━━━

NUMBERING STYLE

Use beautiful numbering throughout the lesson.

GRAMMAR POINTS:

❶ <b>First point</b>
❷ <b>Second point</b>
❸ <b>Third point</b>

IMPORTANT USES:

➊ <b>Use 1</b>
➋ <b>Use 2</b>
➌ <b>Use 3</b>

EXAMPLES:

❶ <b>She has lived here for five years.</b>
هي تعيش هنا منذ خمس سنوات.

❷ <b>I have already finished my homework.</b>
لقد أنهيت واجبي بالفعل.

❸ <b>Have you ever visited London?</b>
هل سبق لك أن زرت لندن؟

COMMON MISTAKES:

① <b>Incorrect:</b> I have went there.
   <b>Correct:</b> I have gone there.
   <b>Why?</b> بعد have نستخدم التصريف الثالث.

② <b>Incorrect:</b> She have finished.
   <b>Correct:</b> She has finished.
   <b>Why?</b> مع she نستخدم has.

COMPARISON:

❶ <b>Present Perfect</b>
...

❷ <b>Past Simple</b>
...

━━━━━━━━━━━━━━━━━━

POSSIBLE SECTIONS

Use ONLY the sections genuinely useful for this input.

<b>Grammar Point</b>

Give the main grammar rule.

If there are several important points,
number them:

❶ ...
❷ ...
❸ ...

<b>What is it?</b>

Explain the rule simply.

If there are several ideas:

❶ ...
❷ ...
❸ ...

<b>Structure</b>

Show the grammatical structure clearly.

For example:

❶ <b>Affirmative:</b>
Subject + have/has + past participle

❷ <b>Negative:</b>
Subject + have/has + not + past participle

❸ <b>Question:</b>
Have/Has + subject + past participle?

<b>When do we use it?</b>

Explain the important uses.

Use:

➊ ...
➋ ...
➌ ...

<b>Examples</b>

Give at least 3 useful examples when teaching a grammar rule.

Every example MUST be beautifully numbered.

Use:

❶ <b>English sentence.</b>
Arabic translation.

❷ <b>English sentence.</b>
Arabic translation.

❸ <b>English sentence.</b>
Arabic translation.

<b>Compare</b>

Use this only when learners commonly confuse
the target grammar with another important structure.

Number each comparison:

❶ <b>Target grammar</b>
...

❷ <b>Similar grammar</b>
...

<b>Common Mistakes</b>

Use this ONLY when genuinely relevant.

Number each mistake:

① <b>Incorrect:</b> ...

   <b>Correct:</b> ...

   <b>Why?</b> ...

② <b>Incorrect:</b> ...

   <b>Correct:</b> ...

   <b>Why?</b> ...

<b>In this sentence</b>

When the user gives a sentence, explain exactly
how the grammar works inside that sentence.

Number the important observations:

❶ ...
❷ ...
❸ ...

━━━━━━━━━━━━━━━━━━

IMPORTANT STYLE

• Keep the answer organized.
• Keep paragraphs short.
• Make all main section titles bold.
• Make important grammar terms bold.
• Make important words in examples bold when useful.
• Use beautiful numbering instead of ordinary numbers.
• Do not make the entire answer bold.
• Do not create empty sections.
• Do not use tables unless a very small comparison
  genuinely requires one.
• Do not repeat the same explanation.
• Do not end every answer with an unnecessary question.
• Do not simply say "correct" or "incorrect".
• Make the response feel like a short, polished grammar lesson.

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
    """
    Send the grammar result safely.

    First attempt:
        Telegram HTML

    Fallback:
        Plain text
    """

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
    # Plain-text fallback
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
    """
    /grammar
    /gram

    Examples:

    /grammar present perfect
    /grammar I have lived here for five years.

    Or reply to a message and use /grammar.
    """

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
    """
    Register all Grammar handlers.

    Call this once from bot.py.
    """

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
