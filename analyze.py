# analyze.py
# FixMyEnglish - Word Analysis
#
# Design:
# - /analysis and /analys
# - Main analysis appears first.
# - Each user has an independent 20-minute session.
# - Callback data is tied to the session:
#       analysis:{session_id}:{action}
# - Main buttons EDIT the original analysis message keyboard.
# - Submenu buttons EDIT the original analysis message keyboard.
# - Only final choices send a NEW result message.
# - After a final result, the original message returns to the main buttons.
# - Old/expired callbacks are rejected safely.
# - Root belongs to Deep Analysis.
# - Synonyms / Antonyms / Word Levels belong to Relations.
#
# Expected configure() interface:
# configure(ask_groq_func, get_target_text_func, is_approved_func)

import asyncio
import html
import inspect
import json
import re
import time
import uuid
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode


# ============================================================
# CONFIG
# ============================================================

SESSION_TTL = 20 * 60

EXPIRED_TEXT = (
    "⏳ This analysis session has expired.\n"
    "Please use /analysis again to start a new session."
)

NO_INFO = "No reliable information was found for this section."

MAX_SESSION_COUNT = 500


# ============================================================
# INJECTED FUNCTIONS FROM bot.py
# ============================================================

_ask_groq = None
_get_target_text = None
_is_approved = None

_sessions = {}


def configure(
    ask_groq_func=None,
    get_target_text_func=None,
    is_approved_func=None,
):
    """
    Called from bot.py.

    Example:
        analyze.configure(
            ask_groq,
            get_target_text,
            is_approved,
        )
    """
    global _ask_groq
    global _get_target_text
    global _is_approved

    _ask_groq = ask_groq_func
    _get_target_text = get_target_text_func
    _is_approved = is_approved_func


# ============================================================
# GENERIC HELPERS
# ============================================================

def _now():
    return time.time()


def _clean_spaces(text):
    if not text:
        return ""

    text = str(text)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _unique(items):
    result = []
    seen = set()

    for item in items or []:
        if item is None:
            continue

        item = str(item).strip()

        if not item:
            continue

        key = item.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def _strip_markdown(text):
    """
    Prevent Markdown such as **word** or ```...``` from appearing
    literally in Telegram.

    We use HTML output ourselves.
    """
    if not text:
        return ""

    text = str(text)

    text = re.sub(r"```(?:\w+)?", "", text)
    text = text.replace("```", "")

    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)

    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)

    return text.strip()


def _html(text):
    return html.escape(str(text or ""), quote=False)


def _safe_word(word):
    word = str(word or "").strip()
    word = re.sub(r"\s+", " ", word)

    if len(word) > 80:
        word = word[:80]

    return word


def _normalize_word(word):
    return _safe_word(word).lower()


def _is_word_like(text):
    if not text:
        return False

    text = text.strip()

    if len(text) > 80:
        return False

    return bool(re.fullmatch(r"[A-Za-z][A-Za-z' -]*", text))


# ============================================================
# APPROVAL
# ============================================================

async def _call_approved(update):
    if not _is_approved:
        return True

    user = update.effective_user

    if not user:
        return False

    try:
        result = _is_approved(user.id)

        if inspect.isawaitable(result):
            result = await result

        return bool(result)

    except Exception:
        return False


# ============================================================
# TARGET WORD
# ============================================================

async def _get_word_from_update(update, context):
    """
    Supports:
    /analysis word

    and, if bot.py provides a target extractor,
    reply-based analysis.
    """

    message = update.effective_message

    if not message:
        return ""

    # First try injected target extractor.
    if _get_target_text:
        try:
            result = _get_target_text(message)

            if inspect.isawaitable(result):
                result = await result

            if result:
                result = _safe_word(result)

                if result:
                    return result

        except Exception:
            pass

    # /analysis word
    text = message.text or ""

    parts = text.split(maxsplit=1)

    if len(parts) == 2:
        target = _safe_word(parts[1])

        if target:
            return target

    # Reply to a message.
    reply = message.reply_to_message

    if reply:
        reply_text = (
            reply.text
            or reply.caption
            or ""
        ).strip()

        if _is_word_like(reply_text):
            return reply_text

        if len(reply_text.split()) <= 5:
            first = reply_text.strip(
                ".,!?;:\"'()[]{}"
            )

            if _is_word_like(first):
                return first

    return ""


# ============================================================
# HTTP
# ============================================================

async def _http_get(url, timeout=8):
    def _request():
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "FixMyEnglish/1.0 "
                    "(English learning Telegram bot)"
                )
            },
        )

        with urlopen(req, timeout=timeout) as response:
            return response.read().decode(
                "utf-8",
                errors="ignore",
            )

    try:
        return await asyncio.to_thread(_request)
    except Exception:
        return ""


async def _http_json(url, timeout=8):
    raw = await _http_get(url, timeout)

    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


# ============================================================
# DICTIONARY API
# ============================================================

async def _dictionary_api(word):
    word = _normalize_word(word)

    if not word:
        return {}

    url = (
        "https://api.dictionaryapi.dev/api/v2/entries/en/"
        + quote(word)
    )

    data = await _http_json(url, timeout=8)

    if not isinstance(data, list) or not data:
        return {}

    entry = data[0]

    if not isinstance(entry, dict):
        return {}

    meanings = []

    for meaning in entry.get("meanings", []) or []:
        if not isinstance(meaning, dict):
            continue

        pos = str(
            meaning.get("partOfSpeech") or ""
        ).strip()

        definitions = []

        for item in meaning.get("definitions", []) or []:
            if not isinstance(item, dict):
                continue

            definition = str(
                item.get("definition") or ""
            ).strip()

            example = str(
                item.get("example") or ""
            ).strip()

            synonyms = item.get("synonyms") or []
            antonyms = item.get("antonyms") or []

            definitions.append(
                {
                    "definition": definition,
                    "example": example,
                    "synonyms": _unique(synonyms),
                    "antonyms": _unique(antonyms),
                }
            )

        if definitions:
            meanings.append(
                {
                    "part_of_speech": pos,
                    "definitions": definitions,
                }
            )

    phonetics = []

    for item in entry.get("phonetics", []) or []:
        if not isinstance(item, dict):
            continue

        text = str(item.get("text") or "").strip()
        audio = str(item.get("audio") or "").strip()

        if text or audio:
            phonetics.append(
                {
                    "text": text,
                    "audio": audio,
                }
            )

    return {
        "word": str(entry.get("word") or word).strip(),
        "phonetic": str(entry.get("phonetic") or "").strip(),
        "phonetics": phonetics,
        "meanings": meanings,
        "source": "Dictionary API",
    }


# ============================================================
# DATAMUSE
# ============================================================

async def _datamuse(word, relation):
    word = _normalize_word(word)

    if not word:
        return []

    params = {
        relation: word,
        "max": "30",
    }

    url = "https://api.datamuse.com/words?" + urlencode(params)
    data = await _http_json(url, timeout=8)

    if not isinstance(data, list):
        return []

    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        value = str(item.get("word") or "").strip()
        if value:
            result.append(value)

    return _unique(result)


# ============================================================
# WORDNET
# ============================================================

def _wordnet_data(word):
    try:
        from nltk.corpus import wordnet as wn
    except Exception:
        return {
            "synonyms": [],
            "antonyms": [],
            "hypernyms": [],
            "hyponyms": [],
            "lemmas": [],
        }

    try:
        synsets = wn.synsets(word)
    except Exception:
        return {
            "synonyms": [],
            "antonyms": [],
            "hypernyms": [],
            "hyponyms": [],
            "lemmas": [],
        }

    synonyms = []
    antonyms = []
    hypernyms = []
    hyponyms = []
    lemmas = []

    for synset in synsets:
        for lemma in synset.lemmas():
            name = lemma.name().replace("_", " ")

            if name:
                lemmas.append(name)

                for ant in lemma.antonyms():
                    ant_name = ant.name().replace("_", " ")
                    if ant_name:
                        antonyms.append(ant_name)

        for hyper in synset.hypernyms():
            for lemma in hyper.lemmas():
                hypernyms.append(lemma.name().replace("_", " "))

        for hypo in synset.hyponyms():
            for lemma in hypo.lemmas():
                hyponyms.append(lemma.name().replace("_", " "))

    synonyms = _unique(lemmas)
    target = word.lower()
    synonyms = [x for x in synonyms if x.lower() != target]

    return {
        "synonyms": synonyms,
        "antonyms": _unique(antonyms),
        "hypernyms": _unique(hypernyms),
        "hyponyms": _unique(hyponyms),
        "lemmas": synonyms,
    }


# ============================================================
# WIKTIONARY
# ============================================================

async def _wiktionary_extract(word):
    word = _normalize_word(word)

    if not word:
        return ""

    url = (
        "https://en.wiktionary.org/w/api.php?"
        + urlencode(
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "redirects": "1",
                "titles": word,
                "format": "json",
            }
        )
    )

    data = await _http_json(url, timeout=10)

    if not isinstance(data, dict):
        return ""

    query = data.get("query") or {}
    pages = query.get("pages") or {}

    for page in pages.values():
        if not isinstance(page, dict):
            continue

        extract = str(page.get("extract") or "").strip()
        if extract:
            return extract[:12000]

    return ""


# ============================================================
# SOURCE DATA
# ============================================================

async def _load_source_data(word):
    dictionary_task = asyncio.create_task(_dictionary_api(word))
    synonym_task = asyncio.create_task(_datamuse(word, "rel_syn"))
    antonym_task = asyncio.create_task(_datamuse(word, "rel_ant"))
    similar_task = asyncio.create_task(_datamuse(word, "sp"))
    sounds_task = asyncio.create_task(_datamuse(word, "sl"))
    wiktionary_task = asyncio.create_task(_wiktionary_extract(word))

    try: dictionary = await dictionary_task
    except Exception: dictionary = {}

    try: datamuse_synonyms = await synonym_task
    except Exception: datamuse_synonyms = []

    try: datamuse_antonyms = await antonym_task
    except Exception: datamuse_antonyms = []

    try: similar_spelling = await similar_task
    except Exception: similar_spelling = []

    try: sound_alikes = await sounds_task
    except Exception: sound_alikes = []

    try: wiktionary = await wiktionary_task
    except Exception: wiktionary = ""

    wordnet = await asyncio.to_thread(_wordnet_data, word)

    return {
        "dictionary": dictionary,
        "datamuse_synonyms": datamuse_synonyms,
        "datamuse_antonyms": datamuse_antonyms,
        "similar_spelling": similar_spelling,
        "sound_alikes": sound_alikes,
        "wiktionary": wiktionary,
        "wordnet": wordnet,
    }


# ============================================================
# GROQ
# ============================================================

async def _groq(prompt, max_tokens=500):
    if not _ask_groq:
        return ""

    system_prompt = (
        "You are an accurate English-learning assistant. "
        "Use established English knowledge only. "
        "Never invent facts. "
        "Never invent CEFR levels, etymology, slang, "
        "homophones, pronunciation, idioms, or word history. "
        "If information is uncertain, omit it. "
        "Return clean plain text only. "
        "Do not use Markdown. "
        "Do not use asterisks. "
        "Do not use Markdown tables."
    )

    try:
        result = _ask_groq(
            prompt,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=18)

        if result is None:
            return ""

        result = str(result).strip()

        if not result:
            return ""

        lowered = result.lower()

        bad_responses = {
            "empty ai response",
            "❌ empty ai response.",
            "error",
            "none",
            "null",
            "no response",
        }

        if lowered in bad_responses:
            return ""

        return _strip_markdown(result)

    except asyncio.TimeoutError:
        return ""
    except Exception:
        return ""


async def _groq_json(prompt, max_tokens=700):
    raw = await _groq(
        prompt + "\n\nReturn ONLY valid JSON. No Markdown and no code fences.",
        max_tokens=max_tokens,
    )

    if not raw:
        return None

    raw = raw.strip()

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


# ============================================================
# MAIN ANALYSIS
# ============================================================

def _dictionary_summary(dictionary):
    if not dictionary:
        return ""

    lines = []

    for meaning in dictionary.get("meanings", [])[:5]:
        pos = meaning.get("part_of_speech", "").strip()

        for definition in meaning.get("definitions", [])[:2]:
            text = definition.get("definition", "").strip()

            if not text:
                continue

            if pos:
                lines.append(f"{pos}: {text}")
            else:
                lines.append(text)

    return "\n".join(lines[:8])


async def _build_main_analysis(word, data):
    dictionary = data.get("dictionary") or {}
    dictionary_text = _dictionary_summary(dictionary)
    wiki = data.get("wiktionary") or ""
    wiki_excerpt = wiki[:5000]

    prompt = f"""
Analyze the English word "{word}" for an English learner.

Use the source information below as evidence.

Dictionary information:
{dictionary_text or "No dictionary definition available."}

Wiktionary information:
{wiki_excerpt or "No Wiktionary information available."}

Write a concise but useful natural analysis of about 6–8 lines.

Include, when reliably known:
1. Part of speech.
2. Main meaning or meanings.
3. Typical real-life usage/context.
4. Register: neutral, formal, informal, or slang only if reliable.
5. Important usage/collocation information.
6. A useful learner note.
7. Arabic meaning.

Do NOT list synonyms or antonyms here.
Do NOT invent a CEFR level.
Do NOT invent etymology.
Do NOT use headings, bullets, stars, Markdown, or tables.
Use plain text.
"""

    result = await _groq(prompt, max_tokens=500)

    if result:
        return _html(result)

    # Deterministic fallback
    lines = []
    if dictionary.get("meanings"):
        first_meanings = dictionary["meanings"][:3]
        pos_parts = []
        for meaning in first_meanings:
            pos = meaning.get("part_of_speech", "").strip()
            if pos:
                pos_parts.append(pos)
        pos_parts = _unique(pos_parts)
        if pos_parts:
            lines.append(f"<b>Part of speech:</b> {_html(', '.join(pos_parts))}")

        definitions = []
        for meaning in first_meanings:
            for item in meaning.get("definitions", [])[:2]:
                definition = item.get("definition", "").strip()
                if definition:
                    definitions.append(definition)
        definitions = _unique(definitions)
        if definitions:
            lines.append("<b>Meaning:</b> " + _html("; ".join(definitions[:3])))

        examples = []
        for meaning in first_meanings:
            for item in meaning.get("definitions", []):
                example = item.get("example", "").strip()
                if example:
                    examples.append(example)
        if examples:
            lines.append("<b>Usage:</b> " + _html(examples[0]))

    if not lines:
        return (
            "<b>Word Analysis</b>\n"
            "No reliable dictionary information was available for this word."
        )

    lines.append("<b>Arabic:</b> لم تتوفر ترجمة عربية موثوقة من المصدر.")
    return "\n".join(lines)


# ============================================================
# SESSION
# ============================================================

def _cleanup_sessions():
    current = _now()
    expired = []

    for session_id, session in list(_sessions.items()):
        if current - session.get("created_at", current) > SESSION_TTL:
            expired.append(session_id)

    for session_id in expired:
        _sessions.pop(session_id, None)

    if len(_sessions) > MAX_SESSION_COUNT:
        ordered = sorted(_sessions.items(), key=lambda item: item[1].get("created_at", 0))
        excess = len(_sessions) - MAX_SESSION_COUNT
        for session_id, _ in ordered[:excess]:
            _sessions.pop(session_id, None)


def _create_session(user_id, chat_id, word, data):
    _cleanup_sessions()
    session_id = uuid.uuid4().hex[:12]

    session = {
        "session_id": session_id,
        "user_id": int(user_id),
        "chat_id": int(chat_id),
        "word": word,
        "created_at": _now(),
        "data": data,
        "main_message_id": None,
    }

    # New analysis invalidates old ones for this user
    for old_id, old_session in list(_sessions.items()):
        if old_session.get("user_id") == int(user_id):
            _sessions.pop(old_id, None)

    _sessions[session_id] = session
    return session


def _get_session(session_id):
    if not session_id:
        return None

    session = _sessions.get(session_id)
    if not session:
        return None

    if _now() - session.get("created_at", 0) > SESSION_TTL:
        _sessions.pop(session_id, None)
        return None

    return session


# ============================================================
# CALLBACK DATA
# ============================================================

def _callback(session_id, action):
    return f"analysis:{session_id}:{action}"


# ============================================================
# MAIN KEYBOARD
# ============================================================

def _main_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📖 Meaning & Usage", callback_data=_callback(session_id, "meaning")),
                InlineKeyboardButton("🔗 Relations", callback_data=_callback(session_id, "relations")),
            ],
            [
                InlineKeyboardButton("🔬 Deep Analysis", callback_data=_callback(session_id, "deep")),
                InlineKeyboardButton("💬 Expressions", callback_data=_callback(session_id, "expressions")),
            ],
            [
                InlineKeyboardButton("🗣️ Slang & Phrasal", callback_data=_callback(session_id, "slang")),
                InlineKeyboardButton("🔊 Pronunciation", callback_data=_callback(session_id, "pronunciation")),
            ],
        ]
    )


# ============================================================
# SUBMENUS
# ============================================================

def _meaning_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Meanings", callback_data=_callback(session_id, "meaning_meanings")),
                InlineKeyboardButton("📝 Usage", callback_data=_callback(session_id, "meaning_usage")),
            ],
            [
                InlineKeyboardButton("🔗 Collocations", callback_data=_callback(session_id, "meaning_collocations")),
                InlineKeyboardButton("🎚 Register", callback_data=_callback(session_id, "meaning_register")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )

def _relations_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Synonyms", callback_data=_callback(session_id, "synonyms")),
                InlineKeyboardButton("↔️ Antonyms", callback_data=_callback(session_id, "antonyms")),
            ],
            [
                InlineKeyboardButton("🔊 Homophones", callback_data=_callback(session_id, "homophones")),
                InlineKeyboardButton("✍️ Similar Spelling", callback_data=_callback(session_id, "spelling")),
            ],
            [
                InlineKeyboardButton("🌳 Word Family", callback_data=_callback(session_id, "family")),
                InlineKeyboardButton("📊 Word Levels", callback_data=_callback(session_id, "levels")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )

def _deep_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌱 Root & Etymology", callback_data=_callback(session_id, "root")),
                InlineKeyboardButton("🧩 Word Formation", callback_data=_callback(session_id, "formation")),
            ],
            [
                InlineKeyboardButton("🧠 Semantic Analysis", callback_data=_callback(session_id, "semantic")),
                InlineKeyboardButton("⚠️ Learner Notes", callback_data=_callback(session_id, "learner_notes")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )

def _expressions_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 Idioms", callback_data=_callback(session_id, "idioms")),
                InlineKeyboardButton("🧱 Fixed Phrases", callback_data=_callback(session_id, "fixed_phrases")),
            ],
            [
                InlineKeyboardButton("🔗 Common Collocations", callback_data=_callback(session_id, "expression_collocations")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )

def _slang_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗣️ Slang", callback_data=_callback(session_id, "slang_words")),
                InlineKeyboardButton("🔀 Phrasal Verbs", callback_data=_callback(session_id, "phrasal")),
            ],
            [
                InlineKeyboardButton("💬 Informal Uses", callback_data=_callback(session_id, "informal")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )

def _pronunciation_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇺🇸 US Pronunciation", callback_data=_callback(session_id, "pron_us")),
                InlineKeyboardButton("🇬🇧 UK Pronunciation", callback_data=_callback(session_id, "pron_uk")),
            ],
            [
                InlineKeyboardButton("🎯 Stress", callback_data=_callback(session_id, "stress")),
                InlineKeyboardButton("🗣️ Tips", callback_data=_callback(session_id, "pron_tips")),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back")),
            ],
        ]
    )


# ============================================================
# ARABIC GLOSSES
# ============================================================

async def _arabic_glosses(word, words):
    words = _unique(words)[:15]
    if not words:
        return {}

    prompt = f"""
For the English word "{word}", give a short Arabic meaning/gloss
for each of these related English words.

Words:
{json.dumps(words, ensure_ascii=False)}

Return JSON only in this exact structure:
{{
  "items": [
    {{
      "word": "English word",
      "arabic": "Arabic meaning"
    }}
  ]
}}

Use common Arabic meanings appropriate for English learners.
Do not invent unusual meanings.
"""

    data = await _groq_json(prompt, max_tokens=600)
    result = {}

    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue

                en = str(item.get("word") or "").strip()
                ar = str(item.get("arabic") or "").strip()

                if en and ar:
                    result[en.lower()] = ar

    return result


def _list_with_arabic(words, arabic_map):
    lines = []
    for word in _unique(words)[:15]:
        arabic = arabic_map.get(word.lower(), "")
        if arabic:
            lines.append(f"• <b>{_html(word)}</b> — {_html(arabic)}")
        else:
            lines.append(f"• <b>{_html(word)}</b>")
    return lines


# ============================================================
# SYNONYMS
# ============================================================

async def _synonyms_result(word, data):
    datamuse = data.get("datamuse_synonyms", [])
    wordnet = (data.get("wordnet") or {}).get("synonyms", [])
    dictionary_synonyms = []
    dictionary = data.get("dictionary") or {}

    for meaning in dictionary.get("meanings", []):
        for definition in meaning.get("definitions", []):
            dictionary_synonyms.extend(definition.get("synonyms", []))

    words = _unique(datamuse + wordnet + dictionary_synonyms)
    words = [x for x in words if x.lower() != word.lower()][:15]

    if not words:
        return (
            "<b>🔄 Synonyms</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"No reliable synonyms were found for <b>{_html(word)}</b>."
        )

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return (
        f"<b>🔄 Synonyms — {_html(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
        + "\n\n📚 Sources: WordNet / Datamuse / Dictionary API"
    )


# ============================================================
# ANTONYMS
# ============================================================

async def _antonyms_result(word, data):
    datamuse = data.get("datamuse_antonyms", [])
    wordnet = (data.get("wordnet") or {}).get("antonyms", [])
    dictionary_antonyms = []
    dictionary = data.get("dictionary") or {}

    for meaning in dictionary.get("meanings", []):
        for definition in meaning.get("definitions", []):
            dictionary_antonyms.extend(definition.get("antonyms", []))

    words = _unique(datamuse + wordnet + dictionary_antonyms)
    words = [x for x in words if x.lower() != word.lower()][:15]

    if not words:
        return (
            "<b>↔️ Antonyms</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"No reliable antonyms were found for <b>{_html(word)}</b>."
        )

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return (
        f"<b>↔️ Antonyms — {_html(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
        + "\n\n📚 Sources: WordNet / Datamuse / Dictionary API"
    )


# ============================================================
# GENERIC AI SECTIONS
# ============================================================

async def _ai_section(word, title, instruction, data, max_tokens=550):
    dictionary = data.get("dictionary") or {}
    dictionary_text = _dictionary_summary(dictionary)

    prompt = f"""
English word: "{word}"

Dictionary information:
{dictionary_text or "No dictionary data available."}

Task:
{instruction}

Give useful information specifically about "{word}".

Rules:
- Use established English knowledge.
- Do not invent information.
- If a point is not reliably known, omit it.
- Use concise English explanations.
- Give Arabic meanings/translations where appropriate.
- Use plain text only.
- No Markdown.
- No asterisks.
- No tables.
"""

    result = await _groq(prompt, max_tokens=max_tokens)
    if result:
        return f"<b>{title}</b>\n━━━━━━━━━━━━━━━━━━\n" + _html(result)

    return f"<b>{title}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"


# ============================================================
# MEANING RESULTS
# ============================================================

async def _meaning_result(word, data):
    dictionary = data.get("dictionary") or {}
    lines = [f"<b>📚 Meanings — {_html(word)}</b>", "━━━━━━━━━━━━━━━━━━"]
    number = 1

    for meaning in dictionary.get("meanings", [])[:5]:
        pos = str(meaning.get("part_of_speech") or "").strip()
        if pos:
            lines.append(f"<b>{number}. {_html(pos)}</b>")
        else:
            lines.append(f"<b>{number}.</b>")

        for definition in meaning.get("definitions", [])[:3]:
            text = definition.get("definition", "").strip()
            if text:
                lines.append(f"• {_html(text)}")
        number += 1

    if len(lines) <= 2:
        return f"<b>📚 Meanings — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"

    return "\n".join(lines)


# ============================================================
# RELATIONS
# ============================================================

async def _homophones_result(word, data):
    words = data.get("sound_alikes", [])
    words = [x for x in _unique(words) if x.lower() != word.lower()][:15]

    if not words:
        return f"<b>🔊 Homophones — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable homophones were found."

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return (
        f"<b>🔊 Homophones — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n\n📚 Source: Datamuse"
    )

async def _spelling_result(word, data):
    words = data.get("similar_spelling", [])
    words = [x for x in _unique(words) if x.lower() != word.lower()][:15]

    if not words:
        return f"<b>✍️ Similar Spelling — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable similar-spelling words were found."

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return (
        f"<b>✍️ Similar Spelling — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n\n📚 Source: Datamuse"
    )


# ============================================================
# WORD FAMILY
# ============================================================

async def _family_result(word, data):
    wordnet = data.get("wordnet") or {}
    source_words = _unique(wordnet.get("lemmas", []) + wordnet.get("synonyms", []))
    source_words = [x for x in source_words if x.lower() != word.lower()][:30]

    prompt = f"""
For the English word "{word}", identify its genuine English word family.

Use only established forms.
Do not include synonyms merely because they have a related meaning.
Do not invent forms.

Possible source words:
{json.dumps(source_words, ensure_ascii=False)}

Return JSON only:
{{
  "items": [
    {{
      "word": "form",
      "part_of_speech": "noun/verb/adjective/adverb",
      "arabic": "Arabic meaning"
    }}
  ]
}}
"""
    data_json = await _groq_json(prompt, max_tokens=600)
    items = []

    if isinstance(data_json, dict):
        raw_items = data_json.get("items")
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue

                form = str(item.get("word") or "").strip()
                pos = str(item.get("part_of_speech") or "").strip()
                arabic = str(item.get("arabic") or "").strip()

                if form and (form.lower() != word.lower()):
                    items.append((form, pos, arabic))

    if not items:
        return f"<b>🌳 Word Family — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"

    lines = []
    for form, pos, arabic in items[:15]:
        line = f"• <b>{_html(form)}</b>"
        if pos: line += f" — {_html(pos)}"
        if arabic: line += f" — {_html(arabic)}"
        lines.append(line)

    return f"<b>🌳 Word Family — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)


# ============================================================
# WORD LEVELS
# ============================================================

async def _levels_result(word, data):
    dictionary = data.get("dictionary") or {}
    dictionary_text = _dictionary_summary(dictionary)

    prompt = f"""
Estimate the CEFR level of the English word "{word}".

Dictionary evidence:
{dictionary_text or "No dictionary evidence."}

Return JSON only:
{{
  "level": "A1/A2/B1/B2/C1/C2/unknown",
  "confidence": "high/medium/low",
  "note": "short explanation"
}}
"""
    result = await _groq_json(prompt, max_tokens=300)

    if not isinstance(result, dict):
        return f"<b>📊 Word Level — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable CEFR level was found."

    level = str(result.get("level") or "").strip()
    confidence = str(result.get("confidence") or "").strip()
    note = str(result.get("note") or "").strip()

    if level.lower() == "unknown" or level not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
        return f"<b>📊 Word Level — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable CEFR level was found."

    lines = [
        f"<b>📊 Word Level — {_html(word)}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"• <b>CEFR:</b> {_html(level)}"
    ]
    if confidence: lines.append(f"• <b>Confidence:</b> {_html(confidence)}")
    if note: lines.append(f"• {_html(note)}")

    return "\n".join(lines)


# ============================================================
# PRONUNCIATION
# ============================================================

async def _pronunciation_result(word, data, variant="both"):
    dictionary = data.get("dictionary") or {}
    phonetic = dictionary.get("phonetic", "").strip()
    phonetics = dictionary.get("phonetics", [])
    texts = []

    for item in phonetics:
        value = str(item.get("text") or "").strip()
        if value: texts.append(value)

    texts = _unique(texts)
    prompt_variant = "American English pronunciation" if variant == "us" else "British English pronunciation" if variant == "uk" else "American and British English pronunciation"

    prompt = f"""
Give the reliable pronunciation information for the English word "{word}" in {prompt_variant}.

Dictionary phonetic information:
{phonetic or "None"}

Other phonetic entries:
{json.dumps(texts, ensure_ascii=False)}

Return JSON only:
{{
  "us": "IPA or unknown",
  "uk": "IPA or unknown",
  "stress": "short stress description",
  "note": "short pronunciation note"
}}
"""
    result = await _groq_json(prompt, max_tokens=350)
    lines = [f"<b>🔊 Pronunciation — {_html(word)}</b>", "━━━━━━━━━━━━━━━━━━"]

    if isinstance(result, dict):
        us = str(result.get("us") or "").strip()
        uk = str(result.get("uk") or "").strip()
        stress = str(result.get("stress") or "").strip()
        note = str(result.get("note") or "").strip()

        if variant in {"both", "us"} and us.lower() != "unknown" and us:
            lines.append(f"🇺🇸 <b>US:</b> {_html(us)}")
        if variant in {"both", "uk"} and uk.lower() != "unknown" and uk:
            lines.append(f"🇬🇧 <b>UK:</b> {_html(uk)}")
        if stress: lines.append(f"🎯 <b>Stress:</b> {_html(stress)}")
        if note: lines.append(f"💡 {_html(note)}")

    if len(lines) == 2:
        if phonetic: lines.append(f"• {_html(phonetic)}")
        elif texts:
            for text in texts[:2]: lines.append(f"• {_html(text)}")

    if len(lines) == 2:
        lines.append(_html(NO_INFO))

    return "\n".join(lines)


# ============================================================
# GENERIC FINAL SECTIONS
# ============================================================

async def _usage_result(word, data):
    return await _ai_section(word, "📝 Usage", "Explain how native speakers commonly use this word. Give 2–3 natural example sentences and include Arabic translations.", data, max_tokens=600)

async def _collocations_result(word, data):
    return await _ai_section(word, "🔗 Collocations", "Give the most common natural collocations with this word. Group them briefly when useful. Give Arabic meanings.", data, max_tokens=600)

async def _register_result(word, data):
    return await _ai_section(word, "🎚 Register", "Explain whether this word is neutral, formal, informal, or slang. Explain contexts and give Arabic clarification.", data, max_tokens=450)

async def _root_result(word, data):
    wiki = data.get("wiktionary") or ""
    prompt = f"Explain the reliable etymology/root of '{word}'.\nWiktionary source:\n{wiki[:7000] or 'No source available.'}\nInclude Arabic explanation. Plain text only."
    result = await _groq(prompt, max_tokens=550)
    if not result: return f"<b>🌱 Root & Etymology — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"
    return f"<b>🌱 Root & Etymology — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + _html(result)

async def _formation_result(word, data):
    return await _ai_section(word, "🧩 Word Formation", "Explain how this word is formed (prefix, suffix, root). Give Arabic explanation.", data, max_tokens=500)

async def _semantic_result(word, data):
    return await _ai_section(word, "🧠 Semantic Analysis", "Explain semantic differences between main meanings. Give short examples and Arabic clarification.", data, max_tokens=650)

async def _learner_notes_result(word, data):
    return await _ai_section(word, "⚠️ Learner Notes", "Give the most useful learner warnings (common mistakes, confusing words). Give Arabic clarification.", data, max_tokens=550)

async def _idioms_result(word, data):
    return await _ai_section(word, "💬 Idioms", "List common English idioms containing this word. Give Arabic meanings and short examples.", data, max_tokens=600)

async def _fixed_phrases_result(word, data):
    return await _ai_section(word, "🧱 Fixed Phrases", "Give common fixed phrases containing this word. Include Arabic meanings and examples.", data, max_tokens=600)

async def _expression_collocations_result(word, data):
    return await _collocations_result(word, data)

async def _slang_result(word, data):
    return await _ai_section(word, "🗣️ Slang", "Identify genuine slang meanings. Distinguish from informal. Give Arabic meanings.", data, max_tokens=550)

async def _phrasal_result(word, data):
    return await _ai_section(word, "🔀 Phrasal Verbs", "List genuine common phrasal verbs formed with this word. Give Arabic translation and examples.", data, max_tokens=600)

async def _informal_result(word, data):
    return await _ai_section(word, "💬 Informal Uses", "Explain genuine informal uses differing from neutral. Give Arabic meanings and examples.", data, max_tokens=550)

async def _stress_result(word, data):
    return await _ai_section(word, "🎯 Stress", "Explain word stress. If it changes between noun/verb, explain. Give IPA only when reliable. Include Arabic explanation.", data, max_tokens=450)

async def _pron_tips_result(word, data):
    return await _ai_section(word, "🗣️ Pronunciation Tips", "Give useful pronunciation tips (silent letters, connected speech). Include Arabic explanation.", data, max_tokens=500)


# ============================================================
# RESULT ROUTER
# ============================================================

async def _final_result(action, word, data):
    if action == "meaning_meanings": return await _meaning_result(word, data)
    if action == "meaning_usage": return await _usage_result(word, data)
    if action == "meaning_collocations": return await _collocations_result(word, data)
    if action == "meaning_register": return await _register_result(word, data)
    if action == "synonyms": return await _synonyms_result(word, data)
    if action == "antonyms": return await _antonyms_result(word, data)
    if action == "homophones": return await _homophones_result(word, data)
    if action == "spelling": return await _spelling_result(word, data)
    if action == "family": return await _family_result(word, data)
    if action == "levels": return await _levels_result(word, data)
    if action == "root": return await _root_result(word, data)
    if action == "formation": return await _formation_result(word, data)
    if action == "semantic": return await _semantic_result(word, data)
    if action == "learner_notes": return await _learner_notes_result(word, data)
    if action == "idioms": return await _idioms_result(word, data)
    if action == "fixed_phrases": return await _fixed_phrases_result(word, data)
    if action == "expression_collocations": return await _expression_collocations_result(word, data)
    if action == "slang_words": return await _slang_result(word, data)
    if action == "phrasal": return await _phrasal_result(word, data)
    if action == "informal": return await _informal_result(word, data)
    if action == "pron_us": return await _pronunciation_result(word, data, "us")
    if action == "pron_uk": return await _pronunciation_result(word, data, "uk")
    if action == "stress": return await _stress_result(word, data)
    if action == "pron_tips": return await _pron_tips_result(word, data)

    return f"<b>{_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"


# ============================================================
# ANALYSIS COMMAND
# ============================================================

async def analysis_command(update, context):
    if not await _call_approved(update):
        return

    word = await _get_word_from_update(update, context)

    if not word:
        await update.effective_message.reply_text(
            "🔎 Please provide an English word.\n\nExample:\n/analysis pleasant"
        )
        return

    word = _safe_word(word)

    if len(word.split()) > 5:
        await update.effective_message.reply_text("🔎 Please use a word or a short expression for Word Analysis.")
        return

    data = await _load_source_data(word)

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    session = _create_session(user.id, chat.id, word, data)
    session_id = session["session_id"]

    main_text = await _build_main_analysis(word, data)

    message = await update.effective_message.reply_text(
        main_text,
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(session_id),
        disable_web_page_preview=True,
    )

    session["main_message_id"] = message.message_id


# ============================================================
# CALLBACK (Fully Protected with Try/Except)
# ============================================================

async def analysis_callback(update, context):
    query = update.callback_query

    if not query:
        return

    callback_data = query.data or ""

    if not callback_data.startswith("analysis:"):
        return

    parts = callback_data.split(":", 2)

    if len(parts) != 3:
        try:
            await query.answer()
        except Exception:
            pass
        return

    _, session_id, action = parts
    session = _get_session(session_id)

    if not session:
        try:
            await query.answer(EXPIRED_TEXT, show_alert=True)
        except Exception:
            pass
        return

    user = update.effective_user

    if not user:
        try:
            await query.answer()
        except Exception:
            pass
        return

    if int(user.id) != int(session.get("user_id")):
        try:
            await query.answer("This analysis belongs to another user.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass

    # ========================================================
    # MAIN SECTION -> EDIT SAME MESSAGE (Safe Edit)
    # ========================================================

    try:
        if action == "meaning":
            await query.edit_message_reply_markup(reply_markup=_meaning_keyboard(session_id))
            return
        if action == "relations":
            await query.edit_message_reply_markup(reply_markup=_relations_keyboard(session_id))
            return
        if action == "deep":
            await query.edit_message_reply_markup(reply_markup=_deep_keyboard(session_id))
            return
        if action == "expressions":
            await query.edit_message_reply_markup(reply_markup=_expressions_keyboard(session_id))
            return
        if action == "slang":
            await query.edit_message_reply_markup(reply_markup=_slang_keyboard(session_id))
            return
        if action == "pronunciation":
            await query.edit_message_reply_markup(reply_markup=_pronunciation_keyboard(session_id))
            return
        if action == "back":
            await query.edit_message_reply_markup(reply_markup=_main_keyboard(session_id))
            return
    except Exception as e:
        print("Keyboard edit error (handled):", repr(e), flush=True)
        return

    # ========================================================
    # FINAL ACTIONS
    # ========================================================

    final_actions = {
        "meaning_meanings", "meaning_usage", "meaning_collocations", "meaning_register",
        "synonyms", "antonyms", "homophones", "spelling", "family", "levels",
        "root", "formation", "semantic", "learner_notes",
        "idioms", "fixed_phrases", "expression_collocations",
        "slang_words", "phrasal", "informal",
        "pron_us", "pron_uk", "stress", "pron_tips",
    }

    if action not in final_actions:
        return

    word = session.get("word", "")
    source_data = session.get("data") or {}

    result = await _final_result(action, word, source_data)

    if not result:
        result = f"<b>{_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n{_html(NO_INFO)}"

    # ========================================================
    # Send ONLY the final result as a NEW message
    # ========================================================

    try:
        await query.message.reply_text(
            result,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            plain = re.sub(r"<[^>]+>", "", result)
            await query.message.reply_text(plain)
        except Exception:
            pass

    # ========================================================
    # Restore original message buttons
    # ========================================================

    try:
        await query.edit_message_reply_markup(
            reply_markup=_main_keyboard(session_id)
        )
    except Exception:
        pass
