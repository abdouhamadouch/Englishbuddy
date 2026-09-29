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
# - Protects long answers from Telegram's message-length limit
# - Avoids incomplete / cut-off answers as much as possible
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

# Telegram messages have a practical maximum of 4096 characters.
# We keep a safety margin so formatting does not cause failures.
TELEGRAM_SAFE_LIMIT = 3900


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
    Clean AI output while keeping useful Telegram HTML.
    """

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

    # Convert Markdown bullets.
    text = re.sub(
        r"(?m)^\s*[-*+]\s+",
        "• ",
        text,
    )

    # Remove Markdown bold / italic markers.
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("*", "")

    # Normalize strong -> b.
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

    # Only allow useful Telegram HTML tags.
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
# CHECK WHETHER AI RESPONSE LOOKS INCOMPLETE
# ============================================================

def _looks_incomplete(text: str) -> bool:
    """
    Detect obvious cases where the AI stopped in the middle
    of an answer.

    This is intentionally conservative so that a normal short
    answer is not rejected unnecessarily.
    """

    if not text:
        return True

    plain = _remove_all_html(text).strip()

    if not plain:
        return True

    # Unclosed HTML tags.
    for tag in ("b", "i", "em", "u", "s", "code", "pre"):
        opening = len(
            re.findall(
                rf"<{tag}(?:\s[^>]*)?>",
                text,
                flags=re.IGNORECASE,
            )
        )

        closing = len(
            re.findall(
                rf"</{tag}>",
                text,
                flags=re.IGNORECASE,
            )
        )

        if opening != closing:
            return True

    # Obvious unfinished punctuation.
    if plain.endswith(
        (
            ":",
            ",",
            "—",
            "–",
            "...",
            "…",
            "(",
            "[",
            "{",
        )
    ):
        return True

    # Obvious unfinished English sentence.
    last_line = plain.splitlines()[-1].strip()

    if last_line:
        # These endings often indicate that the model stopped
        # before completing the sentence.
        unfinished_endings = (
            "and",
            "or",
            "but",
            "because",
            "when",
            "if",
            "that",
            "which",
            "who",
            "such as",
            "for example",
            "used to",
            "in order to",
            "rather than",
        )

        lower_last = last_line.lower()

        for ending in unfinished_endings:
            if lower_last.endswith(" " + ending):
                return True

    return False


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

            # On later attempts, explicitly tell the model
            # to return a complete answer and not stop midway.
            if attempt > 1:
                current_prompt = f"""
{prompt}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL COMPLETENESS REQUIREMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This is retry attempt {attempt}.

The previous answer may have been incomplete.

Generate the ENTIRE grammar lesson again from the beginning.

Do NOT continue from the previous answer.
Do NOT refer to a previous answer.
Do NOT stop in the middle of a section.
Do NOT leave an example unfinished.
Do NOT leave an HTML tag unfinished.

Keep the answer concise enough to fit in one Telegram message,
while still covering the genuinely important information.

The final character of the answer must be the natural end
of the final sentence.
"""

            result = await _ai_function(current_prompt)

            if result:

                result = str(result).strip()

                if result:

                    cleaned = _clean_ai_output(result)

                    if cleaned and not _looks_incomplete(cleaned):

                        print(
                            f"[GRAMMAR] Complete AI response "
                            f"received on attempt {attempt}."
                        )

                        return cleaned

                    if cleaned:
                        print(
                            f"[GRAMMAR] Response appears incomplete "
                            f"on attempt {attempt}/"
                            f"{MAX_AI_ATTEMPTS}."
                        )

            else:
                print(
                    f"[GRAMMAR] Empty AI response "
                    f"on attempt {attempt}/"
                    f"{MAX_AI_ATTEMPTS}."
                )

        except Exception as e:

            print(
                f"[GRAMMAR] AI error "
                f"on attempt {attempt}/"
                f"{MAX_AI_ATTEMPTS}: {e}"
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
VERY IMPORTANT: COMPLETE ANSWER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The answer MUST be complete.

Never stop in the middle of:

• a sentence
• an example
• a translation
• a numbered point
• a grammar rule
• a comparison
• a Common Mistake
• an HTML tag
• a section

Before finishing, mentally check that every opened idea
has been completed.

Do NOT produce a long unnecessary explanation.

Prefer a concise COMPLETE explanation over a long answer
that may get cut off.

Only include information that is genuinely useful for this
specific input.

The final section must end naturally with a complete sentence.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TITLE DESIGN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Do NOT put the title inside a huge rectangular box.

The title itself must be surrounded by a beautiful compact frame.

Use this style:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

The important title text must be inside:

⟦ <b>...</b> ⟧

The title itself MUST be bold.

Examples:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>② WHAT IS IT?</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

╔═══════ ✦ ⟦ <b>③ STRUCTURE</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

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

The exact width can be adjusted slightly according to the
title length.

The title must always look surrounded, bold and visually
separate from the explanation.

Do NOT use a giant box around all the content.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN SECTION NUMBERING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Number main sections sequentially:

① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨

Use only the sections that are actually useful.

Do NOT create empty sections.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NUMBERING INSIDE SECTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use these beautiful numbers:

❶
❷
❸
❹
❺
❻

Every natural list should be numbered.

Do not write:

Example 1:
Example 2:
Example 3:

Instead write:

❶ <b>...</b>

❷ <b>...</b>

❸ <b>...</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GRAMMAR POINT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start with the main grammar point.

Example:

╔═══════ ✦ ⟦ <b>① GRAMMAR POINT</b> ⟧ ✦ ═══════╗
╚═══════════════════════════════════════════════╝

<b>Present Perfect</b>

Give a short, clear identification of the grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the rule simply in Arabic.

Number important ideas:

❶ ...

❷ ...

❸ ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show the grammatical structure clearly.

For example:

❶ <b>Subject + have/has + past participle</b>

❷ <b>Subject + have/has not + past participle</b>

❸ <b>Have/Has + subject + past participle?</b>

Keep structures short and clear.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHEN DO WE USE IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the important uses.

Number them:

❶ <b>Experience</b>
شرح عربي واضح.

❷ <b>An unfinished situation</b>
شرح عربي واضح.

❸ <b>A recent action with a present result</b>
شرح عربي واضح.

Only include uses relevant to the target.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every example MUST be numbered.

Use:

❶ <b>She has lived here for five years.</b>
هي تعيش هنا منذ خمس سنوات.

❷ <b>I have already finished my homework.</b>
لقد أنهيت واجبي بالفعل.

❸ <b>They have never visited London.</b>
لم يزوروا لندن من قبل.

Important:
• English examples must be bold.
• Arabic translations go immediately underneath.
• Never leave an example without its translation.
• Give at least 3 useful examples when teaching a rule.
• Make examples practical for an A2-B1 learner.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use ONLY when learners genuinely confuse the target
with another grammar structure.

Number the comparison:

❶ <b>Present Perfect</b>
Example + explanation.

❷ <b>Past Simple</b>
Example + explanation.

❸ <b>The difference</b>
Explain the difference clearly in Arabic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use ONLY for genuine grammar mistakes.

Format:

❶ ❌ <b>I have saw him.</b>

❷ ✅ <b>I have seen him.</b>

❸ <b>Why?</b>
بعد have/has نستخدم التصريف الثالث.

If there are several mistakes, number each one.

Do not invent mistakes.

Do not treat stylistic preferences as grammar mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When the user gives a sentence, explain exactly how the
target grammar works inside that sentence.

Number important observations:

❶ <b>has lived</b> is the Present Perfect form.

❷ <b>for five years</b> shows the duration.

❸ The situation started in the past and continues until now.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Add this section ONLY when it gives the learner a genuinely
useful memory tip.

Use:

╔═══════ ✦ ⟦ <b>⑨ QUICK TIP</b> ⟧ ✦ ═══════╗
╚══════════════════════════════════════════╝

💡 <b>Remember:</b>
شرح قصير جدًا يساعد المتعلم على تذكر القاعدة.

Do not add a meaningless tip.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL FORMATTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Use Telegram HTML.

• Major titles MUST use <b>...</b>.

• The title itself must be surrounded by ⟦ ... ⟧.

• Use beautiful compact Unicode title frames.

• Do NOT put the entire answer inside one giant box.

• Use main numbering ① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨.

• Use internal numbering ❶ ❷ ❸ ❹ ❺ ❻.

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

• Keep the answer concise enough to remain complete.

• NEVER stop halfway through an answer.

• NEVER leave an unfinished sentence.

• NEVER leave an unfinished example.

• NEVER leave an unfinished HTML tag.

• The final answer must end naturally.

The result should look like a polished, organized mini grammar
lesson that is easy to read on a phone.
"""


# ============================================================
# SPLIT LONG TELEGRAM MESSAGES SAFELY
# ============================================================

def _split_long_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT):
    """
    Split long messages at paragraph/line boundaries whenever
    possible.

    The AI is instructed to stay concise, but this protects
    against Telegram's message-length limit if an unusually
    long response is returned.
    """

    if len(text) <= limit:
        return [text]

    chunks = []

    remaining = text.strip()

    while len(remaining) > limit:

        # Prefer paragraph boundary.
        cut = remaining.rfind("\n\n", 0, limit)

        # Otherwise prefer normal line boundary.
        if cut < int(limit * 0.55):
            cut = remaining.rfind("\n", 0, limit)

        # Otherwise prefer a sentence boundary.
        if cut < int(limit * 0.55):
            sentence_positions = [
                remaining.rfind(". ", 0, limit),
                remaining.rfind("؟ ", 0, limit),
                remaining.rfind("! ", 0, limit),
                remaining.rfind("? ", 0, limit),
            ]

            cut = max(sentence_positions)

        # Last fallback: split at a space.
        if cut < int(limit * 0.55):
            cut = remaining.rfind(" ", 0, limit)

        # Absolute fallback.
        if cut <= 0:
            cut = limit

        chunk = remaining[:cut].strip()

        if chunk:
            chunks.append(chunk)

        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


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

    chunks = _split_long_message(result)

    for index, chunk in enumerate(chunks):

        try:

            await message.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        except Exception as e:

            print(
                f"[GRAMMAR] HTML send error "
                f"for part {index + 1}: {e}"
            )

            plain = _remove_all_html(chunk)

            try:

                await message.reply_text(
                    plain,
                    disable_web_page_preview=True,
                )

            except Exception as second_error:

                print(
                    f"[GRAMMAR] Plain send error "
                    f"for part {index + 1}: {second_error}"
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
