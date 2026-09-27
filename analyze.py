# analyze.py
# FixMyEnglish - Word Analysis
# Main analysis is shown first.
# Every button sends a NEW message.
# Each user has an independent analysis session.
# Session lifetime: 20 minutes.
#
# IMPORTANT DESIGN:
# - Creating an analysis session NEVER depends on all APIs succeeding.
# - The main analysis is attempted first.
# - Each feature fetches ONLY its own required data when requested.
# - Failure of one feature never affects another feature.
# - A new analysis by the same user invalidates the old session.

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


# ============================================================
# CONFIGURATION
# ============================================================

SESSION_TTL = 20 * 60  # 20 minutes

_ask_groq = None
_get_target_text = None
_is_approved = None

# user_id -> current session
_sessions = {}


# ============================================================
# CONFIGURE
# ============================================================

def configure(ask_groq_func, get_target_text_func, is_approved_func):
    global _ask_groq, _get_target_text, _is_approved

    _ask_groq = ask_groq_func
    _get_target_text = get_target_text_func
    _is_approved = is_approved_func


# ============================================================
# GENERAL HELPERS
# ============================================================

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


def _clean_word(text):
    if not text:
        return ""

    text = text.strip()

    # Remove bot commands if present.
    text = re.sub(
        r"^/(?:analysis|analys)(?:@\w+)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove surrounding punctuation.
    text = text.strip(
        " \t\r\n.,!?;:\"'“”‘’()[]{}<>"
    )

    return text[:100].strip()


def extract_word(text):
    return _clean_word(text)


def _new_session_id():
    return uuid.uuid4().hex[:12]


def _create_session(user_id, word, data=None):
    session_id = _new_session_id()

    # A new analysis invalidates only the previous
    # analysis of THIS user.
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


EXPIRED_TEXT = (
    "⏳ This analysis session has expired.\n"
    "Please use /analysis again to start a new session."
)


# ============================================================
# HTTP / API HELPERS
# ============================================================

def _fetch_json_sync(url, timeout=12):
    req = Request(
        url,
        headers={
            "User-Agent": "FixMyEnglish/1.0",
            "Accept": "application/json",
        },
    )

    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")

    return json.loads(raw)


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
# DICTIONARY API
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
        part_of_speech = meaning.get(
            "partOfSpeech",
            "",
        )

        definitions = []

        for item in meaning.get(
            "definitions",
            [],
        ):
            definition = item.get(
                "definition",
                "",
            )

            example = item.get(
                "example",
                "",
            )

            if definition:
                definitions.append(
                    {
                        "definition": definition,
                        "example": example,
                    }
                )

        if definitions:
            meanings.append(
                {
                    "part_of_speech": part_of_speech,
                    "definitions": definitions,
                }
            )

    phonetics = []

    for p in entry.get(
        "phonetics",
        [],
    ):
        text = p.get("text")
        audio = p.get("audio")

        if text or audio:
            phonetics.append(
                {
                    "text": text or "",
                    "audio": audio or "",
                }
            )

    return {
        "word": entry.get(
            "word",
            word,
        ),
        "phonetics": phonetics,
        "meanings": meanings,
        "source": "Dictionary API",
    }


# ============================================================
# DATAMUSE
# ============================================================

async def _datamuse(
    word,
    relation,
    max_results=12,
):
    params = urlencode(
        {
            relation: word,
            "max": max_results,
            "md": "dp",
        }
    )

    url = (
        "https://api.datamuse.com/words?"
        + params
    )

    data = await _fetch_json(url)

    if not isinstance(data, list):
        return []

    results = []

    for item in data:
        w = item.get(
            "word",
            "",
        ).strip()

        if (
            w
            and w.lower() != word.lower()
        ):
            results.append(
                {
                    "word": w,
                    "score": item.get(
                        "score",
                        0,
                    ),
                    "defs": item.get(
                        "defs",
                        [],
                    ),
                    "tags": item.get(
                        "tags",
                        [],
                    ),
                }
            )

    return results


# ============================================================
# WORDNET
# Optional dependency.
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
                    ant_name = ant.name().replace(
                        "_",
                        " ",
                    )

                    if (
                        ant_name.lower()
                        != word.lower()
                    ):
                        antonyms.add(ant_name)

                for related in (
                    lemma.derivationally_related_forms()
                ):
                    related_name = (
                        related.name()
                        .replace("_", " ")
                    )

                    if (
                        related_name.lower()
                        != word.lower()
                    ):
                        family.add(
                            related_name
                        )

        return {
            "synonyms": sorted(synonyms),
            "antonyms": sorted(antonyms),
            "family": sorted(family),
        }

    except Exception:
        return {
            "synonyms": [],
            "antonyms": [],
            "family": [],
        }


async def _wordnet(word):
    return await asyncio.to_thread(
        _wordnet_sync,
        word,
    )


# ============================================================
# WIKTIONARY ETYMOLOGY
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

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


async def _wiktionary_etymology(word):
    params = urlencode(
        {
            "action": "parse",
            "page": word,
            "prop": "wikitext",
            "format": "json",
            "origin": "*",
        }
    )

    url = (
        "https://en.wiktionary.org/w/api.php?"
        + params
    )

    data = await _fetch_json(url)

    try:
        text = (
            data["parse"]
            ["wikitext"]
            ["*"]
        )
    except Exception:
        return ""

    match = re.search(
        r"===+\s*Etymology(?:\s+\d+)?\s*===+"
        r"(.*?)(?=\n===|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return ""

    result = _strip_wiki_markup(
        match.group(1)
    )

    if len(result) > 700:
        result = (
            result[:700]
            .rsplit(" ", 1)[0]
            + "..."
        )

    return result


# ============================================================
# DATA CACHE HELPERS
#
# Only the requested source is fetched.
# ============================================================

async def _get_dictionary_for_session(
    session,
):
    cached = session["data"].get(
        "dictionary"
    )

    if cached is not None:
        return cached

    word = session["word"]

    dictionary = await _dictionary_data(
        word
    )

    if dictionary:
        session["data"][
            "dictionary"
        ] = dictionary

    return dictionary


async def _get_synonym_data(session):
    word = session["word"]

    if "synonym_data" in session["data"]:
        return session["data"][
            "synonym_data"
        ]

    datamuse_task = _datamuse(
        word,
        "rel_syn",
    )

    wordnet_task = _wordnet(word)

    datamuse, wordnet = await asyncio.gather(
        datamuse_task,
        wordnet_task,
    )

    result = {
        "datamuse": datamuse,
        "wordnet": wordnet,
    }

    session["data"][
        "synonym_data"
    ] = result

    return result


async def _get_antonym_data(session):
    word = session["word"]

    if "antonym_data" in session["data"]:
        return session["data"][
            "antonym_data"
        ]

    datamuse_task = _datamuse(
        word,
        "rel_ant",
    )

    wordnet_task = _wordnet(word)

    datamuse, wordnet = await asyncio.gather(
        datamuse_task,
        wordnet_task,
    )

    result = {
        "datamuse": datamuse,
        "wordnet": wordnet,
    }

    session["data"][
        "antonym_data"
    ] = result

    return result


async def _get_family_data(session):
    word = session["word"]

    if "family_data" in session["data"]:
        return session["data"][
            "family_data"
        ]

    result = await _wordnet(word)

    session["data"][
        "family_data"
    ] = result

    return result


async def _get_similar_data(session):
    word = session["word"]

    if "similar_data" in session["data"]:
        return session["data"][
            "similar_data"
        ]

    result = await _datamuse(
        word,
        "sp",
    )

    session["data"][
        "similar_data"
    ] = result

    return result


async def _get_homophone_data(session):
    word = session["word"]

    if "homophone_data" in session["data"]:
        return session["data"][
            "homophone_data"
        ]

    dictionary_task = _dictionary_data(
        word
    )

    sound_task = _datamuse(
        word,
        "sl",
    )

    dictionary, sound_alikes = await asyncio.gather(
        dictionary_task,
        sound_task,
    )

    if dictionary:
        session["data"][
            "dictionary"
        ] = dictionary

    result = {
        "dictionary": dictionary,
        "sound_alikes": sound_alikes,
    }

    session["data"][
        "homophone_data"
    ] = result

    return result


async def _get_etymology_data(session):
    word = session["word"]

    if "etymology_data" in session["data"]:
        return session["data"][
            "etymology_data"
        ]

    result = await _wiktionary_etymology(
        word
    )

    session["data"][
        "etymology_data"
    ] = result

    return result


async def _get_collocation_data(session):
    word = session["word"]

    if "collocation_data" in session["data"]:
        return session["data"][
            "collocation_data"
        ]

    result = await _datamuse(
        word,
        "rel_trg",
        max_results=12,
    )

    session["data"][
        "collocation_data"
    ] = result

    return result


# ============================================================
# DATA FORMATTING
# ============================================================

def _unique(items, limit=12):
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


def _get_meanings(dictionary):
    if not dictionary:
        return []

    result = []

    for meaning in dictionary.get(
        "meanings",
        [],
    ):
        pos = meaning.get(
            "part_of_speech",
            "",
        )

        for definition in meaning.get(
            "definitions",
            [],
        ):
            result.append(
                {
                    "pos": pos,
                    "definition": definition.get(
                        "definition",
                        "",
                    ),
                    "example": definition.get(
                        "example",
                        "",
                    ),
                }
            )

    return result


def _get_synonyms(data):
    wn = data.get(
        "wordnet",
        {},
    )

    return _unique(
        [
            *(
                item["word"]
                for item in data.get(
                    "datamuse",
                    [],
                )
            ),
            *wn.get(
                "synonyms",
                [],
            ),
        ],
        12,
    )


def _get_antonyms(data):
    wn = data.get(
        "wordnet",
        {},
    )

    return _unique(
        [
            *(
                item["word"]
                for item in data.get(
                    "datamuse",
                    [],
                )
            ),
            *wn.get(
                "antonyms",
                [],
            ),
        ],
        12,
    )


def _get_word_family(data):
    wn = data.get(
        "wordnet",
        {}
    )

    return _unique(
        wn.get(
            "family",
            [],
        ),
        15,
    )


# ============================================================
# GROQ
# ============================================================

async def _groq_text(
    prompt,
    max_tokens=300,
):
    if not _ask_groq:
        return ""

    try:
        result = _ask_groq(
            prompt,
            max_tokens=max_tokens,
            system_prompt=(
                "You are a precise English-learning assistant. "
                "Use ONLY the information supplied by the user. "
                "Do not invent facts, meanings, synonyms, "
                "etymology, pronunciation, or examples."
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
        f"- {m['pos']}: {m['definition']}"
        for m in meanings[:4]
    )

    prompt = (
        f"Give a concise Arabic meaning for the English "
        f"word '{word}' based ONLY on these dictionary definitions:\n"
        f"{source}\n\n"
        "Return only the Arabic meaning, with no explanation."
    )

    return await _groq_text(
        prompt,
        max_tokens=120,
    )


# ============================================================
# MAIN ANALYSIS
#
# IMPORTANT:
# Dictionary API is OPTIONAL here.
# If it fails, we STILL create the session
# and STILL show the buttons.
# ============================================================

async def _build_main_analysis(
    word,
    dictionary,
):
    meanings = _get_meanings(
        dictionary
    )

    if not meanings:
        # Dictionary failed.
        # Try Groq as a secondary way to produce
        # the main learner-friendly analysis.
        result = await _groq_text(
            f"""
Give a very short learner-friendly analysis of
the English word "{word}".

Use only generally established English knowledge.
Include:
- the most common part of speech
- the main meaning
- whether it has another common use if relevant
- a concise Arabic meaning

Keep it to about 4-6 short lines.
Do not invent obscure information.
""",
            max_tokens=220,
        )

        if result:
            return (
                "🔎 <b>Word Analysis</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"{html.escape(result)}"
            )

        # Even if every source fails, the analysis session
        # must still exist and the buttons must still appear.
        return (
            "🔎 <b>Word Analysis</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>{html.escape(word)}</b>\n"
            "ℹ️ Main definition data is temporarily unavailable.\n"
            "You can still use the analysis sections below."
        )

    first = meanings[0]

    pos = first["pos"] or "word"
    definition = first["definition"]

    paragraph = (
        f"<b>{html.escape(word)}</b> is mainly used as a "
        f"<b>{html.escape(pos)}</b> meaning "
        f"“{html.escape(definition)}”."
    )

    if len(meanings) > 1:
        second = meanings[1]

        if (
            second["definition"]
            and second["definition"]
            != definition
        ):
            paragraph += (
                f" It can also mean "
                f"“{html.escape(second['definition'])}”."
            )

    example = first.get(
        "example",
        "",
    )

    if example:
        paragraph += (
            f" For example: "
            f"“{html.escape(example)}”"
        )

    arabic = await _arabic_meaning(
        word,
        meanings,
    )

    if arabic:
        paragraph += (
            f"\n🇩🇿 <b>{html.escape(arabic)}</b>"
        )

    return (
        "🔎 <b>Word Analysis</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + paragraph
    )


# ============================================================
# LINKS
# ============================================================

def _dictionary_links(word):
    safe = quote(
        word.strip(),
        safe="",
    )

    cambridge = (
        "https://dictionary.cambridge.org/"
        f"dictionary/english/{safe}"
    )

    oxford = (
        "https://www.oxfordlearnersdictionaries.com/"
        f"definition/english/{safe}"
    )

    youglish = (
        "https://youglish.com/pronounce/"
        f"{safe}/english"
    )

    return (
        cambridge,
        oxford,
        youglish,
    )


# ============================================================
# MAIN KEYBOARD
# ============================================================

def _main_keyboard(
    word,
    session_id,
):
    cambridge, oxford, youglish = (
        _dictionary_links(word)
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📘 Cambridge",
                    url=cambridge,
                ),
                InlineKeyboardButton(
                    "📕 Oxford",
                    url=oxford,
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗣️ YouGlish",
                    url=youglish,
                ),
            ],
            [
                InlineKeyboardButton(
                    "📖 Meaning & Usage",
                    callback_data=(
                        f"wa:{session_id}:meaning"
                    ),
                ),
                InlineKeyboardButton(
                    "🔗 Word Relations",
                    callback_data=(
                        f"wa:{session_id}:relations"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔬 Deep Analysis",
                    callback_data=(
                        f"wa:{session_id}:deep"
                    ),
                ),
                InlineKeyboardButton(
                    "🔊 Pronunciation",
                    callback_data=(
                        f"wa:{session_id}:pron"
                    ),
                ),
            ],
        ]
    )


# ============================================================
# SECTION MENUS
# ============================================================

def _meaning_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📖 Meanings",
                    callback_data=(
                        f"wa:{session_id}:meanings"
                    ),
                ),
                InlineKeyboardButton(
                    "📝 Examples",
                    callback_data=(
                        f"wa:{session_id}:examples"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔗 Collocations",
                    callback_data=(
                        f"wa:{session_id}:collocations"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"wa:{session_id}:back"
                    ),
                ),
            ],
        ]
    )


def _relations_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Synonyms",
                    callback_data=(
                        f"wa:{session_id}:syn"
                    ),
                ),
                InlineKeyboardButton(
                    "🔻 Antonyms",
                    callback_data=(
                        f"wa:{session_id}:ant"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟰 Homophones",
                    callback_data=(
                        f"wa:{session_id}:homo"
                    ),
                ),
                InlineKeyboardButton(
                    "✍️ Similar Spelling",
                    callback_data=(
                        f"wa:{session_id}:similar"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧩 Word Family",
                    callback_data=(
                        f"wa:{session_id}:family"
                    ),
                ),
                InlineKeyboardButton(
                    "🌱 Root",
                    callback_data=(
                        f"wa:{session_id}:root"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"wa:{session_id}:back"
                    ),
                ),
            ],
        ]
    )


def _deep_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📚 Etymology",
                    callback_data=(
                        f"wa:{session_id}:etymology"
                    ),
                ),
                InlineKeyboardButton(
                    "📊 CEFR Level",
                    callback_data=(
                        f"wa:{session_id}:cefr"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚖️ Usage & Register",
                    callback_data=(
                        f"wa:{session_id}:usage"
                    ),
                ),
                InlineKeyboardButton(
                    "🧠 Grammar Patterns",
                    callback_data=(
                        f"wa:{session_id}:grammar"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"wa:{session_id}:back"
                    ),
                ),
            ],
        ]
    )


def _pron_keyboard(
    session_id,
    word,
):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔊 Full Pronunciation",
                    callback_data=(
                        f"wa:{session_id}:pron_result"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗣️ YouGlish",
                    url=_dictionary_links(
                        word
                    )[2],
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=(
                        f"wa:{session_id}:back"
                    ),
                ),
            ],
        ]
    )


# ============================================================
# MENU TEXTS
# ============================================================

def _menu_text(
    title,
    word,
):
    return (
        f"🔎 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Choose what you want to know about "
        f"<b>{html.escape(word)}</b>:"
    )


# ============================================================
# RESULT: MEANINGS
# ============================================================

async def _send_meanings(
    message,
    word,
    dictionary,
):
    meanings = _get_meanings(
        dictionary
    )

    if not meanings:
        await message.reply_text(
            "ℹ️ No reliable meaning data available."
        )
        return

    lines = [
        f"📖 <b>Meanings — {html.escape(word)}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for i, item in enumerate(
        meanings[:8],
        1,
    ):
        pos = item["pos"] or "word"

        lines.append(
            f"\n<b>{i}. {html.escape(pos)}</b>\n"
            f"{html.escape(item['definition'])}"
        )

    lines.append(
        "\n\n📚 Source: Dictionary API"
    )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: EXAMPLES
# ============================================================

async def _send_examples(
    message,
    word,
    dictionary,
):
    meanings = _get_meanings(
        dictionary
    )

    examples = []

    for item in meanings:
        if item.get("example"):
            examples.append(
                (
                    item.get(
                        "pos",
                        "word",
                    ),
                    item["example"],
                )
            )

    if not examples:
        await message.reply_text(
            "ℹ️ No reliable example sentences available."
        )
        return

    lines = [
        f"📝 <b>Examples — {html.escape(word)}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for pos, example in examples[:6]:
        lines.append(
            f"\n<b>{html.escape(pos)}</b>\n"
            f"“{html.escape(example)}”"
        )

    lines.append(
        "\n\n📚 Source: Dictionary API"
    )

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: SYNONYMS
# ============================================================

async def _send_synonyms(
    message,
    word,
    data,
):
    items = _get_synonyms(data)

    if not items:
        await message.reply_text(
            "ℹ️ No reliable synonyms available."
        )
        return

    text = (
        f"🔄 <b>Synonyms — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in items
        )
        + "\n\n📚 Sources: WordNet / Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: ANTONYMS
# ============================================================

async def _send_antonyms(
    message,
    word,
    data,
):
    items = _get_antonyms(data)

    if not items:
        await message.reply_text(
            "ℹ️ No reliable antonyms available."
        )
        return

    text = (
        f"🔻 <b>Antonyms — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in items
        )
        + "\n\n📚 Sources: WordNet / Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: SIMILAR SPELLING
# ============================================================

async def _send_similar(
    message,
    word,
    items,
):
    filtered = []

    for item in items:
        candidate = item.get(
            "word",
            "",
        ).strip()

        if (
            not candidate
            or candidate.lower()
            == word.lower()
        ):
            continue

        if len(candidate) > max(
            20,
            len(word) + 8,
        ):
            continue

        filtered.append(candidate)

    filtered = _unique(
        filtered,
        12,
    )

    if not filtered:
        await message.reply_text(
            "ℹ️ No reliable similar-spelling words available."
        )
        return

    text = (
        f"✍️ <b>Similar Spelling — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in filtered
        )
        + "\n\n📚 Source: Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: HOMOPHONES
# ============================================================

async def _send_homophones(
    message,
    word,
    data,
):
    dictionary = data.get(
        "dictionary"
    )

    candidates = data.get(
        "sound_alikes",
        [],
    )

    if not dictionary:
        await message.reply_text(
            "ℹ️ No reliable homophones available."
        )
        return

    source_prons = set()

    for p in dictionary.get(
        "phonetics",
        [],
    ):
        text = p.get(
            "text",
            "",
        )

        if text:
            cleaned = re.sub(
                r"[^a-zA-Zəɪʊʌɔɑæɛɒːʃʒθðŋtʃdʒˈˌ]",
                "",
                text,
            ).lower()

            source_prons.add(
                cleaned
            )

    homophones = []

    for item in candidates:
        candidate = item.get(
            "word",
            "",
        ).strip()

        tags = item.get(
            "tags",
            [],
        )

        if (
            not candidate
            or candidate.lower()
            == word.lower()
        ):
            continue

        pron_values = []

        for tag in tags:
            if tag.startswith(
                "pron:"
            ):
                pron_values.append(
                    tag[5:]
                )

        if (
            source_prons
            and pron_values
        ):
            for p in pron_values:
                clean = re.sub(
                    r"[^a-zA-Zəɪʊʌɔɑæɛɒːʃʒθðŋtʃdʒˈˌ]",
                    "",
                    p,
                ).lower()

                if clean in source_prons:
                    homophones.append(
                        candidate
                    )
                    break

    homophones = _unique(
        homophones,
        10,
    )

    if not homophones:
        await message.reply_text(
            "ℹ️ No reliable homophones available."
        )
        return

    text = (
        f"🟰 <b>Homophones — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in homophones
        )
        + "\n\n📚 Source: pronunciation data"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: WORD FAMILY
# ============================================================

async def _send_family(
    message,
    word,
    data,
):
    items = _get_word_family(
        data
    )

    if not items:
        await message.reply_text(
            "ℹ️ No reliable word-family data available."
        )
        return

    text = (
        f"🧩 <b>Word Family — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in items
        )
        + "\n\n📚 Source: WordNet"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: ROOT
# ============================================================

async def _send_root(
    message,
    word,
    etymology,
):
    if not etymology:
        await message.reply_text(
            "ℹ️ No reliable root data available."
        )
        return

    text = (
        f"🌱 <b>Root / Etymology — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(etymology)}\n\n"
        "📚 Source: Wiktionary"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: ETYMOLOGY
# ============================================================

async def _send_etymology(
    message,
    word,
    etymology,
):
    if not etymology:
        await message.reply_text(
            "ℹ️ No reliable etymology available."
        )
        return

    text = (
        f"📚 <b>Etymology — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(etymology)}\n\n"
        "📚 Source: Wiktionary"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: COLLOCATIONS
# ============================================================

async def _send_collocations(
    message,
    word,
    related,
):
    if not related:
        await message.reply_text(
            "ℹ️ No reliable collocation data available."
        )
        return

    items = _unique(
        [
            item["word"]
            for item in related
        ],
        10,
    )

    if not items:
        await message.reply_text(
            "ℹ️ No reliable collocation data available."
        )
        return

    text = (
        f"🔗 <b>Commonly Related Words — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(
            html.escape(x)
            for x in items
        )
        + "\n\n📚 Source: Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: USAGE & REGISTER
# ============================================================

async def _send_usage(
    message,
    word,
    dictionary,
):
    meanings = _get_meanings(
        dictionary
    )

    if not meanings:
        await message.reply_text(
            "ℹ️ No reliable usage data available."
        )
        return

    source = "\n".join(
        f"- {m['pos']}: {m['definition']}"
        for m in meanings[:5]
    )

    result = await _groq_text(
        f"""
Explain the common usage of the English word "{word}"
using ONLY these dictionary meanings:

{source}

Give a short learner-friendly explanation.
Do not invent meanings or facts.
Mention register only if it can be safely inferred
from the supplied data.
""",
        max_tokens=250,
    )

    if not result:
        result = (
            "ℹ️ No additional reliable usage information available."
        )

    await message.reply_text(
        f"⚖️ <b>Usage & Register — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(result)}",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: GRAMMAR PATTERNS
# ============================================================

async def _send_grammar(
    message,
    word,
    dictionary,
):
    meanings = _get_meanings(
        dictionary
    )

    if not meanings:
        await message.reply_text(
            "ℹ️ No reliable grammar data available."
        )
        return

    source = "\n".join(
        f"- {m['pos']}: {m['definition']}"
        for m in meanings[:5]
    )

    examples = "\n".join(
        f"- {m['example']}"
        for m in meanings
        if m.get("example")
    )

    result = await _groq_text(
        f"""
For the English word "{word}", explain only the common
grammar patterns that are directly supported by the supplied
dictionary definitions/examples.

Definitions:
{source}

Examples:
{examples}

Be concise.
Do not invent uncommon grammar patterns.
""",
        max_tokens=300,
    )

    if not result:
        result = (
            "ℹ️ No additional reliable grammar information available."
        )

    await message.reply_text(
        f"🧠 <b>Grammar Patterns — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(result)}",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: CEFR
# ============================================================

async def _send_cefr(
    message,
    word,
):
    # We do not guess CEFR.
    await message.reply_text(
        f"📊 <b>CEFR Level — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "ℹ️ No reliable CEFR data available.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# PRONUNCIATION
# ============================================================

def _phonetic_values(
    dictionary,
):
    values = []

    if not dictionary:
        return values

    for item in dictionary.get(
        "phonetics",
        [],
    ):
        text = item.get(
            "text",
            "",
        ).strip()

        if (
            text
            and text not in values
        ):
            values.append(text)

    return values


def _format_pronunciation(
    word,
    dictionary,
):
    phonetics = _phonetic_values(
        dictionary
    )

    if phonetics:
        ipa = " / ".join(
            phonetics[:4]
        )
    else:
        ipa = (
            "Not available from current "
            "dictionary data."
        )

    return (
        f"🔊 <b>Pronunciation — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔤 <b>IPA:</b> {html.escape(ipa)}\n"
        "🇺🇸 <b>American:</b> "
        "Dialect-specific data not available.\n"
        "🇬🇧 <b>British:</b> "
        "Dialect-specific data not available.\n"
        "🔤 <b>Syllables:</b> "
        "Not available from current source.\n"
        "📌 <b>Stress:</b> "
        "Not available from current source.\n"
        "🔇 <b>Silent letters:</b> "
        "Not reliably available."
    )


async def _send_pronunciation(
    message,
    word,
    dictionary,
):
    if not dictionary:
        await message.reply_text(
            "ℹ️ No reliable pronunciation data available."
        )
        return

    await message.reply_text(
        _format_pronunciation(
            word,
            dictionary,
        ),
        parse_mode=ParseMode.HTML,
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
            voice="en-US-AriaNeural",
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
            await message.reply_voice(
                voice=audio_file,
                caption=f"🇺🇸 {word}",
            )

    except Exception:
        pass

    finally:
        try:
            if audio_path:
                Path(
                    audio_path
                ).unlink(
                    missing_ok=True
                )
        except Exception:
            pass


# ============================================================
# ANALYSIS COMMAND
# ============================================================

async def analysis_command(
    update,
    context,
):
    message = update.effective_message

    if not message:
        return

    user = update.effective_user

    if not user:
        return

    if not await _call_approved(
        user.id
    ):
        await message.reply_text(
            "❌ You are not approved to use this bot."
        )
        return

    try:

        # ----------------------------------------------------
        # GET TARGET
        # ----------------------------------------------------

        target = ""

        if _get_target_text:
            try:
                target = _get_target_text(
                    message
                )

                if inspect.isawaitable(
                    target
                ):
                    target = await target

            except Exception:
                target = ""

        # Command arguments.
        if (
            not target
            and context.args
        ):
            target = " ".join(
                context.args
            )

        # Reply-to-message.
        if (
            not target
            and message.reply_to_message
        ):
            target = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )

        word = extract_word(
            target
        )

        if not word:
            await message.reply_text(
                "🔎 Please provide a word.\n\n"
                "Example: /analysis hate"
            )
            return

        # ----------------------------------------------------
        # IMPORTANT:
        # CREATE THE SESSION FIRST.
        #
        # We do NOT call _collect_data().
        # No API is allowed to cancel the analysis.
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # MAIN ANALYSIS ONLY
        #
        # Dictionary is used only for the main card.
        # Even if it fails, the session remains valid.
        # ----------------------------------------------------

        dictionary = None

        try:
            dictionary = (
                await _dictionary_data(
                    word
                )
            )
        except Exception:
            dictionary = None

        if dictionary:
            session["data"][
                "dictionary"
            ] = dictionary

        main_text = (
            await _build_main_analysis(
                word,
                dictionary,
            )
        )

        # ----------------------------------------------------
        # ALWAYS SHOW THE BUTTONS
        # ----------------------------------------------------

        await message.reply_text(
            main_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(
                word,
                session_id,
            ),
            disable_web_page_preview=True,
        )

    except Exception:
        # Even an unexpected error in main generation
        # must not leave the user without a session.
        try:
            await message.reply_text(
                "⚠️ Something went wrong while preparing "
                "the analysis."
            )
        except Exception:
            pass


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def analysis_callback(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    user = update.effective_user

    if not user:
        return

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    # Expected:
    # wa:<session_id>:<action>

    parts = data.split(
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

    # --------------------------------------------------------
    # USER + SESSION CHECK
    # --------------------------------------------------------

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
        # MAIN SECTION MENUS
        # ====================================================

        if action == "meaning":
            await query.message.reply_text(
                _menu_text(
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
                _menu_text(
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
                _menu_text(
                    "Deep Analysis",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_deep_keyboard(
                    session_id
                ),
            )
            return

        if action == "pron":
            await query.message.reply_text(
                _menu_text(
                    "Pronunciation",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_pron_keyboard(
                    session_id,
                    word,
                ),
            )
            return

        # ====================================================
        # BACK
        # ====================================================

        if action == "back":
            await query.message.reply_text(
                _menu_text(
                    "Word Analysis",
                    word,
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_main_keyboard(
                    word,
                    session_id,
                ),
            )
            return

        # ====================================================
        # MEANING & USAGE
        # ====================================================

        if action == "meanings":

            dictionary = (
                await _get_dictionary_for_session(
                    session
                )
            )

            await _send_meanings(
                query.message,
                word,
                dictionary,
            )
            return

        if action == "examples":

            dictionary = (
                await _get_dictionary_for_session(
                    session
                )
            )

            await _send_examples(
                query.message,
                word,
                dictionary,
            )
            return

        if action == "collocations":

            related = (
                await _get_collocation_data(
                    session
                )
            )

            await _send_collocations(
                query.message,
                word,
                related,
            )
            return

        # ====================================================
        # WORD RELATIONS
        # ====================================================

        if action == "syn":

            data = (
                await _get_synonym_data(
                    session
                )
            )

            await _send_synonyms(
                query.message,
                word,
                data,
            )
            return

        if action == "ant":

            data = (
                await _get_antonym_data(
                    session
                )
            )

            await _send_antonyms(
                query.message,
                word,
                data,
            )
            return

        if action == "homo":

            data = (
                await _get_homophone_data(
                    session
                )
            )

            await _send_homophones(
                query.message,
                word,
                data,
            )
            return

        if action == "similar":

            data = (
                await _get_similar_data(
                    session
                )
            )

            await _send_similar(
                query.message,
                word,
                data,
            )
            return

        if action == "family":

            data = (
                await _get_family_data(
                    session
                )
            )

            await _send_family(
                query.message,
                word,
                data,
            )
            return

        if action == "root":

            etymology = (
                await _get_etymology_data(
                    session
                )
            )

            await _send_root(
                query.message,
                word,
                etymology,
            )
            return

        # ====================================================
        # DEEP ANALYSIS
        # ====================================================

        if action == "etymology":

            etymology = (
                await _get_etymology_data(
                    session
                )
            )

            await _send_etymology(
                query.message,
                word,
                etymology,
            )
            return

        if action == "cefr":

            await _send_cefr(
                query.message,
                word,
            )
            return

        if action == "usage":

            dictionary = (
                await _get_dictionary_for_session(
                    session
                )
            )

            await _send_usage(
                query.message,
                word,
                dictionary,
            )
            return

        if action == "grammar":

            dictionary = (
                await _get_dictionary_for_session(
                    session
                )
            )

            await _send_grammar(
                query.message,
                word,
                dictionary,
            )
            return

        # ====================================================
        # PRONUNCIATION
        # ====================================================

        if action == "pron_result":

            dictionary = (
                await _get_dictionary_for_session(
                    session
                )
            )

            await _send_pronunciation(
                query.message,
                word,
                dictionary,
            )
            return

    except Exception:

        # Failure of one requested feature affects
        # ONLY that feature.
        try:
            await query.message.reply_text(
                "ℹ️ No reliable information is available "
                "for this section right now."
            )
        except Exception:
            pass
