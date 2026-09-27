# analyze.py
# FixMyEnglish - Word Analysis
# Main analysis stays unchanged.
# Every button sends a NEW message.
# Each user has an independent analysis session.
# Session lifetime: 20 minutes.

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
# A new analysis by the same user invalidates the old one.
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
    text = text.strip(" \t\r\n.,!?;:\"'“”‘’()[]{}<>")

    return text[:100].strip()


def extract_word(text):
    return _clean_word(text)


def _new_session_id():
    return uuid.uuid4().hex[:12]


def _create_session(user_id, word, data):
    session_id = _new_session_id()

    # Invalidate the previous analysis of THIS user.
    _sessions[user_id] = {
        "session_id": session_id,
        "word": word,
        "data": data,
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
            asyncio.to_thread(_fetch_json_sync, url, timeout),
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
        part_of_speech = meaning.get("partOfSpeech", "")
        definitions = []

        for item in meaning.get("definitions", []):
            definition = item.get("definition", "")
            example = item.get("example", "")

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

    for p in entry.get("phonetics", []):
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
        "word": entry.get("word", word),
        "phonetics": phonetics,
        "meanings": meanings,
        "source": "Dictionary API",
    }


# ============================================================
# DATAMUSE
# ============================================================

async def _datamuse(word, relation, max_results=12):
    params = urlencode(
        {
            relation: word,
            "max": max_results,
            "md": "dp",
        }
    )

    url = "https://api.datamuse.com/words?" + params
    data = await _fetch_json(url)

    if not isinstance(data, list):
        return []

    results = []

    for item in data:
        w = item.get("word", "").strip()

        if w and w.lower() != word.lower():
            results.append(
                {
                    "word": w,
                    "score": item.get("score", 0),
                    "defs": item.get("defs", []),
                    "tags": item.get("tags", []),
                }
            )

    return results


# ============================================================
# WORDNET
# Optional: works if nltk + WordNet corpus are already installed.
# It never becomes a required dependency.
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
                name = lemma.name().replace("_", " ")

                if name.lower() != word.lower():
                    synonyms.add(name)

                for ant in lemma.antonyms():
                    ant_name = ant.name().replace("_", " ")
                    if ant_name.lower() != word.lower():
                        antonyms.add(ant_name)

                for related in lemma.derivationally_related_forms():
                    related_name = related.name().replace("_", " ")

                    if related_name.lower() != word.lower():
                        family.add(related_name)

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
    return await asyncio.to_thread(_wordnet_sync, word)


# ============================================================
# WIKTIONARY ETYMOLOGY
# ============================================================

def _strip_wiki_markup(text):
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"\s+", " ", text)

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

    url = "https://en.wiktionary.org/w/api.php?" + params

    data = await _fetch_json(url)

    try:
        text = data["parse"]["wikitext"]["*"]
    except Exception:
        return ""

    # Try normal Etymology section.
    match = re.search(
        r"===+\s*Etymology(?:\s+\d+)?\s*===+(.*?)(?=\n===|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return ""

    result = _strip_wiki_markup(match.group(1))

    # Keep it concise.
    if len(result) > 700:
        result = result[:700].rsplit(" ", 1)[0] + "..."

    return result


# ============================================================
# COLLECT RELIABLE DATA
# ============================================================

async def _collect_data(word):
    dictionary_task = _dictionary_data(word)
    synonym_task = _datamuse(word, "rel_syn")
    antonym_task = _datamuse(word, "rel_ant")
    similar_task = _datamuse(word, "sp")
    sound_task = _datamuse(word, "sl")
    wordnet_task = _wordnet(word)
    etymology_task = _wiktionary_etymology(word)

    (
        dictionary,
        datamuse_synonyms,
        datamuse_antonyms,
        similar_spelling,
        sound_alikes,
        wordnet_data,
        etymology,
    ) = await asyncio.gather(
        dictionary_task,
        synonym_task,
        antonym_task,
        similar_task,
        sound_task,
        wordnet_task,
        etymology_task,
    )

    if not dictionary:
        return None

    return {
        "dictionary": dictionary,
        "datamuse_synonyms": datamuse_synonyms,
        "datamuse_antonyms": datamuse_antonyms,
        "similar_spelling": similar_spelling,
        "sound_alikes": sound_alikes,
        "wordnet": wordnet_data,
        "etymology": etymology,
    }


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


def _get_meanings(data):
    dictionary = data["dictionary"]

    result = []

    for meaning in dictionary.get("meanings", []):
        pos = meaning.get("part_of_speech", "")
        for definition in meaning.get("definitions", []):
            result.append(
                {
                    "pos": pos,
                    "definition": definition.get("definition", ""),
                    "example": definition.get("example", ""),
                }
            )

    return result


def _get_synonyms(data):
    wn = data.get("wordnet", {})

    return _unique(
        [
            *(item["word"] for item in data.get("datamuse_synonyms", [])),
            *wn.get("synonyms", []),
        ],
        12,
    )


def _get_antonyms(data):
    wn = data.get("wordnet", {})

    return _unique(
        [
            *(item["word"] for item in data.get("datamuse_antonyms", [])),
            *wn.get("antonyms", []),
        ],
        12,
    )


def _get_word_family(data):
    wn = data.get("wordnet", {})
    return _unique(wn.get("family", []), 15)


# ============================================================
# GROQ TRANSLATION
# Groq is used only to organize/translate verified source data.
# It is NOT used as the factual source.
# ============================================================

async def _groq_text(prompt, max_tokens=300):
    if not _ask_groq:
        return ""

    try:
        result = _ask_groq(
            prompt,
            max_tokens=max_tokens,
            system_prompt=(
                "You are a precise English-learning assistant. "
                "Use ONLY the information supplied by the user. "
                "Do not invent facts, meanings, synonyms, etymology, "
                "pronunciation, or examples."
            ),
        )

        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=15)

        return str(result).strip()

    except Exception:
        return ""


async def _arabic_meaning(word, meanings):
    if not meanings:
        return ""

    source = "\n".join(
        f"- {m['pos']}: {m['definition']}"
        for m in meanings[:4]
    )

    prompt = (
        f"Give a concise Arabic meaning for the English word "
        f"'{word}' based ONLY on these dictionary definitions:\n"
        f"{source}\n\n"
        "Return only the Arabic meaning, with no explanation."
    )

    return await _groq_text(prompt, max_tokens=120)


# ============================================================
# MAIN ANALYSIS PARAGRAPH
# ============================================================

async def _build_main_analysis(word, data):
    meanings = _get_meanings(data)

    if not meanings:
        return (
            "⚠️ Word found, but reliable definition data is unavailable."
        )

    first = meanings[0]

    parts = []

    pos = first["pos"] or "word"
    definition = first["definition"]

    paragraph = (
        f"<b>{html.escape(word)}</b> is mainly used as a "
        f"<b>{html.escape(pos)}</b> meaning "
        f"“{html.escape(definition)}”."
    )

    if len(meanings) > 1:
        second = meanings[1]

        if second["definition"] and second["definition"] != definition:
            paragraph += (
                f" It can also mean "
                f"“{html.escape(second['definition'])}”."
            )

    example = first.get("example", "")

    if example:
        paragraph += (
            f" For example: “{html.escape(example)}”"
        )

    arabic = await _arabic_meaning(word, meanings)

    if arabic:
        paragraph += f"\n🇩🇿 <b>{html.escape(arabic)}</b>"

    return (
        "🔎 <b>Word Analysis</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + paragraph
    )


# ============================================================
# LINKS
# ============================================================

def _dictionary_links(word):
    safe = quote(word.strip(), safe="")

    cambridge = (
        f"https://dictionary.cambridge.org/dictionary/english/{safe}"
    )

    oxford = (
        f"https://www.oxfordlearnersdictionaries.com/"
        f"definition/english/{safe}"
    )

    youglish = (
        f"https://youglish.com/pronounce/{safe}/english"
    )

    return cambridge, oxford, youglish


# ============================================================
# MAIN KEYBOARD
# ============================================================

def _main_keyboard(word, session_id):
    cambridge, oxford, youglish = _dictionary_links(word)

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
                    callback_data=f"wa:{session_id}:meaning",
                ),
                InlineKeyboardButton(
                    "🔗 Word Relations",
                    callback_data=f"wa:{session_id}:relations",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔬 Deep Analysis",
                    callback_data=f"wa:{session_id}:deep",
                ),
                InlineKeyboardButton(
                    "🔊 Pronunciation",
                    callback_data=f"wa:{session_id}:pron",
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
                    callback_data=f"wa:{session_id}:meanings",
                ),
                InlineKeyboardButton(
                    "📝 Examples",
                    callback_data=f"wa:{session_id}:examples",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔗 Collocations",
                    callback_data=f"wa:{session_id}:collocations",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"wa:{session_id}:back",
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
                    callback_data=f"wa:{session_id}:syn",
                ),
                InlineKeyboardButton(
                    "🔻 Antonyms",
                    callback_data=f"wa:{session_id}:ant",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🟰 Homophones",
                    callback_data=f"wa:{session_id}:homo",
                ),
                InlineKeyboardButton(
                    "✍️ Similar Spelling",
                    callback_data=f"wa:{session_id}:similar",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧩 Word Family",
                    callback_data=f"wa:{session_id}:family",
                ),
                InlineKeyboardButton(
                    "🌱 Root",
                    callback_data=f"wa:{session_id}:root",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"wa:{session_id}:back",
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
                    callback_data=f"wa:{session_id}:etymology",
                ),
                InlineKeyboardButton(
                    "📊 CEFR Level",
                    callback_data=f"wa:{session_id}:cefr",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚖️ Usage & Register",
                    callback_data=f"wa:{session_id}:usage",
                ),
                InlineKeyboardButton(
                    "🧠 Grammar Patterns",
                    callback_data=f"wa:{session_id}:grammar",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"wa:{session_id}:back",
                ),
            ],
        ]
    )


def _pron_keyboard(session_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔊 Full Pronunciation",
                    callback_data=f"wa:{session_id}:pron_result",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗣️ YouGlish",
                    url=_dictionary_links(
                        _sessions_by_session_id(session_id)
                    )[2],
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"wa:{session_id}:back",
                ),
            ],
        ]
    )


def _sessions_by_session_id(session_id):
    for session in _sessions.values():
        if session.get("session_id") == session_id:
            return session.get("word", "")

    return ""


# ============================================================
# MENU TEXTS
# ============================================================

def _menu_text(title, word):
    return (
        f"🔎 <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Choose what you want to know about "
        f"<b>{html.escape(word)}</b>:"
    )


# ============================================================
# RESULT: MEANINGS
# ============================================================

async def _send_meanings(message, word, data):
    meanings = _get_meanings(data)

    if not meanings:
        await message.reply_text("ℹ️ No reliable data available.")
        return

    lines = [
        f"📖 <b>Meanings — {html.escape(word)}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for i, item in enumerate(meanings[:8], 1):
        pos = item["pos"] or "word"
        definition = item["definition"]

        lines.append(
            f"\n<b>{i}. {html.escape(pos)}</b>\n"
            f"{html.escape(definition)}"
        )

    lines.append("\n\n📚 Source: Dictionary API")

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: EXAMPLES
# ============================================================

async def _send_examples(message, word, data):
    meanings = _get_meanings(data)

    examples = []

    for item in meanings:
        if item.get("example"):
            examples.append(
                (
                    item.get("pos", "word"),
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

    lines.append("\n\n📚 Source: Dictionary API")

    await message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: SYNONYMS
# ============================================================

async def _send_synonyms(message, word, data):
    items = _get_synonyms(data)

    if not items:
        await message.reply_text("ℹ️ No reliable data available.")
        return

    text = (
        f"🔄 <b>Synonyms — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in items)
        + "\n\n📚 Sources: WordNet / Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: ANTONYMS
# ============================================================

async def _send_antonyms(message, word, data):
    items = _get_antonyms(data)

    if not items:
        await message.reply_text("ℹ️ No reliable data available.")
        return

    text = (
        f"🔻 <b>Antonyms — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in items)
        + "\n\n📚 Sources: WordNet / Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: SIMILAR SPELLING
# ============================================================

async def _send_similar(message, word, data):
    items = []

    for item in data.get("similar_spelling", []):
        candidate = item.get("word", "").strip()

        if candidate.lower() == word.lower():
            continue

        # Avoid very long unrelated phrases.
        if len(candidate) > max(20, len(word) + 8):
            continue

        items.append(candidate)

    items = _unique(items, 12)

    if not items:
        await message.reply_text("ℹ️ No reliable data available.")
        return

    text = (
        f"✍️ <b>Similar Spelling — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in items)
        + "\n\n📚 Source: Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: HOMOPHONES
# ============================================================

async def _send_homophones(message, word, data):
    candidates = data.get("sound_alikes", [])

    # Datamuse's "sl" gives sound-alike candidates.
    # We only keep candidates whose returned pronunciation
    # exactly matches the source pronunciation when available.
    source_prons = set()

    for p in data["dictionary"].get("phonetics", []):
        if p.get("text"):
            source_prons.add(
                re.sub(r"[^a-zA-Zəɪʊʌɔɑæɛɒːʃʒθðŋtʃdʒˈˌ]", "", p["text"])
                .lower()
            )

    homophones = []

    for item in candidates:
        candidate = item.get("word", "").strip()
        tags = item.get("tags", [])

        if not candidate or candidate.lower() == word.lower():
            continue

        # If Datamuse provides pronunciation metadata,
        # compare it to the original when possible.
        pron_values = []

        for tag in tags:
            if tag.startswith("pron:"):
                pron_values.append(tag[5:])

        if source_prons and pron_values:
            for p in pron_values:
                clean = re.sub(
                    r"[^a-zA-Zəɪʊʌɔɑæɛɒːʃʒθðŋtʃdʒˈˌ]",
                    "",
                    p,
                ).lower()

                if clean in source_prons:
                    homophones.append(candidate)
                    break

    homophones = _unique(homophones, 10)

    if not homophones:
        await message.reply_text(
            "ℹ️ No reliable homophones available."
        )
        return

    text = (
        f"🟰 <b>Homophones — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in homophones)
        + "\n\n📚 Source: pronunciation data"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: WORD FAMILY
# ============================================================

async def _send_family(message, word, data):
    items = _get_word_family(data)

    if not items:
        await message.reply_text("ℹ️ No reliable word-family data available.")
        return

    text = (
        f"🧩 <b>Word Family — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in items)
        + "\n\n📚 Source: WordNet"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: ROOT / ETYMOLOGY
# ============================================================

async def _send_root(message, word, data):
    etymology = data.get("etymology", "")

    if not etymology:
        await message.reply_text("ℹ️ No reliable root data available.")
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

async def _send_etymology(message, word, data):
    etymology = data.get("etymology", "")

    if not etymology:
        await message.reply_text("ℹ️ No reliable etymology available.")
        return

    text = (
        f"📚 <b>Etymology — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(etymology)}\n\n"
        "Source: Wiktionary"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: COLLocations
# ============================================================

async def _send_collocations(message, word, data):
    # Datamuse's common-word relationships are used only as
    # source data; they are not presented as guaranteed grammar rules.
    related = await _datamuse(word, "rel_trg", max_results=12)

    if not related:
        await message.reply_text(
            "ℹ️ No reliable collocation data available."
        )
        return

    items = _unique(
        [item["word"] for item in related],
        10,
    )

    text = (
        f"🔗 <b>Commonly Related Words — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + ", ".join(html.escape(x) for x in items)
        + "\n\n📚 Source: Datamuse"
    )

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: USAGE & REGISTER
# ============================================================

async def _send_usage(message, word, data):
    meanings = _get_meanings(data)

    if not meanings:
        await message.reply_text("ℹ️ No reliable usage data available.")
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
Mention register only if it can be safely inferred from the supplied data.
""",
        max_tokens=250,
    )

    if not result:
        result = "ℹ️ No additional reliable usage information available."

    await message.reply_text(
        f"⚖️ <b>Usage & Register — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(result)}",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: GRAMMAR PATTERNS
# ============================================================

async def _send_grammar(message, word, data):
    meanings = _get_meanings(data)

    if not meanings:
        await message.reply_text("ℹ️ No reliable grammar data available.")
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
For the English word "{word}", explain only the common grammar
patterns that are directly supported by the supplied dictionary
definitions/examples.

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
        result = "ℹ️ No additional reliable grammar information available."

    await message.reply_text(
        f"🧠 <b>Grammar Patterns — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(result)}",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# RESULT: CEFR
# ============================================================

async def _send_cefr(message, word):
    # No CEFR API is assumed.
    # Never guess a CEFR level.
    await message.reply_text(
        f"📊 <b>CEFR Level — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "ℹ️ No reliable CEFR data available."
        ,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# PRONUNCIATION
# ============================================================

def _phonetic_values(data):
    values = []

    for item in data["dictionary"].get("phonetics", []):
        text = item.get("text", "").strip()

        if text and text not in values:
            values.append(text)

    return values


def _format_pronunciation(word, data):
    phonetics = _phonetic_values(data)

    if phonetics:
        ipa = " / ".join(phonetics[:4])
    else:
        ipa = "Not available from current dictionary data."

    syllable_guess = "Not available from current sources."

    # Do not invent US/UK differences.
    return (
        f"🔊 <b>Pronunciation — {html.escape(word)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🇺🇸 <b>American IPA:</b> {html.escape(ipa)}\n"
        f"🇬🇧 <b>British IPA:</b> {html.escape(ipa)}\n"
        f"🔤 <b>Syllables:</b> {syllable_guess}\n"
        f"📌 <b>Stress:</b> See the IPA above; no separate reliable "
        f"stress data was supplied by the current source.\n"
        f"🔇 <b>Silent letters:</b> Not reliably available.\n\n"
        "💡 The exact American/British distinction is shown only "
        "when reliable dialect-specific data is available."
    )


async def _send_pronunciation(message, word, data):
    await message.reply_text(
        _format_pronunciation(word, data),
        parse_mode=ParseMode.HTML,
    )

    # Audio
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
            communicate.save(audio_path),
            timeout=30,
        )

        with open(audio_path, "rb") as audio_file:
            await message.reply_voice(
                voice=audio_file,
                caption=f"🇺🇸 {word}",
            )

    except Exception:
        pass

    finally:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# ANALYSIS COMMAND
# ============================================================

async def analysis_command(update, context):
    message = update.effective_message

    if not message:
        return

    user = update.effective_user

    if not user:
        return

    if not await _call_approved(user.id):
        await message.reply_text(
            "❌ You are not approved to use this bot."
        )
        return

    try:
        if _get_target_text:
            target = _get_target_text(message)
        else:
            target = ""

        # If helper didn't return anything, use command arguments.
        if not target and context.args:
            target = " ".join(context.args)

        # If replying to a message, use its text.
        if not target and message.reply_to_message:
            target = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )

        word = extract_word(target)

        if not word:
            await message.reply_text(
                "🔎 Please provide a word.\n\n"
                "Example: /analysis charge"
            )
            return

        data = await _collect_data(word)

        if not data:
            await message.reply_text(
                "⚠️ <b>Word not found</b>\n"
                f"I couldn't find reliable dictionary data for: "
                f"<b>{html.escape(word)}</b>",
                parse_mode=ParseMode.HTML,
            )
            return

        # New analysis invalidates only this user's previous session.
        session_id = _create_session(
            user.id,
            word,
            data,
        )

        main_text = await _build_main_analysis(
            word,
            data,
        )

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
        await message.reply_text(
            "⚠️ Something went wrong while analyzing this word."
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def analysis_callback(update, context):
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
    parts = data.split(":", 2)

    if len(parts) != 3 or parts[0] != "wa":
        return

    session_id = parts[1]
    action = parts[2]

    # IMPORTANT:
    # The session is checked using BOTH user ID and session ID.
    # Therefore another user cannot use someone else's analysis.
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
    source_data = session["data"]

    try:

        # ----------------------------------------------------
        # MAIN SECTION MENUS
        # ----------------------------------------------------

        if action == "meaning":
            await query.message.reply_text(
                _menu_text("Meaning & Usage", word),
                parse_mode=ParseMode.HTML,
                reply_markup=_meaning_keyboard(session_id),
            )
            return

        if action == "relations":
            await query.message.reply_text(
                _menu_text("Word Relations", word),
                parse_mode=ParseMode.HTML,
                reply_markup=_relations_keyboard(session_id),
            )
            return

        if action == "deep":
            await query.message.reply_text(
                _menu_text("Deep Analysis", word),
                parse_mode=ParseMode.HTML,
                reply_markup=_deep_keyboard(session_id),
            )
            return

        if action == "pron":
            await query.message.reply_text(
                _menu_text("Pronunciation", word),
                parse_mode=ParseMode.HTML,
                reply_markup=_pron_keyboard(session_id),
            )
            return

        # ----------------------------------------------------
        # BACK
        # ----------------------------------------------------

        if action == "back":
            await query.message.reply_text(
                _menu_text("Word Analysis", word),
                parse_mode=ParseMode.HTML,
                reply_markup=_main_keyboard(
                    word,
                    session_id,
                ),
            )
            return

        # ----------------------------------------------------
        # MEANING & USAGE
        # ----------------------------------------------------

        if action == "meanings":
            await _send_meanings(
                query.message,
                word,
                source_data,
            )
            return

        if action == "examples":
            await _send_examples(
                query.message,
                word,
                source_data,
            )
            return

        if action == "collocations":
            await _send_collocations(
                query.message,
                word,
                source_data,
            )
            return

        # ----------------------------------------------------
        # WORD RELATIONS
        # ----------------------------------------------------

        if action == "syn":
            await _send_synonyms(
                query.message,
                word,
                source_data,
            )
            return

        if action == "ant":
            await _send_antonyms(
                query.message,
                word,
                source_data,
            )
            return

        if action == "homo":
            await _send_homophones(
                query.message,
                word,
                source_data,
            )
            return

        if action == "similar":
            await _send_similar(
                query.message,
                word,
                source_data,
            )
            return

        if action == "family":
            await _send_family(
                query.message,
                word,
                source_data,
            )
            return

        if action == "root":
            await _send_root(
                query.message,
                word,
                source_data,
            )
            return

        # ----------------------------------------------------
        # DEEP ANALYSIS
        # ----------------------------------------------------

        if action == "etymology":
            await _send_etymology(
                query.message,
                word,
                source_data,
            )
            return

        if action == "cefr":
            await _send_cefr(
                query.message,
                word,
            )
            return

        if action == "usage":
            await _send_usage(
                query.message,
                word,
                source_data,
            )
            return

        if action == "grammar":
            await _send_grammar(
                query.message,
                word,
                source_data,
            )
            return

        # ----------------------------------------------------
        # PRONUNCIATION
        # ----------------------------------------------------

        if action == "pron_result":
            await _send_pronunciation(
                query.message,
                word,
                source_data,
            )
            return

    except Exception:
        await query.message.reply_text(
            "⚠️ Something went wrong while getting this information."
    )
