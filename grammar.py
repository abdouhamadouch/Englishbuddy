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
# - Retries AI several times
# - Organized title frames
# - Bold titles
# - Numbered sections and examples
# - Quick Tip when useful
# - Complete-answer protection
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
    if not text:
        return ""

    text = str(text).strip()

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

    # Convert Markdown bullets to Telegram bullets.
    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text,
    )

    # Remove Markdown bold / italic.
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Normalize <strong> to <b>.
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

    # Telegram-safe HTML tags.
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

            current_prompt = prompt

            # Only add a stronger completeness instruction
            # on retries. Do not reject the response afterward.
            if attempt > 1:
                current_prompt = f"""
{prompt}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT RETRY INSTRUCTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This is retry attempt {attempt}.

Generate the COMPLETE grammar explanation again
from the beginning.

Do not stop halfway.

Complete every sentence, example, translation,
numbered point and section before ending.

Use fewer examples or shorter explanations if necessary.

Do NOT start another example if you do not have
enough space to finish both the English sentence
and its Arabic translation.

Keep the answer concise enough for Telegram.

Do not mention this retry instruction.
"""

            result = await _ai_function(current_prompt)

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

• If the user gives a grammar rule or grammar name such as:
  Present Perfect
  Past Perfect
  First Conditional
  Second Conditional
  Third Conditional
  Passive Voice
  Reported Speech
  Used to
  Wish
  Relative Clauses

  teach that grammar rule directly.

• If the user gives a numbered grammar name such as:
  "2 conditional"
  "second conditional"
  "conditional 2"

  understand that the user means the Second Conditional
  and teach it directly.

• Do not ask the user to clarify an obvious grammar-rule name.

• If the user gives a word, explain the important grammatical
  patterns and constructions associated with that word.

• If the user gives a phrase, explain the grammar contained in the phrase.

• Do not invent grammar problems.

• Do not discuss grammar unrelated to the user's input.

• Focus on useful English grammar for an A2-B1 learner,
  but explain more advanced grammar when the input requires it.

• Explain mainly in clear Arabic.

• Keep English grammar terms in English when useful.

• Make the explanation easy to understand.

• Give practical examples.

• Explain WHY the structure is used, not only WHAT it is.

• Do not overload the answer with unnecessary theory.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPLETE ANSWER REQUIREMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The answer MUST be complete.

Do not stop in the middle of:

• a sentence
• an example
• a translation
• a numbered point
• a grammar rule
• a comparison
• a common mistake
• an HTML tag
• a section

Before finishing, make sure the final sentence is complete.

IMPORTANT:

Prefer a SHORT COMPLETE answer over a long incomplete answer.

If the explanation is becoming long:

• shorten the explanation
• remove unnecessary details
• use fewer examples
• skip an unnecessary section

NEVER sacrifice completeness just to add more information.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TELEGRAM LENGTH CONTROL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The entire answer must be concise enough for ONE Telegram message.

Aim for approximately 500–850 words maximum.

For simple grammar topics, use much less.

Do NOT try to fill the available space.

Do NOT add unnecessary examples.

Completeness is more important than quantity.

If you have already explained the rule clearly,
do not continue adding extra material.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TITLE DESIGN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The title must be surrounded by a compact decorative frame.

Do NOT put the entire answer inside one huge box.

Use this style:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

The title itself must be bold.

The title must be visually surrounded by:

⟦ <b>...</b> ⟧

Use the same general design for the other sections.

Examples:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>② WHAT IS IT?</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>③ STRUCTURE</b> ⟧ ✦ ═══════╗
╚════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>④ WHEN DO WE USE IT?</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>⑤ EXAMPLES</b> ⟧ ✦ ═══════╗
╚════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>⑥ COMPARE</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>⑦ COMMON MISTAKES</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>⑧ IN THIS SENTENCE</b> ⟧ ✦ ═══════╗
╚════════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>⑨ QUICK TIP</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════╝

Adjust the decorative line length if necessary so that
the title looks balanced.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN SECTION NUMBERING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use:

① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨

Use only sections that are actually useful.

Do not create empty sections.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NUMBERING INSIDE SECTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use:

❶
❷
❸
❹
❺
❻

Every natural list should be numbered.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GRAMMAR POINT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start with the main grammar rule.

Example:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

<b>Second Conditional</b>

Give a short and clear identification.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the rule simply in Arabic.

Number the important ideas:

❶ ...

❷ ...

❸ ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show the grammatical structure clearly.

Example:

❶ <b>If + past simple, would + base verb</b>

❷ <b>If I had more time, I would study English.</b>

If there are positive, negative or question forms
that are genuinely useful, explain them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEN DO WE USE IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the important uses.

Number them:

❶ <b>Unreal or unlikely situations</b>
شرح عربي واضح.

❷ <b>Imaginary situations</b>
شرح عربي واضح.

❸ <b>Advice or hypothetical results</b>
شرح عربي واضح.

Only include uses relevant to the target grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Give 2 COMPLETE useful examples by default.

A third example is allowed ONLY if the answer is still
short enough and there is enough space to finish it completely.

Every example MUST be numbered.

Use:

❶ <b>If I had more money, I would travel more.</b>
لو كان لدي مال أكثر، لسافرت أكثر.

❷ <b>If she studied harder, she would pass the exam.</b>
لو درست بجدية أكبر، لنجحت في الامتحان.

Important:

• English examples must be bold.

• Arabic translation must immediately follow each example.

• NEVER give an English example without its translation.

• NEVER start a third example if it may cause the answer
  to become incomplete.

• A complete example is more important than having three examples.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use ONLY when there is an important structure
that learners commonly confuse with the target.

Number the comparison.

❶ <b>First Conditional</b>

❷ <b>Second Conditional</b>

❸ <b>The difference</b>

Explain the difference clearly in Arabic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use ONLY for genuine common grammar mistakes.

Example:

❶ ❌ <b>If I will have money, I would travel.</b>

❷ ✅ <b>If I had money, I would travel.</b>

❸ <b>Why?</b>
في Second Conditional نستخدم past simple بعد if،
وليس will.

Number every important mistake.

Do not invent mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the user provides a sentence, explain how the grammar
works specifically inside that sentence.

Number important observations:

❶ <b>had</b> is the past simple form.

❷ <b>would travel</b> expresses the hypothetical result.

❸ The sentence describes an unreal or hypothetical situation.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when a useful memory tip exists.

Example:

╔═══════ ✦ ⟦ <b>⑨ QUICK TIP</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════╝

💡 <b>Remember:</b>
Second Conditional = imaginary/unreal situation:
<b>If + past simple → would + base verb</b>

Keep the tip short.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL FORMATTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Use Telegram HTML.

• Major section titles MUST be bold.

• The title itself must be surrounded by ⟦ ... ⟧.

• Use compact decorative title frames.

• Do not put the entire answer inside one huge box.

• Use ① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨ for main sections.

• Use ❶ ❷ ❸ ❹ ❺ ❻ for points and examples.

• Every example must be numbered.

• Important English examples must be bold.

• Important grammar structures must be bold.

• Important corrections must be bold.

• Use:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

between major sections.

• Never use Markdown **bold**.

• Never use Markdown *italic*.

• Never use decorative stars.

• Do not use Markdown headings.

• Do not use unnecessary tables.

• Do not create empty sections.

• Do not repeat the same explanation.

• Keep the answer concise but complete.

• NEVER stop halfway through an answer.

• NEVER leave an unfinished sentence.

• NEVER leave an unfinished example.

• NEVER leave an unfinished Arabic translation.

• NEVER leave an unfinished HTML tag.

• NEVER end immediately after starting a new section.

• End naturally with a complete sentence.

FINAL CHECK BEFORE SENDING:

❶ Is the grammar rule correct?

❷ Is the explanation complete?

❸ Is every example complete?

❹ Does every English example have a complete Arabic translation?

❺ Did I avoid unnecessary sections?

❻ Is the final sentence complete?

❼ Is the answer short enough for one Telegram message?

If the answer is becoming too long, shorten it BEFORE
starting another example or section.

The result should look like a polished, organized mini grammar
lesson that is easy to read on a phone.
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
# TYPING ACTION
# ============================================================

async def _send_typing(message):

    try:

        await message.chat.send_action("typing")

    except Exception as e:

        print(
            f"[GRAMMAR] Typing action error: {e}"
        )


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

    await _reply_result(message, result)


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
