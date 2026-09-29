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
# - Uses Telegram HTML formatting
# - AI function is injected from bot.py
# - Retries AI several times if the response fails
# - Clean, organized Unicode frames
# - Bold section titles
# - Numbered sections and numbered examples
# - No decorative Markdown stars

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
# CONFIGURATION
# ============================================================

_ai_function = None

MAX_INPUT_LENGTH = 3000
MAX_AI_ATTEMPTS = 4


# ============================================================
# AI INJECTION
# ============================================================

def set_ai_function(ai_function):
    global _ai_function
    _ai_function = ai_function


# ============================================================
# REPLY TEXT
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
# INPUT CLEANING
# ============================================================

def _clean_input(text: str) -> str:
    if not text:
        return ""

    text = str(text).strip()

    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    return text


# ============================================================
# AI OUTPUT CLEANING
# ============================================================

def _clean_ai_output(text: str) -> str:
    """
    Clean AI output while keeping Telegram HTML formatting.

    We remove Markdown formatting and unsupported HTML,
    but preserve useful Telegram HTML tags.
    """

    if not text:
        return ""

    text = str(text).strip()

    # Remove code fences.
    text = re.sub(r"```(?:html|HTML)?", "", text)
    text = text.replace("```", "")

    # Remove Markdown headings.
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)

    # Convert Markdown bullets to Telegram bullets.
    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text,
    )

    # Remove Markdown bold/italic markers.
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Normalize strong to b.
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

    # Keep only safe Telegram HTML tags.
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

    # Normalize excessive blank lines.
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
# AI REQUEST WITH RETRIES
# ============================================================

async def _ask_ai_with_retry(prompt: str):
    if _ai_function is None:
        print("[GRAMMAR] AI function is not configured.")
        return None

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):

        try:
            result = await _ai_function(prompt)

            if result:
                result = str(result).strip()

                if result:
                    cleaned = _clean_ai_output(result)

                    if cleaned:
                        print(
                            f"[GRAMMAR] AI response received "
                            f"on attempt {attempt}."
                        )
                        return cleaned

            print(
                f"[GRAMMAR] Empty AI response "
                f"on attempt {attempt}/{MAX_AI_ATTEMPTS}."
            )

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

The user wants to LEARN grammar, not merely receive a short correction.

Analyze the following user input:

USER INPUT:
{text}

Your job is to identify the genuinely relevant grammar and TEACH it clearly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT TEACHING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Do not give a useless one-line answer.

• Do not automatically treat every input as an error.

• If the sentence is correct, explain the grammar rule that makes it correct.

• If the sentence is incorrect, explain the real grammatical problem and why it is wrong.

• If the user gives a grammar rule/name such as "Present Perfect", teach that rule directly.

• If the user gives a word, explain the important grammatical patterns and constructions associated with that word.

• If the user gives a phrase, explain the grammar contained in the phrase.

• Do not invent grammar problems.

• Do not discuss grammar that is unrelated to the user's input.

• Focus on useful English grammar for an A2-B1 learner, but explain more advanced grammar when the input requires it.

• Explain mainly in clear Arabic.

• Keep English grammar terms in English when useful.

• Make the explanation easy to understand.

• Give practical examples.

• Explain WHY the structure is used, not only WHAT it is.

• Do not overload the answer with unnecessary theory.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DESIGN AND FORMATTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The final answer must look like a beautifully organized mini grammar lesson.

Use Telegram HTML.

IMPORTANT:
• Use <b>...</b> for ALL major section titles.
• Use <b>...</b> for important grammar names, structures, corrections and key points.
• Section titles must be visually strong and clearly bold.
• NEVER use Markdown **bold**.
• NEVER use Markdown *italic*.
• NEVER use decorative stars such as * or **.
• Do not use Markdown headings.
• Use Unicode symbols and Unicode frames instead.
• Do not use tables unless a very small comparison genuinely requires one.

The design MUST use organized double Unicode frames.

Use this general frame style:

╔══════════════════════════════════════╗
║  🧠  <b>① GRAMMAR POINT</b>          ║
╚══════════════════════════════════════╝

For sections with more content, use:

╔══════════════════════════════════════╗
║  📘  <b>② WHAT IS IT?</b>            ║
╠══════════════════════════════════════╣
║                                      ║
║  content                             ║
║                                      ║
╚══════════════════════════════════════╝

Use:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

between major sections.

The frames should be neat, balanced and consistent.

Do NOT make random ugly boxes.

Do NOT use a different random structure for every sentence.

Keep the design readable on a phone.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECTION NUMBERING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Number the MAIN sections sequentially.

Use:

①
②
③
④
⑤
⑥
⑦

For example:

╔══════════════════════════════════════╗
║  🧠  <b>① GRAMMAR POINT</b>          ║
╚══════════════════════════════════════╝

Then:

╔══════════════════════════════════════╗
║  📘  <b>② WHAT IS IT?</b>            ║
╚══════════════════════════════════════╝

Then:

╔══════════════════════════════════════╗
║  🧩  <b>③ STRUCTURE</b>              ║
╚══════════════════════════════════════╝

Then:

╔══════════════════════════════════════╗
║  ⏰  <b>④ WHEN DO WE USE IT?</b>      ║
╚══════════════════════════════════════╝

Then:

╔══════════════════════════════════════╗
║  💬  <b>⑤ EXAMPLES</b>                ║
╚══════════════════════════════════════╝

Then, only if useful:

╔══════════════════════════════════════╗
║  ⚖️  <b>⑥ COMPARE</b>                 ║
╚══════════════════════════════════════╝

Then, only if genuinely relevant:

╔══════════════════════════════════════╗
║  ⚠️  <b>⑦ COMMON MISTAKES</b>          ║
╚══════════════════════════════════════╝

Do NOT create sections that are not useful.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NUMBERING INSIDE SECTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every important list must be numbered.

Use:

❶
❷
❸
❹
❺
❻

Do NOT use plain unnumbered paragraphs when the information is naturally a list.

For example:

❶ <b>Subject + have/has + past participle</b>

❷ <b>He/She/It + has</b>

❸ <b>I/You/We/They + have</b>

For examples, ALWAYS use:

❶
❷
❸

Do NOT write:

Example 1:
Example 2:
Example 3:

Do NOT leave examples unnumbered.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GRAMMAR POINT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start with the main grammar point.

Example:

╔══════════════════════════════════════╗
║  🧠  <b>① GRAMMAR POINT</b>          ║
╚══════════════════════════════════════╝

<b>Present Perfect</b>

Give a short clear identification of the target grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the rule in simple Arabic.

If useful, number the main ideas:

❶ ...
❷ ...
❸ ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show the grammatical structure clearly.

Use simple notation such as:

<b>Subject + have/has + past participle</b>

If there are several important structures, number them:

❶ <b>Subject + have/has + V3</b>

❷ <b>Subject + have/has not + V3</b>

❸ <b>Have/Has + subject + V3?</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEN DO WE USE IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the important uses.

Number each genuinely important use:

❶ <b>Experience</b>
شرح عربي واضح.

❷ <b>An unfinished situation</b>
شرح عربي واضح.

❸ <b>A recent action with a present result</b>
شرح عربي واضح.

Do not invent uses that are unrelated to the target grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This section is extremely important.

Every example MUST be numbered using:

❶
❷
❸
❹
❺

Each English example must be bold.

Immediately below it, give its Arabic translation.

Example:

❶ <b>She has lived here for five years.</b>
هي تعيش هنا منذ خمس سنوات.

❷ <b>I have already finished my homework.</b>
لقد أنهيت واجبي بالفعل.

❸ <b>They have never visited London.</b>
لم يزوروا لندن من قبل.

Give at least 3 useful new examples when a grammar rule is being taught.

Use more examples when genuinely useful.

Do not number the Arabic translation separately.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when there is an important similar structure that learners commonly confuse with the target rule.

Number the comparison:

❶ <b>Present Perfect</b>
Example and explanation.

❷ <b>Past Simple</b>
Example and explanation.

❸ <b>The difference</b>
Explain the difference clearly in Arabic.

Do not compare unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when there are genuine common mistakes related to the target grammar.

Every mistake must be clearly numbered.

Use:

❶ ❌ <b>I have saw him.</b>

❷ ✅ <b>I have seen him.</b>

❸ <b>Why?</b>
بعد have/has نستخدم التصريف الثالث للفعل.

If there are multiple mistakes, number them:

❶
❷
❸

Do not invent mistakes.

Do not include stylistic preferences as grammar mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When the user gives a sentence, explain exactly how the grammar works inside that sentence.

Use numbered points:

❶ <b>has lived</b> is the Present Perfect form.

❷ <b>for five years</b> shows the duration.

❸ The sentence describes a situation that started in the past and continues until now.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL STYLE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• The response should feel like a polished mini grammar lesson.

• Use clear Arabic explanations.

• Keep English grammar terms where useful.

• Major section titles MUST be bold using Telegram HTML <b>...</b>.

• Important English examples MUST be bold.

• Important grammar structures MUST be bold.

• Important corrections MUST be bold.

• Use the main section numbering ① ② ③ ④ ⑤ ⑥ ⑦.

• Use internal numbering ❶ ❷ ❸ ❹ ❺.

• Keep the sections visually separated.

• Use double Unicode frames.

• Use "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" between major sections.

• Do not use Markdown stars.

• Do not use decorative asterisks.

• Do not use Markdown headings.

• Do not produce empty sections.

• Do not produce unnecessary sections.

• Do not use a table unless a very small comparison genuinely requires one.

• Do not put the whole explanation in one paragraph.

• Keep it easy to read on a phone.

• Never mention these formatting instructions to the user.

• Never say that you are following a template.

The final answer should be clean, professional, visually attractive and highly organized.
"""


# ============================================================
# ANALYZE GRAMMAR
# ============================================================

async def analyze_grammar(text: str):
    text = _clean_input(text)

    if not text:
        return None

    prompt = build_grammar_prompt(text)

    result = await _ask_ai_with_retry(prompt)

    return result


# ============================================================
# SEND RESULT
# ============================================================

async def _reply_result(message, result: str):

    result = (result or "").strip()

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation."
        )
        return

    result = _clean_ai_output(result)

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation."
        )
        return

    try:
        await message.reply_text(
            result,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as e:
        print(f"[GRAMMAR] HTML send error: {e}")

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
# TYPING ACTION
# ============================================================

async def _send_typing(message):

    try:
        await message.chat.send_action("typing")

    except Exception as e:
        print(f"[GRAMMAR] Typing action error: {e}")


# ============================================================
# /grammar AND /gram
# ============================================================

async def grammar_command(update, context):

    message = update.effective_message

    if not message:
        return

    text = ""

    if context.args:
        text = " ".join(context.args).strip()

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

    await _reply_result(message, result)


# ============================================================
# ARABIC COMMANDS
# قواعد / جرامر
# ============================================================

async def grammar_reply_command(update, context):

    message = update.effective_message

    if not message or not message.text:
        return

    full_text = message.text.strip()

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
            "تعذر إنشاء شرح القاعدة. حاول مرة أخرى."
        )

        return

    await _reply_result(message, result)


# ============================================================
# REGISTER HANDLERS
# ============================================================

def register_grammar_handlers(application: Application):

    # /grammar
    # /gram
    application.add_handler(
        CommandHandler(
            ["grammar", "gram"],
            grammar_command,
        )
    )

    # قواعد
    # جرامر
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
