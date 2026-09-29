# grammar.py
# FixMyEnglish - Grammar Analyzer

import re

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

_ai_function = None

MAX_INPUT_LENGTH = 3000
MAX_AI_ATTEMPTS = 4
GRAMMAR_MAX_TOKENS = 1800


# ============================================================
# AI INJECTION
# ============================================================

def set_ai_function(ai_function):
    global _ai_function
    _ai_function = ai_function

    print(
        "[GRAMMAR] AI function connected:",
        getattr(ai_function, "__name__", str(ai_function))
    )


# ============================================================
# GET REPLIED MESSAGE
# ============================================================

def _get_reply_text(update: Update):
    message = update.effective_message

    if not message or not message.reply_to_message:
        return None

    replied = message.reply_to_message

    if replied.text:
        return replied.text.strip()

    if replied.caption:
        return replied.caption.strip()

    return None


# ============================================================
# CLEAN INPUT
# ============================================================

def _clean_input(text: str) -> str:
    if not text:
        return ""

    text = str(text).strip()

    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    return text


# ============================================================
# CLEAN AI OUTPUT
# ============================================================

def _clean_ai_output(text: str) -> str:

    if not text:
        return ""

    text = str(text).strip()

    # Known empty-response messages from bot.py
    empty_markers = {
        "❌ Empty AI response.",
        "❌ empty ai response.",
        "empty ai response.",
        "empty ai response",
    }

    if text.lower() in {
        item.lower()
        for item in empty_markers
    }:
        return ""

    # Remove code fences
    text = re.sub(
        r"```(?:html|HTML)?",
        "",
        text,
    )

    text = text.replace("```", "")

    # Remove Markdown headings
    text = re.sub(
        r"^\s*#{1,6}\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Markdown bullets -> Telegram bullets
    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text,
    )

    # Remove Markdown decoration
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Normalize strong -> b
    text = re.sub(
        r"<\s*strong\s*>",
        "<b>",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"<\s*/\s*strong\s*>",
        "</b>",
        text,
        flags=re.IGNORECASE,
    )

    # Allowed Telegram HTML
    allowed_tags = {
        "b",
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
            r"<\s*/?\s*([a-zA-Z0-9]+)",
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

    # Remove excessive blank lines
    text = re.sub(
        r"\n[ \t]*\n[ \t]*\n+",
        "\n\n",
        text,
    )

    return text.strip()


def _remove_all_html(text: str) -> str:

    if not text:
        return ""

    return re.sub(
        r"</?[^>]+>",
        "",
        text,
    )


# ============================================================
# AI REQUEST
# ============================================================

async def _call_ai(prompt: str):

    if _ai_function is None:
        print("[GRAMMAR] ERROR: AI function is not configured.")
        return None

    try:
        # The normal FixMyEnglish ask_groq interface is:
        #
        # ask_groq(prompt, max_tokens=1200, system_prompt=None)
        #
        # We deliberately use keyword arguments here so this remains
        # compatible with the current bot.py implementation.

        result = await _ai_function(
            prompt,
            max_tokens=GRAMMAR_MAX_TOKENS,
            system_prompt=(
                "You are FixMyEnglish Grammar Teacher. "
                "Teach English grammar clearly and accurately. "
                "Use simple English suitable for A2-B1 learners. "
                "Use Arabic only for important translations "
                "and short necessary clarifications."
            ),
        )

        return result

    except TypeError as e:

        # Compatibility fallback in case the injected function
        # only accepts one argument.

        print(
            f"[GRAMMAR] AI function does not accept "
            f"extended arguments: {e}"
        )

        try:
            result = await _ai_function(prompt)
            return result

        except Exception as second_error:

            print(
                f"[GRAMMAR] AI fallback error: {second_error}"
            )

            return None

    except Exception as e:

        print(
            f"[GRAMMAR] AI call error: {e}"
        )

        return None


# ============================================================
# AI WITH RETRIES
# ============================================================

async def _ask_ai_with_retry(prompt: str):

    if _ai_function is None:
        print("[GRAMMAR] AI function is not configured.")
        return None

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):

        try:

            current_prompt = prompt

            if attempt > 1:

                current_prompt = f"""
{prompt}

IMPORTANT:
This is retry attempt {attempt}.

Generate the complete answer again from the beginning.

The previous attempt did not return a usable answer.

Make the response complete and concise enough to finish
within the available response length.

Do not mention this retry instruction.
"""

            result = await _call_ai(current_prompt)

            if result is None:

                print(
                    f"[GRAMMAR] AI returned None "
                    f"on attempt {attempt}/{MAX_AI_ATTEMPTS}."
                )

                continue

            result = str(result).strip()

            # Detect bot.py's empty-response message.
            if (
                not result
                or result.lower()
                in {
                    "❌ empty ai response.",
                    "❌ empty ai response",
                    "empty ai response.",
                    "empty ai response",
                }
            ):

                print(
                    f"[GRAMMAR] Empty AI response "
                    f"on attempt {attempt}/{MAX_AI_ATTEMPTS}."
                )

                continue

            cleaned = _clean_ai_output(result)

            if not cleaned:

                print(
                    f"[GRAMMAR] AI result became empty after cleaning "
                    f"on attempt {attempt}/{MAX_AI_ATTEMPTS}."
                )

                continue

            print(
                f"[GRAMMAR] AI response received "
                f"on attempt {attempt}."
            )

            return cleaned

        except Exception as e:

            print(
                f"[GRAMMAR] AI error "
                f"on attempt {attempt}/{MAX_AI_ATTEMPTS}: {e}"
            )

    print("[GRAMMAR] All AI attempts failed.")

    return None


# ============================================================
# GRAMMAR PROMPT
# ============================================================

def build_grammar_prompt(text: str) -> str:

    return f"""
You are the Grammar Teacher inside FixMyEnglish.

Analyze this user input:

"{text}"

Teach the relevant grammar clearly and accurately.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The main explanation must be in simple English suitable
for an A2-B1 learner.

Do NOT write the whole lesson in Arabic.

Use Arabic only for:

• important grammar-rule translations
• Arabic translations of English examples
• short clarifications when they are genuinely useful

English must remain the main language.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TEACH THE GRAMMAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Do not merely name the grammar rule.

Explain:

• what the grammar is
• how the structure works
• when it is used
• why it is used
• useful examples
• important differences when relevant
• genuine common mistakes when relevant

If the input is already correct, do not invent an error.
Explain the grammar that makes it correct.

If the input is incorrect, explain the real grammatical
problem and give the corrected form.

If the input is the name of a grammar rule, teach that rule
directly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TITLE FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Each main section title must use this simple rectangular frame:

┌──────────────────────────────┐
│   <b>① GRAMMAR POINT</b>     │
└──────────────────────────────┘

Use a simple, slightly rough rectangular border.

Do NOT use complicated decorative borders.

Do NOT use:

✦
★
☆
⟦ ⟧
╔ ╗ ╚ ╝
or decorative stars.

Only the title gets the frame.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION ORDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use only the sections that are useful.

Possible sections:

① GRAMMAR POINT
② WHAT IS IT?
③ STRUCTURE
④ WHEN DO WE USE IT?
⑤ EXAMPLES
⑥ COMPARE
⑦ COMMON MISTAKES
⑧ IN THIS SENTENCE
⑨ QUICK TIP

Use the numbered symbols ① ② ③ etc.

For points and examples use:

❶ ❷ ❸ ❹ ❺ ❻

Do not create empty sections.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXPLANATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the grammar in clear, simple English.

For example:

The Second Conditional is used for imaginary,
unreal, or unlikely situations.

Arabic may be added briefly:

يُستخدم للحديث عن مواقف افتراضية أو غير حقيقية.

Do not translate the whole explanation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show important structures clearly.

Example:

❶ <b>If + past simple, would + base verb</b>

Then explain the structure simply.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Examples are very important.

Give at least 3 useful examples for a grammar rule.

Give more when there are several important uses and
the extra examples are genuinely useful.

Every example must be complete.

Use:

❶ <b>If I had more time, I would study more.</b>
لو كان لدي وقت أكثر، لدرست أكثر.

❷ <b>If she studied harder, she would pass the exam.</b>
لو درست بجدية أكبر، لنجحت في الامتحان.

❸ <b>If we lived near the school, we would walk there.</b>
لو كنا نعيش بالقرب من المدرسة، لذهبنا إلى هناك مشيًا.

Rules:

• English example must be bold.
• Arabic translation must immediately follow it.
• Every English example must have an Arabic translation.
• Do not translate every explanation sentence.
• Do not start an example that cannot be completed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARISONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use a comparison only when learners commonly confuse
the target grammar with another structure.

Keep it concise.

Explain the real difference clearly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY if there are genuine common mistakes.

Show:

❶ Incorrect form
❷ Correct form
❸ Why it is wrong

Use Arabic only for a short useful clarification.

Do not invent common mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the user gives a sentence, explain how the grammar
works specifically inside that sentence.

Focus on the actual sentence.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this only when there is a genuinely useful memory tip.

Keep it short.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMATTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use Telegram HTML.

Use <b> for important words, structures, corrections,
examples and titles.

Do not use Markdown bold.

Do not use Markdown headings.

Do not use decorative Markdown stars.

Use this separator:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use numbered organization, not decorative clutter.

Do not fill the lesson with emojis.

Do not use unnecessary tables.

Keep the lesson easy to read on a phone.

Do not repeat information.

The answer must be complete.

Never leave:

• an unfinished sentence
• an unfinished example
• an unfinished Arabic translation
• an unfinished section
• an unfinished HTML tag

If the lesson becomes too long, shorten explanations and
remove repetition before removing useful examples.

End naturally with a complete sentence.
"""


# ============================================================
# ANALYZE
# ============================================================

async def analyze_grammar(text: str):

    text = _clean_input(text)

    if not text:
        return None

    prompt = build_grammar_prompt(text)

    return await _ask_ai_with_retry(prompt)


# ============================================================
# SEND RESULT
# ============================================================

async def _reply_result(message, result: str):

    result = (result or "").strip()

    if not result:

        await message.reply_text(
            "I couldn't generate the grammar explanation. "
            "Please try again."
        )

        return

    result = _clean_ai_output(result)

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

    except Exception as e:

        print(
            f"[GRAMMAR] HTML send error: {e}"
        )

        plain = _remove_all_html(result)

        try:

            await message.reply_text(
                plain,
                disable_web_page_preview=True,
            )

        except Exception as second_error:

            print(
                f"[GRAMMAR] Plain send error: {second_error}"
            )


# ============================================================
# TYPING
# ============================================================

async def _send_typing(message):

    try:

        await message.chat.send_action("typing")

    except Exception as e:

        print(
            f"[GRAMMAR] Typing action error: {e}"
        )


# ============================================================
# /grammar /gram
# ============================================================

async def grammar_command(update, context):

    message = update.effective_message

    if not message:
        return

    text = ""

    if context.args:

        text = " ".join(
            context.args
        ).strip()

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
            "I couldn't generate the grammar explanation. "
            "Please try again."
        )

        return

    await _reply_result(
        message,
        result,
    )


# ============================================================
# قواعد / جرامر
# ============================================================

async def grammar_reply_command(update, context):

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
            "تعذر إنشاء شرح القاعدة. حاول مرة أخرى."
        )

        return

    await _reply_result(
        message,
        result,
    )


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register_grammar_handlers(application: Application):

    application.add_handler(
        CommandHandler(
            ["grammar", "gram"],
            grammar_command,
        )
    )

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
