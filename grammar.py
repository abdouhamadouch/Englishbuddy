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

def _clean_ai_output(text: str):

    if not text:
        return ""

    text = str(text).strip()

    empty_markers = {
        "❌ empty ai response.",
        "❌ empty ai response",
        "empty ai response.",
        "empty ai response",
    }

    if text.lower() in empty_markers:
        return ""

    # Remove code fences.
    text = re.sub(
        r"```(?:html|HTML)?",
        "",
        text,
    )

    text = text.replace("```", "")

    # Remove Markdown headings.
    text = re.sub(
        r"^\s*#{1,6}\s*",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Convert Markdown bullets.
    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text,
    )

    # Remove Markdown decoration.
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Normalize strong.
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

    # Allowed Telegram HTML tags.
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

    # Remove excessive blank lines.
    text = re.sub(
        r"\n[ \t]*\n[ \t]*\n+",
        "\n\n",
        text,
    )

    return text.strip()


def _remove_all_html(text: str):

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

        result = await _ai_function(
            prompt,
            max_tokens=GRAMMAR_MAX_TOKENS,
            system_prompt=(
                "You are FixMyEnglish Grammar Teacher. "
                "Teach English grammar accurately and clearly. "
                "Use simple English suitable for A2-B1 learners. "
                "English is the main language. "
                "Use Arabic only for important translations "
                "and short necessary clarifications. "
                "Give complete and useful examples."
            ),
        )

        return result

    except TypeError as e:

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

RETRY INSTRUCTION:

This is retry attempt {attempt}.

The previous attempt did not produce a usable answer.

Generate the complete grammar lesson again from the beginning.

Make it complete, accurate, organized, and concise enough
to finish completely.

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

def build_grammar_prompt(text: str):

    return f"""
You are the Grammar Teacher inside FixMyEnglish.

Analyze this input:

"{text}"

Your job is to TEACH the relevant grammar clearly,
accurately, and practically.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• English is the main language.

• Use simple, natural English suitable for an A2-B1 learner.

• Do NOT write the whole lesson in Arabic.

• Use Arabic mainly for:
  - translating the grammar rule when useful
  - translating English examples
  - short important clarifications

• Do not translate every explanation sentence.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACCURACY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Explain the real grammar involved.

• Do not invent mistakes.

• If the sentence is correct, say that it is correct
  and explain the grammar behind it.

• If it is incorrect, identify the real error,
  correct it, and explain why.

• If the user gives the name of a grammar rule,
  teach that rule directly.

• Do not discuss unrelated grammar.

• Prefer accurate, useful explanations over long explanations.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VISUAL STYLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Do NOT use rectangular frames or borders.

Do NOT use complicated decorative symbols.

Do NOT use:

✦
★
☆
⟦ ⟧
╔ ╗ ╚ ╝
┌ ┐ └ ┘

Make the lesson visually organized and lively using
simple colored emojis and numbered sections.

Use different functional emojis such as:

🟦
🟩
🟨
🟧
🟥
🔵
🟢
🟡
🟠
🔴
📌
💡
⚠️
✅
❌

Do not overuse them.

The emojis should organize the lesson, not decorate every line.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION ORGANIZATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use only the sections that are genuinely useful.

Possible structure:

🟦 <b>① GRAMMAR POINT</b>

🟢 <b>② WHAT IS IT?</b>

🟨 <b>③ STRUCTURE</b>

🟠 <b>④ WHEN DO WE USE IT?</b>

🔵 <b>⑤ EXAMPLES</b>

🟣 <b>⑥ COMPARE</b>

⚠️ <b>⑦ COMMON MISTAKES</b>

📌 <b>⑧ IN THIS SENTENCE</b>

💡 <b>⑨ QUICK TIP</b>

Do not create empty sections.

Do not force every section into every answer.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GRAMMAR POINT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start by clearly identifying the grammar.

Example:

🟦 <b>① GRAMMAR POINT</b>

<b>Second Conditional</b>

The Second Conditional is used to talk about
imaginary, unreal, or unlikely situations.

Arabic translation may be added briefly:

يُستخدم للحديث عن مواقف افتراضية أو غير حقيقية.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the idea in simple English.

Use numbered points when useful:

❶ ...
❷ ...
❸ ...

Arabic should only be used for important translations
or short clarifications.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show the grammar structure clearly.

Example:

🟨 <b>③ STRUCTURE</b>

❶ <b>If + past simple, would + base verb</b>

Explain what each part means.

Highlight important grammar forms with <b>...</b>.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEN DO WE USE IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the main uses.

For example:

❶ <b>Imaginary situations</b>
We imagine a situation that is not true now.

❷ <b>Unlikely situations</b>
We talk about something possible but not very likely.

Use Arabic only when a translation or short clarification
is genuinely useful.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Examples are very important.

Give at least 3 useful examples when teaching a grammar rule.

Give more when there are several important uses and
the extra examples are genuinely helpful.

Every example must be complete.

Use:

🔵 <b>⑤ EXAMPLES</b>

❶ <b>If I had more time, I would study more.</b>
لو كان لدي وقت أكثر، لدرست أكثر.

❷ <b>If she studied harder, she would pass the exam.</b>
لو درست بجدية أكبر، لنجحت في الامتحان.

❸ <b>If we lived near the school, we would walk there.</b>
لو كنا نعيش بالقرب من المدرسة، لذهبنا إلى هناك مشيًا.

Rules:

• English examples must be bold.

• Every English example must have an Arabic translation.

• The Arabic translation should immediately follow
  the English example.

• Do not translate the whole explanation.

• Do not leave an example incomplete.

• Use natural English examples, not artificial sentences.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARISON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this only when the learner may confuse the grammar
with another structure.

For example:

🟣 <b>⑥ COMPARE</b>

❶ <b>First Conditional</b>
Used for real or possible future situations.

❷ <b>Second Conditional</b>
Used for imaginary or unlikely situations.

Then give short examples if useful.

Do not make unnecessary comparisons.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when there are genuine common mistakes.

⚠️ <b>⑦ COMMON MISTAKES</b>

Show:

❶ ❌ Incorrect
❷ ✅ Correct
❸ Explain why.

Example:

❶ ❌ <b>If I will have money, I would travel.</b>

❷ ✅ <b>If I had money, I would travel.</b>

❸ <b>Why?</b>
In the Second Conditional, we normally use
past simple after <b>if</b>, not <b>will</b>.

Arabic can be used for the important clarification:

في Second Conditional نستخدم past simple بعد if.

Do not invent common mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the user provides a sentence, explain the grammar
inside that exact sentence.

Focus on the words and structures actually used.

For example:

📌 <b>⑧ IN THIS SENTENCE</b>

❶ <b>had</b> = past simple form.

❷ <b>would study</b> = hypothetical result.

❸ The sentence describes an unreal situation.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this only when a genuinely useful memory tip exists.

Example:

💡 <b>⑨ QUICK TIP</b>

Remember:

<b>If + past simple → would + base verb</b>

Keep it short.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMATTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use Telegram HTML.

Use <b>...</b> for:

• important grammar terms
• structures
• corrections
• English examples
• section titles

Do not use Markdown bold.

Do not use Markdown headings.

Do not use decorative stars.

Do not use rectangular frames.

Use colored emojis mainly for section organization.

Use numbered points such as:

❶ ❷ ❸ ❹ ❺

Keep the lesson visually clean and easy to read on a phone.

Use this separator between major sections when useful:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Do not put a separator after every small point.

Do not use unnecessary tables.

Do not repeat the same explanation.

Do not fill the answer with emojis.

The lesson should feel lively but still like a serious
and accurate English grammar lesson.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPLETENESS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every sentence must be complete.

Every example must be complete.

Every Arabic translation must be complete.

Every section must be complete.

Every HTML tag must be complete.

If the answer becomes too long:

1. Remove repetition.
2. Shorten explanations.
3. Keep the important examples.
4. Do not leave anything unfinished.

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
