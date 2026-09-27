# analyze.py
# FixMyEnglish - Word Analysis

import asyncio
import html
import inspect
import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode


SESSION_TTL = 20 * 60

_ask_groq = None
_get_target_text = None
_is_approved = None

# user_id -> current session
_sessions = {}


EXPIRED_TEXT = (
    "⏳ This analysis session has expired.\n"
    "Please use /analysis again to start a new session."
)


# ============================================================
# CONFIG
# ============================================================

def configure(ask_groq_func, get_target_text_func, is_approved_func):
    global _ask_groq, _get_target_text, _is_approved

    _ask_groq = ask_groq_func
    _get_target_text = get_target_text_func
    _is_approved = is_approved_func


async def _call_approved(user_id):
    if not _is_approved:
        return True

    try:
        result = _is_approved(user_id)

        if inspect.isawaitable(result):
            result = await result

        return bool(result)

    except Exception:
        return False


# ============================================================
# WORD / SESSION
# ============================================================

def _clean_word(text):
    if not text:
        return ""

    text = str(text).strip()

    text = re.sub(
        r"^/(?:analysis|analys)(?:@\w+)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = text.strip(
        " \t\r\n.,!?;:\"'“”‘’()[]{}<>"
    )

    return text[:100].strip()


def extract_word(text):
    return _clean_word(text)


def _create_session(user_id, word, data=None):
    session_id = uuid.uuid4().hex[:12]

    _sessions[user_id] = {
        "session_id": session_id,
        "word": word,
        "data": data or {},
        "created_at": time.time(),
    }

    return session_id


def _get_valid_session(user_id, session_id):
    session = _sessions.get(user_id)

    if not session:
        return None

    if session.get("session_id") != session_id:
        return None

    if time.time() - session.get("created_at", 0) > SESSION_TTL:
        _sessions.pop(user_id, None)
        return None

    return session


# ============================================================
# HTTP
# ============================================================

def _fetch_json_sync(url, timeout=12):
    request = Request(
        url,
        headers={
            "User-Agent": "FixMyEnglish/1.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=timeout) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


async def _fetch_json(url, timeout=12):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_json_sync,
                url,
                timeout,
            ),
            timeout=timeout + 2,
        )

    except Exception:
        return None


# ============================================================
# DICTIONARY
# ============================================================

async def _dictionary_data(word):
    url = (
        "https://api.dictionaryapi.dev/api/v2/entries/en/"
        + quote(word, safe="")
    )

    data = await _fetch_json(url)

    if not isinstance(data, list) or not data:
        return None

    entry = data[0]

    meanings = []

    for meaning in entry.get("meanings", []):
        pos = meaning.get("partOfSpeech", "")

        definitions = []

        for item in meaning.get("definitions", []):
            definition = item.get("definition", "")

            if definition:
                definitions.append({
                    "definition": definition,
                    "example": item.get("example", ""),
                })

        if definitions:
            meanings.append({
                "part_of_speech": pos,
                "definitions": definitions,
            })

    phonetics = []

    for item in entry.get("phonetics", []):
        text = item.get("text")
        audio = item.get("audio")

        if text or audio:
            phonetics.append({
                "text": text or "",
                "audio": audio or "",
            })

    if not meanings and not phonetics:
        return None

    return {
        "word": entry.get("word", word),
        "meanings": meanings,
        "phonetics": phonetics,
        "source": "Dictionary API",
    }


async def _get_dictionary(session):
    if session["data"].get("dictionary"):
        return session["data"]["dictionary"]

    result = await _dictionary_data(
        session["word"]
    )

    if result:
        session["data"]["dictionary"] = result

    return result


# ============================================================
# DATAMUSE
# ============================================================

async def _datamuse(word, relation, max_results=15):
    params = urlencode({
        relation: word,
        "max": max_results,
        "md": "dp",
    })

    url = (
        "https://api.datamuse.com/words?"
        + params
    )

    data = await _fetch_json(url)

    if not isinstance(data, list):
        return None

    result = []

    for item in data:
        candidate = str(
            item.get("word", "")
        ).strip()

        if not candidate:
            continue

        if candidate.lower() == word.lower():
            continue

        result.append({
            "word": candidate,
            "score": item.get("score", 0),
            "defs": item.get("defs", []),
            "tags": item.get("tags", []),
        })

    return result


# ============================================================
# WORDNET
# ============================================================

def _wordnet_sync(word):
    try:
        from nltk.corpus import wordnet as wn

        synsets = wn.synsets(word)

        synonyms = set()
        antonyms = set()
        family = set()

        for syn in synsets:

            for lemma in syn.lemmas():
                name = lemma.name().replace(
                    "_",
                    " ",
                )

                if name.lower() != word.lower():
                    synonyms.add(name)

                for ant in lemma.antonyms():
                    name = ant.name().replace(
                        "_",
                        " ",
                    )

                    if name.lower() != word.lower():
                        antonyms.add(name)

                for rel in lemma.derivationally_related_forms():
                    name = rel.name().replace(
                        "_",
                        " ",
                    )

                    if name.lower() != word.lower():
                        family.add(name)

        return {
            "synonyms": sorted(synonyms),
            "antonyms": sorted(antonyms),
            "family": sorted(family),
        }

    except Exception:
        return None


async def _wordnet(word):
    return await asyncio.to_thread(
        _wordnet_sync,
        word,
    )


# ============================================================
# WIKTIONARY
# ============================================================

def _strip_wiki_markup(text):
    if not text:
        return ""

    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    text = re.sub(
        r"\{\{[^{}]*\}\}",
        "",
        text,
    )

    text = re.sub(
        r"\[\[([^|\]]+)\|([^\]]+)\]\]",
        r"\2",
        text,
    )

    text = re.sub(
        r"\[\[([^\]]+)\]\]",
        r"\1",
        text,
    )

    text = re.sub(
        r"'{2,}",
        "",
        text,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


async def _wiktionary_sections(word):
    params = urlencode({
        "action": "parse",
        "page": word,
        "prop": "wikitext",
        "format": "json",
        "origin": "*",
    })

    url = (
        "https://en.wiktionary.org/w/api.php?"
        + params
    )

    data = await _fetch_json(url)

    try:
        text = data["parse"]["wikitext"]["*"]
    except Exception:
        return None

    def section(name):
        pattern = (
            rf"===+\s*{re.escape(name)}"
            rf"(?:\s+\d+)?\s*===+"
            rf"(.*?)(?=\n===|\Z)"
        )

        match = re.search(
            pattern,
            text,
            flags=re.I | re.S,
        )

        if not match:
            return ""

        value = _strip_wiki_markup(
            match.group(1)
        )

        if len(value) > 1000:
            value = value[:1000].rsplit(
                " ",
                1,
            )[0] + "..."

        return value

    return {
        "etymology": section("Etymology"),
        "pronunciation": section("Pronunciation"),
        "usage": section("Usage notes"),
    }


async def _get_wiktionary(session):
    if session["data"].get("wiktionary"):
        return session["data"]["wiktionary"]

    result = await _wiktionary_sections(
        session["word"]
    )

    if result:
        session["data"]["wiktionary"] = result

    return result


# ============================================================
# HELPERS
# ============================================================

def _unique(items, limit=15):
    seen = set()
    result = []

    for item in items:
        item = str(item).strip()

        if not item:
            continue

        key = item.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

        if len(result) >= limit:
            break

    return result


def _meanings(dictionary):
    if not dictionary:
        return []

    result = []

    for meaning in dictionary.get(
        "meanings",
        [],
    ):
        for definition in meaning.get(
            "definitions",
            [],
        ):
            result.append({
                "pos": meaning.get(
                    "part_of_speech",
                    "word",
                ),
                "definition": definition.get(
                    "definition",
                    "",
                ),
                "example": definition.get(
                    "example",
                    "",
                ),
            })

    return result


# ============================================================
# RELATION DATA
# ============================================================

async def _get_relation_data(session, kind):
    key = kind + "_data"

    if session["data"].get(key):
        return session["data"][key]

    word = session["word"]

    if kind == "synonym":
        results = await asyncio.gather(
            _datamuse(word, "rel_syn"),
            _wordnet(word),
        )

    elif kind == "antonym":
        results = await asyncio.gather(
            _datamuse(word, "rel_ant"),
            _wordnet(word),
        )

    elif kind == "similar":
        results = [
            await _datamuse(
                word,
                "sp",
            )
        ]

    elif kind == "homo":
        results = await asyncio.gather(
            _dictionary_data(word),
            _datamuse(word, "sl"),
        )

    elif kind == "family":
        results = [
            await _wordnet(word)
        ]

    else:
        return None

    # Do NOT cache empty/failed results.
    if not any(results):
        return None

    session["data"][key] = results

    return results


def _synonyms(data):
    if not data:
        return []

    datamuse_data, wordnet_data = data

    return _unique(
        [x["word"] for x in (datamuse_data or [])]
        + list(
            (wordnet_data or {}).get(
                "synonyms",
                [],
            )
        ),
        15,
    )


def _antonyms(data):
    if not data:
        return []

    datamuse_data, wordnet_data = data

    return _unique(
        [x["word"] for x in (datamuse_data or [])]
        + list(
            (wordnet_data or {}).get(
                "antonyms",
                [],
            )
        ),
        15,
    )


def _family(data):
    if not data or not data[0]:
        return []

    return _unique(
        data[0].get(
            "family",
            [],
        ),
        15,
    )


# ============================================================
# GROQ
# ============================================================

async def _groq(
    prompt,
    max_tokens=350,
):
    if not _ask_groq:
        return ""

    try:
        result = _ask_groq(
            prompt,
            max_tokens=max_tokens,
            system_prompt=(
                "You are a precise English-learning assistant. "
                "Use established English knowledge only. "
                "Do not invent etymology, CEFR levels, "
                "pronunciation, slang, idioms, or usage facts. "
                "If reliable information is unavailable, say so."
            ),
        )

        if inspect.isawaitable(result):
            result = await asyncio.wait_for(
                result,
                timeout=15,
            )

        return str(result).strip()

    except Exception:
        return ""


async def _arabic_meaning(
    word,
    meanings,
):
    if not meanings:
        return ""

    source = "\n".join(
        f"- {x['pos']}: {x['definition']}"
        for x in meanings[:5]
    )

    return await _groq(
        f"""
Give the concise Arabic meaning of the English word "{word}".

Use ONLY these dictionary meanings:
{source}

Return only the Arabic meaning.
""",
        100,
    )


# ============================================================
# MAIN ANALYSIS
# ============================================================

async def _build_main_analysis(
    word,
    dictionary,
):
    meanings = _meanings(dictionary)

    if meanings:
        first = meanings[0]

        text = (
            f"<b>{html.escape(word)}</b> is mainly used as a "
            f"<b>{html.escape(first['pos'] or 'word')}</b> meaning "
            f"“{html.escape(first['definition'])}”."
        )

        if (
            len(meanings) > 1
            and meanings[1]["definition"]
            != first["definition"]
        ):
            text += (
                "\nIt can also mean "
                f"“{html.escape(meanings[1]['definition'])}”."
            )

        arabic = await _arabic_meaning(
            word,
            meanings,
        )

        if arabic:
            text += (
                f"\n\n🇩🇿 "
                f"<b>{html.escape(arabic)}</b>"
            )

        return (
            "🔎 <b>Word Analysis</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + text
        )

    result = await _groq(
        f"""
Analyze the English word "{word}" for an English learner.

Give:
1. Main part of speech
2. Main meaning
3. Another important meaning if relevant
4. Concise Arabic meaning

Use established knowledge only.
Do not invent facts.
Keep it short.
""",
        250,
    )

    if result:
        return (
            "🔎 <b>Word Analysis</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + html.escape(result)
        )

    return (
        "🔎 <b>Word Analysis</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔤 <b>{html.escape(word)}</b>\n\n"
        "ℹ️ The main definition could not be loaded right now."
    )


# ============================================================
# EXTERNAL LINKS
# ============================================================

def _external_urls(word):
    safe = quote(
        word.strip(),
        safe="",
    )

    return {
        "cambridge":
            f"https://dictionary.cambridge.org/dictionary/english/{safe}",

        "oxford":
            f"https://www.oxfordlearnersdictionaries.com/definition/english/{safe}",

        "youglish":
            f"https://youglish.com/pronounce/{safe}/english",
    }


def _external_buttons(word):
    urls = _external_urls(word)

    return [
        InlineKeyboardButton(
            "📘 Cambridge",
            url=urls["cambridge"],
        ),
        InlineKeyboardButton(
            "📕 Oxford",
            url=urls["oxford"],
        ),
        InlineKeyboardButton(
            "🗣️ YouGlish",
            url=urls["youglish"],
        ),
    ]


# ============================================================
# BUTTON HELPERS
# ============================================================

def _btn(
    text,
    session_id,
    action,
):
    return InlineKeyboardButton(
        text,
        callback_data=(
            f"wa:{session_id}:{action}"
        ),
    )


# ============================================================
# MAIN 6 BUTTONS
# ============================================================

def _main_keyboard(
    session_id,
    word,
):
    return InlineKeyboardMarkup([
        [
            _btn(
                "📖 Meaning & Usage",
                session_id,
                "meaning",
            ),
            _btn(
                "🔗 Word Relations",
                session_id,
                "relations",
            ),
        ],
        [
            _btn(
                "🔬 Deep Analysis",
                session_id,
                "deep",
            ),
            _btn(
                "💬 Expressions & Idioms",
                session_id,
                "expressions",
            ),
        ],
        [
            _btn(
                "🗣️ Slang & Phrasal Verbs",
                session_id,
                "slang",
            ),
            _btn(
                "🔊 Pronunciation",
                session_id,
                "pron",
            ),
        ],
        _external_buttons(word),
    ])


# ============================================================
# MEANING & USAGE
# ============================================================

def _meaning_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "📌 Meanings",
                session_id,
                "meanings",
            ),
            _btn(
                "✏️ Examples",
                session_id,
                "examples",
            ),
        ],
        [
            _btn(
                "🔗 Collocations",
                session_id,
                "collocations",
            ),
            _btn(
                "📝 Grammar Patterns",
                session_id,
                "grammar",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# WORD RELATIONS
# ============================================================

def _relations_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "🔄 Synonyms",
                session_id,
                "syn",
            ),
            _btn(
                "🔻 Antonyms",
                session_id,
                "ant",
            ),
        ],
        [
            _btn(
                "🟰 Homophones",
                session_id,
                "homo",
            ),
            _btn(
                "✍️ Similar Spelling",
                session_id,
                "similar",
            ),
        ],
        [
            _btn(
                "🧩 Word Family",
                session_id,
                "family",
            ),
            _btn(
                "📊 Word Levels",
                session_id,
                "levels",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# DEEP ANALYSIS
# ============================================================

def _deep_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "🌱 Root",
                session_id,
                "root",
            ),
            _btn(
                "📜 Etymology",
                session_id,
                "etymology",
            ),
        ],
        [
            _btn(
                "⚠️ Register & Tone",
                session_id,
                "register",
            ),
            _btn(
                "🔤 Word Forms",
                session_id,
                "forms",
            ),
        ],
        [
            _btn(
                "📊 Word Frequency",
                session_id,
                "frequency",
            ),
            _btn(
                "🧠 Usage Notes",
                session_id,
                "usage_notes",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# EXPRESSIONS & IDIOMS
# ============================================================

def _expressions_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "💬 Idioms",
                session_id,
                "idioms",
            ),
            _btn(
                "📜 Proverbs & Sayings",
                session_id,
                "proverbs",
            ),
        ],
        [
            _btn(
                "🧠 Common Expressions",
                session_id,
                "expressions_common",
            ),
            _btn(
                "🤝 Fixed Phrases",
                session_id,
                "fixed",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# SLANG & PHRASAL VERBS
# ============================================================

def _slang_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "🗣️ Slang",
                session_id,
                "slang_result",
            ),
            _btn(
                "🔀 Phrasal Verbs",
                session_id,
                "phrasal",
            ),
        ],
        [
            _btn(
                "🌎 US / UK Usage",
                session_id,
                "usuk",
            ),
            _btn(
                "⚠️ Informal Uses",
                session_id,
                "informal",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# PRONUNCIATION
# ============================================================

def _pron_keyboard(session_id):
    return InlineKeyboardMarkup([
        [
            _btn(
                "🇺🇸 American",
                session_id,
                "american",
            ),
            _btn(
                "🇬🇧 British",
                session_id,
                "british",
            ),
        ],
        [
            _btn(
                "🔤 IPA & Stress",
                session_id,
                "ipa",
            ),
            _btn(
                "🗣️ YouGlish",
                session_id,
                "youglish",
            ),
        ],
        [
            _btn(
                "⬅️ Back",
                session_id,
                "back",
            )
        ],
    ])


# ============================================================
# MENU TEXT
# ============================================================

def _menu(
    title,
    word,
):
    return (
        f"🔎 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Choose what you want to know about "
        f"<b>{html.escape(word)}</b>:"
    )


# ============================================================
# RESULT HELPERS
# ============================================================

async def _send_section(
    message,
    title,
    body,
    source=None,
):
    text = (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}"
    )

    if source:
        text += (
            f"\n\n📚 Source: {source}"
        )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


async def _send_unavailable(message):
    await message.reply_text(
        "ℹ️ No reliable information available "
        "for this section."
    )


# ============================================================
# MEANINGS
# ============================================================

async def _result_meanings(
    message,
    word,
    dictionary,
):
    items = _meanings(dictionary)

    if not items:
        await _send_unavailable(message)
        return

    lines = []

    for index, item in enumerate(
        items[:8],
        1,
    ):
        lines.append(
            f"<b>{index}. "
            f"{html.escape(item['pos'] or 'word')}</b>\n"
            f"{html.escape(item['definition'])}"
        )

    await _send_section(
        message,
        f"📌 <b>Meanings — "
        f"{html.escape(word)}</b>",
        "\n\n".join(lines),
        "Dictionary API",
    )


# ============================================================
# EXAMPLES
# ============================================================

async def _result_examples(
    message,
    word,
    dictionary,
):
    items = [
        x
        for x in _meanings(dictionary)
        if x["example"]
    ]

    if not items:
        await _send_unavailable(message)
        return

    body = "\n\n".join(
        f"<b>{html.escape(x['pos'] or 'word')}</b>\n"
        f"“{html.escape(x['example'])}”"
        for x in items[:8]
    )

    await _send_section(
        message,
        f"✏️ <b>Examples — "
        f"{html.escape(word)}</b>",
        body,
        "Dictionary API",
    )


# ============================================================
# SYNONYMS
# ============================================================

async def _result_synonyms(
    message,
    word,
    data,
):
    items = _synonyms(data)

    if not items:
        await _send_unavailable(message)
        return

    body = "\n".join(
        f"• {html.escape(item)}"
        for item in items
    )

    await _send_section(
        message,
        f"🔄 <b>Synonyms — "
        f"{html.escape(word)}</b>",
        body,
        "WordNet / Datamuse",
    )


# ============================================================
# ANTONYMS
# ============================================================

async def _result_antonyms(
    message,
    word,
    data,
):
    items = _antonyms(data)

    if not items:
        await _send_unavailable(message)
        return

    body = "\n".join(
        f"• {html.escape(item)}"
        for item in items
    )

    await _send_section(
        message,
        f"🔻 <b>Antonyms — "
        f"{html.escape(word)}</b>",
        body,
        "WordNet / Datamuse",
    )


# ============================================================
# SIMILAR SPELLING
# ============================================================

async def _result_similar(
    message,
    word,
    data,
):
    items = []

    if data and data[0]:
        for item in data[0]:
            candidate = item.get(
                "word",
                "",
            )

            if not candidate:
                continue

            if candidate.lower() == word.lower():
                continue

            if len(candidate) <= max(
                20,
                len(word) + 8,
            ):
                items.append(candidate)

    items = _unique(
        items,
        12,
    )

    if not items:
        await _send_unavailable(message)
        return

    await _send_section(
        message,
        f"✍️ <b>Similar Spelling — "
        f"{html.escape(word)}</b>",
        "\n".join(
            f"• {html.escape(x)}"
            for x in items
        ),
        "Datamuse",
    )


# ============================================================
# HOMOPHONES
# ============================================================

async def _result_homophones(
    message,
    word,
    data,
):
    if not data:
        await _send_unavailable(message)
        return

    dictionary, candidates = data

    if not dictionary:
        await _send_unavailable(message)
        return

    source_pronunciations = set()

    for phonetic in dictionary.get(
        "phonetics",
        [],
    ):
        value = phonetic.get(
            "text",
            "",
        )

        if value:
            source_pronunciations.add(
                value.lower().strip()
            )

    found = []

    for item in candidates or []:
        candidate = item.get(
            "word",
            "",
        )

        if not candidate:
            continue

        if candidate.lower() == word.lower():
            continue

        for tag in item.get(
            "tags",
            [],
        ):
            if not tag.startswith(
                "pron:"
            ):
                continue

            pronunciation = tag[5:].lower().strip()

            if pronunciation in source_pronunciations:
                found.append(candidate)
                break

    found = _unique(
        found,
        10,
    )

    if not found:
        await _send_unavailable(message)
        return

    await _send_section(
        message,
        f"🟰 <b>Homophones — "
        f"{html.escape(word)}</b>",
        "\n".join(
            f"• {html.escape(x)}"
            for x in found
        ),
        "Datamuse",
    )


# ============================================================
# WORD FAMILY
# ============================================================

async def _result_family(
    message,
    word,
    data,
):
    items = _family(data)

    if not items:
        await _send_unavailable(message)
        return

    await _send_section(
        message,
        f"🧩 <b>Word Family — "
        f"{html.escape(word)}</b>",
        "\n".join(
            f"• {html.escape(x)}"
            for x in items
        ),
        "WordNet",
    )


# ============================================================
# GROQ SECTION
# ============================================================

async def _groq_section(
    message,
    word,
    title,
    instruction,
    dictionary=None,
    max_tokens=350,
):
    source = ""

    if dictionary:
        source = "\n".join(
            f"- {x['pos']}: {x['definition']}"
            for x in _meanings(dictionary)[:8]
        )

    prompt = f"""
Word: {word}

Dictionary information:
{source}

Task:
{instruction}

Use reliable and established English knowledge.
Do not invent information.
If there is no reliable information, say so.
"""

    result = await _groq(
        prompt,
        max_tokens,
    )

    if not result:
        await _send_unavailable(message)
        return

    await _send_section(
        message,
        title,
        html.escape(result),
    )


# ============================================================
# COMMAND
# ============================================================

async def analysis_command(
    update,
    context,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if not await _call_approved(
        user.id
    ):
        await message.reply_text(
            "❌ You are not approved to use this bot."
        )
        return

    try:
        target = ""

        if context and context.args:
            target = " ".join(
                context.args
            )

        if (
            not target
            and message.reply_to_message
        ):
            target = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )

        if (
            not target
            and _get_target_text
        ):
            try:
                target = _get_target_text(
                    message
                )

                if inspect.isawaitable(target):
                    target = await target

            except Exception:
                target = ""

        word = extract_word(target)

        if not word:
            await message.reply_text(
                "🔎 Please provide a word.\n\n"
                "Example: /analysis hate"
            )
            return

        # New analysis invalidates the previous
        # analysis of the same user.
        session_id = _create_session(
            user.id,
            word,
            {},
        )

        session = _get_valid_session(
            user.id,
            session_id,
        )

        if not session:
            return

        # Only the main information is loaded now.
        # Other sections load only when their button
        # is pressed.
        dictionary = await _dictionary_data(
            word
        )

        if dictionary:
            session["data"]["dictionary"] = (
                dictionary
            )

        main_text = await _build_main_analysis(
            word,
            dictionary,
        )

        await message.reply_text(
            main_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(
                session_id,
                word,
            ),
            disable_web_page_preview=True,
        )

    except Exception:
        try:
            await message.reply_text(
                "⚠️ Something went wrong "
                "while preparing the analysis."
            )
        except Exception:
            pass


# ============================================================
# CALLBACK
# ============================================================

async def analysis_callback(
    update,
    context,
):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    try:
        await query.answer()
    except Exception:
        pass

    parts = (
        query.data or ""
    ).split(
        ":",
        2,
    )

    if (
        len(parts) != 3
        or parts[0] != "wa"
    ):
        return

    session_id = parts[1]
    action = parts[2]

    # IMPORTANT:
    # The callback is validated against
    # the CURRENT session of THIS USER.
    session = _get_valid_session(
        user.id,
        session_id,
    )

    if not session:
        await query.message.reply_text(
            EXPIRED_TEXT
        )
        return

    word = session["word"]

    try:

        # ====================================================
        # MAIN SIX SECTIONS
        # ====================================================

        if action == "meaning":
            await query.message.reply_text(
                _menu(
                    "Meaning & Usage",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_meaning_keyboard(
                    session_id
                ),
            )
            return

        if action == "relations":
            await query.message.reply_text(
                _menu(
                    "Word Relations",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_relations_keyboard(
                    session_id
                ),
            )
            return

        if action == "deep":
            await query.message.reply_text(
                _menu(
                    "Deep Analysis",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_deep_keyboard(
                    session_id
                ),
            )
            return

        if action == "expressions":
            await query.message.reply_text(
                _menu(
                    "Expressions & Idioms",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_expressions_keyboard(
                    session_id
                ),
            )
            return

        if action == "slang":
            await query.message.reply_text(
                _menu(
                    "Slang & Phrasal Verbs",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_slang_keyboard(
                    session_id
                ),
            )
            return

        if action == "pron":
            await query.message.reply_text(
                _menu(
                    "Pronunciation",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_pron_keyboard(
                    session_id
                ),
            )
            return

        # ====================================================
        # BACK
        # ====================================================

        if action == "back":
            await query.message.reply_text(
                _menu(
                    "Word Analysis",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_main_keyboard(
                    session_id,
                    word,
                ),
                disable_web_page_preview=True,
            )
            return

        # ====================================================
        # DICTIONARY
        # ====================================================

        dictionary = None

        if action in {
            "meanings",
            "examples",
            "grammar",
            "collocations",
            "register",
            "forms",
            "frequency",
            "usage_notes",
            "levels",
            "idioms",
            "proverbs",
            "expressions_common",
            "fixed",
            "slang_result",
            "phrasal",
            "usuk",
            "informal",
            "american",
            "british",
            "ipa",
        }:
            dictionary = await _get_dictionary(
                session
            )

        # ====================================================
        # MEANING & USAGE
        # ====================================================

        if action == "meanings":
            await _result_meanings(
                query.message,
                word,
                dictionary,
            )
            return

        if action == "examples":
            await _result_examples(
                query.message,
                word,
                dictionary,
            )
            return

        if action == "collocations":
            await _groq_section(
                query.message,
                word,
                f"🔗 <b>Collocations — "
                f"{html.escape(word)}</b>",
                (
                    "List common natural collocations "
                    "with this word. Give only established "
                    "combinations. Include short meanings "
                    "where useful."
                ),
                dictionary,
            )
            return

        if action == "grammar":
            await _groq_section(
                query.message,
                word,
                f"📝 <b>Grammar Patterns — "
                f"{html.escape(word)}</b>",
                (
                    "Give common grammar patterns for "
                    "this word. Include verb patterns, "
                    "noun patterns, prepositions, "
                    "to-infinitive, -ing, or complements "
                    "only when genuinely applicable."
                ),
                dictionary,
            )
            return

        # ====================================================
        # WORD RELATIONS
        # ====================================================

        if action == "syn":
            data = await _get_relation_data(
                session,
                "synonym",
            )

            await _result_synonyms(
                query.message,
                word,
                data,
            )
            return

        if action == "ant":
            data = await _get_relation_data(
                session,
                "antonym",
            )

            await _result_antonyms(
                query.message,
                word,
                data,
            )
            return

        if action == "homo":
            data = await _get_relation_data(
                session,
                "homo",
            )

            await _result_homophones(
                query.message,
                word,
                data,
            )
            return

        if action == "similar":
            data = await _get_relation_data(
                session,
                "similar",
            )

            await _result_similar(
                query.message,
                word,
                data,
            )
            return

        if action == "family":
            data = await _get_relation_data(
                session,
                "family",
            )

            await _result_family(
                query.message,
                word,
                data,
            )
            return

        if action == "levels":
            await _groq_section(
                query.message,
                word,
                f"📊 <b>Word Levels — "
                f"{html.escape(word)}</b>",
                (
                    "Give the CEFR level of the target "
                    "word only when reliably known. "
                    "Then give a few related words with "
                    "their CEFR levels only when reliable. "
                    "Never guess CEFR levels. "
                    "If uncertain, clearly say so."
                ),
                dictionary,
            )
            return

        # ====================================================
        # DEEP ANALYSIS
        # ====================================================

        if action in {
            "root",
            "etymology",
        }:
            wiki = await _get_wiktionary(
                session
            )

            if (
                not wiki
                or not wiki.get("etymology")
            ):
                await _send_unavailable(
                    query.message
                )
                return

            if action == "etymology":
                title = (
                    f"📜 <b>Etymology — "
                    f"{html.escape(word)}</b>"
                )

                body = html.escape(
                    wiki["etymology"]
                )

                await _send_section(
                    query.message,
                    title,
                    body,
                    "Wiktionary",
                )
                return

            # Root is deliberately separated from
            # synonyms and kept inside Deep Analysis.
            root_result = await _groq(
                f"""
Word: {word}

Wiktionary etymology:
{wiki["etymology"]}

Identify the historical root/base of the word
ONLY if it can be reliably identified from the
provided etymology.

Explain very briefly:
- Root
- Original language if known
- Basic original sense

Do not guess.
If no reliable root can be identified, say:
"No reliable root information available."
""",
                220,
            )

            if not root_result:
                await _send_unavailable(
                    query.message
                )
                return

            await _send_section(
                query.message,
                f"🌱 <b>Root — "
                f"{html.escape(word)}</b>",
                html.escape(root_result),
                "Wiktionary + analysis",
            )
            return

        if action == "register":
            await _groq_section(
                query.message,
                word,
                f"⚠️ <b>Register & Tone — "
                f"{html.escape(word)}</b>",
                (
                    "Explain whether this word is "
                    "neutral, formal, informal, slang, "
                    "literary, offensive, etc. "
                    "Only use a label when reliable. "
                    "Explain the tone briefly."
                ),
                dictionary,
            )
            return

        if action == "forms":
            await _groq_section(
                query.message,
                word,
                f"🔤 <b>Word Forms — "
                f"{html.escape(word)}</b>",
                (
                    "List reliable standard forms of the "
                    "word: verb, noun, adjective, adverb, "
                    "and other common forms when they exist. "
                    "Do not invent forms."
                ),
                dictionary,
            )
            return

        if action == "frequency":
            await _groq_section(
                query.message,
                word,
                f"📊 <b>Word Frequency — "
                f"{html.escape(word)}</b>",
                (
                    "Classify this word as Very common, "
                    "Common, Less common, or Rare only "
                    "when reasonably reliable. "
                    "Do not invent numeric frequency data."
                ),
                dictionary,
            )
            return

        if action == "usage_notes":
            await _groq_section(
                query.message,
                word,
                f"🧠 <b>Usage Notes — "
                f"{html.escape(word)}</b>",
                (
                    "Give important learner notes, "
                    "subtle meaning differences, "
                    "common mistakes, special uses, "
                    "or useful distinctions. "
                    "Keep only reliable information."
                ),
                dictionary,
            )
            return

        # ====================================================
        # EXPRESSIONS & IDIOMS
        # ====================================================

        expression_tasks = {
            "idioms": (
                "List genuine common idioms containing "
                "or strongly associated with this word. "
                "Give meaning and a short example. "
                "Do not invent idioms."
            ),

            "proverbs": (
                "List genuine established proverbs "
                "or sayings containing or strongly "
                "associated with this word. "
                "Do not invent any."
            ),

            "expressions_common": (
                "List common English expressions using "
                "this word. Give a concise meaning and "
                "one short example for each."
            ),

            "fixed": (
                "List common fixed phrases involving "
                "this word. Give the phrase and its "
                "meaning. Include only established usage."
            ),
        }

        if action in expression_tasks:
            titles = {
                "idioms": "💬 Idioms",
                "proverbs": "📜 Proverbs & Sayings",
                "expressions_common": "🧠 Common Expressions",
                "fixed": "🤝 Fixed Phrases",
            }

            await _groq_section(
                query.message,
                word,
                (
                    f"{titles[action]} — "
                    f"<b>{html.escape(word)}</b>"
                ),
                expression_tasks[action],
                dictionary,
            )
            return

        # ====================================================
        # SLANG & PHRASAL VERBS
        # ====================================================

        slang_tasks = {
            "slang_result": (
                "Say whether this word has a reliable "
                "slang use. If yes, give the meaning, "
                "region (US/UK/Both), and one clean "
                "example. If none, say no reliable slang "
                "usage found."
            ),

            "phrasal": (
                "List common phrasal verbs formed with "
                "this word or strongly associated with it. "
                "Give meaning and a natural example. "
                "Do not invent phrasal verbs."
            ),

            "usuk": (
                "Give only genuine differences between "
                "US and UK English involving this word: "
                "spelling, pronunciation, meaning, "
                "or commonness. If there is no important "
                "difference, say so."
            ),

            "informal": (
                "Give important informal uses of this "
                "word that are useful for learners. "
                "Do not call something slang unless it "
                "really is slang."
            ),
        }

        if action in slang_tasks:
            titles = {
                "slang_result": "🗣️ Slang",
                "phrasal": "🔀 Phrasal Verbs",
                "usuk": "🌎 US / UK Usage",
                "informal": "⚠️ Informal Uses",
            }

            await _groq_section(
                query.message,
                word,
                (
                    f"{titles[action]} — "
                    f"<b>{html.escape(word)}</b>"
                ),
                slang_tasks[action],
                dictionary,
            )
            return

        # ====================================================
        # PRONUNCIATION
        # ====================================================

        if action in {
            "american",
            "british",
            "ipa",
        }:
            if not dictionary:
                await _send_unavailable(
                    query.message
                )
                return

            phonetics = [
                x["text"]
                for x in dictionary.get(
                    "phonetics",
                    [],
                )
                if x.get("text")
            ]

            phonetics = _unique(
                phonetics,
                6,
            )

            ipa = " / ".join(
                phonetics
            )

            if not ipa:
                await query.message.reply_text(
                    "ℹ️ No reliable pronunciation "
                    "data available."
                )
                return

            # --------------------------------------------
            # IPA & STRESS
            # --------------------------------------------

            if action == "ipa":
                await _send_section(
                    query.message,
                    f"🔤 <b>IPA & Stress — "
                    f"{html.escape(word)}</b>",
                    (
                        f"IPA: "
                        f"<b>{html.escape(ipa)}</b>\n\n"
                        "Stress information is shown "
                        "when reliable pronunciation "
                        "data provides it."
                    ),
                    "Dictionary API",
                )
                return

            # --------------------------------------------
            # AUDIO
            # --------------------------------------------

            if action == "american":
                voice = "en-US-AriaNeural"
                flag = "🇺🇸"
                label = "American"

            else:
                voice = "en-GB-SoniaNeural"
                flag = "🇬🇧"
                label = "British"

            await _send_section(
                query.message,
                (
                    f"{flag} <b>{label} — "
                    f"{html.escape(word)}</b>"
                ),
                (
                    f"🔤 IPA: "
                    f"<b>{html.escape(ipa)}</b>"
                ),
                "Dictionary API",
            )

            audio_path = None

            try:
                import edge_tts

                with tempfile.NamedTemporaryFile(
                    suffix=".mp3",
                    delete=False,
                ) as tmp:
                    audio_path = tmp.name

                communicate = edge_tts.Communicate(
                    word,
                    voice=voice,
                )

                await asyncio.wait_for(
                    communicate.save(
                        audio_path
                    ),
                    timeout=30,
                )

                with open(
                    audio_path,
                    "rb",
                ) as audio_file:
                    await query.message.reply_voice(
                        voice=audio_file,
                        caption=(
                            f"{flag} {label} — "
                            f"{word}"
                        ),
                    )

            except Exception:
                pass

            finally:
                if audio_path:
                    try:
                        Path(
                            audio_path
                        ).unlink(
                            missing_ok=True
                        )
                    except Exception:
                        pass

            return

        # ====================================================
        # YOUGLISH
        # ====================================================

        if action == "youglish":
            url = _external_urls(
                word
            )["youglish"]

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"🗣️ Open YouGlish — {word}",
                        url=url,
                    )
                ],
                [
                    _btn(
                        "⬅️ Back",
                        session_id,
                        "pron",
                    )
                ],
            ])

            await query.message.reply_text(
                (
                    "🗣️ <b>YouGlish</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    f"Listen to real examples of "
                    f"<b>{html.escape(word)}</b> "
                    "in spoken English."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return

    except Exception:
        try:
            await _send_unavailable(
                query.message
            )
        except Exception:
            pass
