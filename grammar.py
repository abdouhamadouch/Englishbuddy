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

    text = text.strip()

    if len(text) > 3000:
        text = text[:3000]

    return text


def _escape_problematic_html(text: str) -> str:
    """
    Telegram HTML can fail if the AI accidentally produces
    unsupported tags. We only remove obvious unsupported tags.
    We keep <b> because it is intentionally used.
    """
    if not text:
        return text

    # Remove markdown emphasis if AI accidentally uses it.
    text = text.replace("**", "")

    # Remove common markdown code fences.
    text = text.replace("```html", "")
    text = text.replace("```", "")

    # Remove unsupported HTML tags while keeping <b>.
    text = re.sub(
        r"</?(?!b\b)[a-zA-Z][^>]*>",
        "",
        text,
    )

    return text.strip()


async def _reply_result(message, result: str):
    """
    Send the grammar result safely.
    """
    result = (result or "").strip()

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation."
        )
        return

    result = _escape_problematic_html(result)

    try:
        await message.reply_text(
            result,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        # Safe fallback if malformed HTML remains.
        plain = re.sub(r"</?b>", "", result)

        await message.reply_text(
            plain,
            disable_web_page_preview=True,
        )


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

Analyze the following user input:

USER INPUT:
{text}

Your job is to identify the genuinely relevant grammar and TEACH it clearly.

IMPORTANT:
- Do not give a useless one-line answer.
- Do not automatically treat every input as an error.
- If the sentence is correct, explain the grammar rule that makes it correct.
- If the sentence is incorrect, explain the real grammatical problem and why it is wrong.
- If the user gives a grammar rule/name such as "Present Perfect", teach that rule directly.
- If the user gives a word, explain the important grammatical patterns and constructions associated with that word.
- If the user gives a phrase, explain the grammar contained in the phrase.
- Do not invent grammar problems.
- Do not discuss grammar that is unrelated to the user's input.
- Focus on useful English grammar for an A2-B1 learner, but explain more advanced grammar when the input requires it.

TEACHING STYLE:
- Explain mainly in clear Arabic.
- Keep English grammar terms in English when useful.
- Make the explanation easy to understand.
- Give practical examples.
- Explain WHY the structure is used, not only WHAT it is.
- Do not overload the answer with unnecessary theory.

ORGANIZATION:

Use only the sections that are actually useful for this particular input.

Possible sections:

<b>Grammar Point</b>
Give the name of the main grammar rule.

━━━━━━━━━━━━━━━━━━

<b>What is it?</b>
Explain the rule simply.

<b>Structure</b>
Show the grammatical structure clearly.

<b>When do we use it?</b>
Explain the important situations where it is used.

<b>Examples</b>
Give at least 3 useful new examples when a rule is being taught.
Each example must have an Arabic translation.

<b>Compare</b>
Use this only when there is an important similar structure that learners commonly confuse with the target rule.

<b>Common Mistakes</b>
Use this only when there are genuine common mistakes related to the target grammar.
Explain:
- the incorrect form
- the correct form
- why it is wrong
- additional examples

<b>In this sentence</b>
When the user gives a sentence, explain exactly how the grammar rule works inside that sentence.

Do NOT create empty sections.

FORMATTING:
- Use Telegram HTML.
- Use <b>...</b> for important words, grammar names and key points.
- NEVER use Markdown **bold**.
- NEVER use Markdown *italic*.
- Do not use decorative stars.
- Use "•" for bullet points.
- Use "━━━━━━━━━━━━━━━━━━" between major sections.
- Keep the answer clean and easy to read on a phone.
- Do not put everything in one huge paragraph.
- Do not use tables unless a very small comparison genuinely requires one.
- For grammatical structures, prefer simple notation such as:
  Subject + have/has + past participle
  Subject + be + past participle
  If + past simple, would + base verb

EXAMPLES:
Every important example should be written in English followed by its Arabic translation.

Example:
<b>She has lived here for five years.</b>
هي تعيش هنا منذ خمس سنوات.

IMPORTANT:
The answer must feel like a short grammar lesson connected directly to the user's input.
Do not merely say "correct" or "incorrect" and stop.
"""


# =========================================================
# AI
# =========================================================

async def _ask_ai(prompt: str):
    """
    Ask the injected AI function.
    """
    if _ai_function is None:
        return None

    try:
        result = await _ai_function(prompt)

        if not result:
            return None

        return str(result).strip()

    except Exception as e:
        print(f"[GRAMMAR] AI error: {e}")
        return None


async def analyze_grammar(text: str):
    """
    Analyze grammar using AI.
    """
    text = _clean_input(text)

    if not text:
        return None

    prompt = build_grammar_prompt(text)

    result = await _ask_ai(prompt)

    return result


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
    /grammar

    Or reply to a message:
    /grammar
    """

    message = update.effective_message

    if not message:
        return

    text = ""

    # First: command arguments
    if context.args:
        text = " ".join(context.args).strip()

    # Second: replied message
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

    await message.chat.send_action("typing")

    result = await analyze_grammar(text)

    if not result:
        await message.reply_text(
            "I couldn't generate the grammar explanation. "
            "Please try again."
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

    Also accepts:
    قواعد present perfect
    جرامر I have been studying.
    قواعد passive voice

    And can be used as a reply.
    """

    message = update.effective_message

    if not message or not message.text:
        return

    full_text = message.text.strip()

    # Remove the command itself.
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

    await message.chat.send_action("typing")

    result = await analyze_grammar(text)

    if not result:
        await message.reply_text(
            "تعذر إنشاء شرح القاعدة. حاول مرة أخرى."
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

    # English commands
    application.add_handler(
        CommandHandler(
            ["grammar", "gram"],
            grammar_command,
        )
    )

    # Arabic:
    # قواعد
    # جرامر
    # قواعد present perfect
    # جرامر passive voice
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
