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
# - Teaches the actual grammar rule
# - Explains structure, usage, examples, comparisons and common mistakes
# - Common Mistakes appears only when genuinely relevant
# - Uses Telegram HTML formatting
# - AI function is injected from bot.py
# - Retries AI several times
# - Simple rough rectangular title frames
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

            if attempt > 1:
                current_prompt = f"""
{prompt}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IMPORTANT RETRY INSTRUCTION

This is retry attempt {attempt}.

Generate the complete grammar lesson again
from the beginning.

Make sure every sentence, example, translation,
numbered point and section is complete.

If the answer is becoming too long, shorten the
explanation or remove unnecessary sections.

Do not leave any example or translation incomplete.

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

The user wants to LEARN grammar.

Analyze this input:

USER INPUT:
{text}

Your task is to teach the relevant grammar clearly,
accurately, and practically.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGUAGE STYLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• The main explanation should be in SIMPLE, CLEAR ENGLISH.

• Use English suitable for an A2-B1 learner.

• Do NOT write the whole explanation in Arabic.

• Use Arabic only where it is genuinely useful.

• Arabic should mainly be used for:
  - translating important grammar rules
  - translating English examples
  - explaining a difficult point briefly
  - clarifying an important difference

• Do not translate every English sentence into Arabic unless
  it is an example or the translation is genuinely useful.

• Keep important grammar terms in English.

• The user is learning English, so English should remain
  the main language of the lesson.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT TEACHING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Do not give a useless one-line answer.

• Do not automatically treat every input as an error.

• If the sentence is correct, explain the grammar rule
  that makes it correct.

• If the sentence is incorrect, explain the real grammatical
  problem and why it is wrong.

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

• If the user gives:

  "2 conditional"
  "second conditional"
  "conditional 2"

  understand that the user means the Second Conditional
  and teach it directly.

• Do not ask the user to clarify an obvious grammar-rule name.

• If the user gives a word, explain the important grammatical
  patterns and constructions associated with that word.

• If the user gives a phrase, explain the grammar contained
  in the phrase.

• Do not invent grammar problems.

• Do not discuss grammar unrelated to the user's input.

• Focus on useful English grammar for an A2-B1 learner,
  but explain more advanced grammar when the input requires it.

• Explain WHY the structure is used, not only WHAT it is.

• Use practical examples.

• Examples are important.

• Give several useful examples when the grammar topic benefits
  from them.

• Do not reduce useful examples unnecessarily just to save space.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPLETE ANSWER REQUIREMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The answer must be complete.

Never stop in the middle of:

• a sentence
• an example
• a translation
• a numbered point
• a grammar rule
• a comparison
• a common mistake
• an HTML tag
• a section

Every English example must have a complete Arabic translation.

If the answer becomes too long:

• shorten the explanation
• remove repetition
• remove an unnecessary section

Do not remove useful examples unnecessarily.

Never start an example that you cannot finish completely.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TITLE DESIGN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every main section title must use ONLY a simple rectangular
frame around the title.

Use this exact visual style:

┌──────────────────────────────┐
│   <b>① GRAMMAR POINT</b>     │
└──────────────────────────────┘

The frame must be:

• simple
• slightly rough
• clean
• readable on a phone

The frame must NOT contain decorative symbols.

Do NOT use:

✦
⟦ ⟧
★
☆
╔
╗
╚
╝
████
or other decorative borders.

Do not put the entire answer inside a frame.

ONLY the title gets the frame.

The title itself must be bold.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN SECTION NUMBERING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use:

① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨

Possible sections:

① Grammar Point
② What Is It?
③ Structure
④ When Do We Use It?
⑤ Examples
⑥ Compare
⑦ Common Mistakes
⑧ In This Sentence
⑨ Quick Tip

Use ONLY the sections that are actually useful.

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

Use these for:

• uses
• rules
• examples
• mistakes
• comparisons
• important observations

Do not use unnecessary numbering for normal paragraphs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
① GRAMMAR POINT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start with the name of the grammar point.

Example:

┌──────────────────────────────┐
│   <b>① GRAMMAR POINT</b>     │
└──────────────────────────────┘

<b>Second Conditional</b>

Then give a short English explanation.

Example:

The Second Conditional is used to talk about
unreal, imaginary, or unlikely situations.

Arabic translation may be added briefly when useful:

يُستخدم للحديث عن مواقف غير حقيقية أو افتراضية.

Do not translate every sentence.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
② WHAT IS IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the grammar simply in English.

Use Arabic only when a short clarification or translation
helps the learner.

For example:

❶ We use it for an imaginary or unlikely situation.

❷ The situation is not real or is unlikely to happen.

❸ The result is also hypothetical.

Do not write the whole section in Arabic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
③ STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Show the structure clearly.

Example:

❶ <b>If + past simple, would + base verb</b>

Example:
<b>If I had more time, I would study more.</b>

Use Arabic translation only for the example:

لو كان لدي وقت أكثر، لدرست أكثر.

If positive, negative, or question forms are genuinely useful,
explain them.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
④ WHEN DO WE USE IT?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Explain the important uses in clear English.

Example:

❶ <b>Imaginary situations</b>
We imagine a situation that is not true now.

❷ <b>Unlikely situations</b>
We talk about something that is possible but not very likely.

❸ <b>Hypothetical results</b>
We describe what would happen in that situation.

Use Arabic briefly only when it improves understanding.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⑤ EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Examples are an important part of the lesson.

Give at least 3 useful examples when teaching a grammar rule.

Give more examples when there are several important uses
and the answer remains clear and complete.

Every example must be numbered.

Use this style:

❶ <b>If I had more money, I would travel more.</b>
لو كان لدي مال أكثر، لسافرت أكثر.

❷ <b>If she studied harder, she would pass the exam.</b>
لو درست بجدية أكبر، لنجحت في الامتحان.

❸ <b>If we lived near the school, we would walk there.</b>
لو كنا نعيش بالقرب من المدرسة، لذهبنا إلى هناك مشيًا.

Important:

• English examples must be bold.

• Every English example must have an Arabic translation.

• The Arabic translation should come immediately after
  the English example.

• Do NOT translate the explanation of every example.

• Translate the example itself.

• Do not start an example unless you can finish both
  the English sentence and its Arabic translation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⑥ COMPARE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use ONLY when learners commonly confuse the target grammar
with another structure.

Example:

❶ <b>First Conditional</b>
Real or possible future situations.

<b>If I study, I will pass.</b>
إذا درست، سأنجح.

❷ <b>Second Conditional</b>
Imaginary or unlikely situations.

<b>If I studied more, I would pass.</b>
لو درست أكثر، لنجحت.

❸ <b>Main difference</b>
First Conditional = real/possible.
Second Conditional = unreal/imaginary or unlikely.

Keep the comparison concise.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⑦ COMMON MISTAKES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when there are genuine common mistakes.

Example:

❶ ❌ <b>If I will have money, I would travel.</b>

❷ ✅ <b>If I had money, I would travel.</b>

❸ <b>Why?</b>
In the Second Conditional, we normally use
past simple after <b>if</b>, not <b>will</b>.

Arabic clarification may be added briefly:

في Second Conditional نستخدم past simple بعد if.

Do not write the whole section in Arabic.

Do not invent mistakes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⑧ IN THIS SENTENCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the user provides a sentence, explain how the grammar
works specifically in that sentence.

Example:

❶ <b>had</b> is the past simple form.

❷ <b>would travel</b> shows the hypothetical result.

❸ The sentence describes an unreal or hypothetical situation.

Use Arabic translation only if useful.

Do not discuss unrelated grammar.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⑨ QUICK TIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use this section ONLY when there is a genuinely useful
memory tip.

Use the same simple title frame:

┌──────────────────────────────┐
│     <b>⑨ QUICK TIP</b>       │
└──────────────────────────────┘

💡 <b>Remember:</b>
Second Conditional =
<b>If + past simple → would + base verb</b>

Arabic can be added briefly if useful.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL FORMATTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Use Telegram HTML.

• Main section titles must be bold.

• Put ONLY the title inside a simple rectangular frame.

• The frame must be simple and slightly rough.

• Do not decorate the frame.

• Do not use ✦.

• Do not use ⟦ ⟧.

• Do not use stars.

• Do not use complicated borders.

• Use ① ② ③ ④ ⑤ ⑥ ⑦ ⑧ ⑨ for main sections.

• Use ❶ ❷ ❸ ❹ ❺ ❻ for items and examples.

• Use emojis only when they improve organization or meaning.

• Do not fill the answer with emojis.

• English is the MAIN language of the lesson.

• Arabic is mainly for translations and short necessary
  clarifications.

• Do not translate the entire explanation into Arabic.

• Every English example must have an Arabic translation.

• Important English examples must be bold.

• Important grammar structures must be bold.

• Important corrections must be bold.

• Use this separator between major sections:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

• Never use Markdown **bold**.

• Never use Markdown *italic*.

• Never use decorative Markdown stars.

• Do not use Markdown headings.

• Do not use unnecessary tables.

• Do not create empty sections.

• Do not repeat the same explanation.

• Keep the answer organized and easy to read on a phone.

• Give useful examples generously.

• Every example must be complete.

• Every English example must have a complete Arabic translation.

• Never leave an unfinished sentence.

• Never leave an unfinished example.

• Never leave an unfinished Arabic translation.

• Never leave an unfinished HTML tag.

• End naturally with a complete sentence.

The final result should feel like a polished English grammar lesson:
English explanation first, Arabic translations where useful,
clear numbering, useful examples, and simple rough title frames.
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
