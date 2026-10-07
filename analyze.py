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
# AI backend:
# - Gemini only (GEMINI_API_KEY / GEMINI_API_KEY_2 / GEMINI_API_KEY_3)
#   with round-robin key rotation on timeout / quota / transient errors.
# - ask_groq_func is accepted by configure() for bot.py compatibility
#   but is NOT used for analysis AI calls.
#
# Expected configure() interface:
# configure(ask_groq_func, get_target_text_func, is_approved_func)

import asyncio
import html
import inspect
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode


logger = logging.getLogger(__name__)


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

# Gemini fallback (same env keys as media_transcribe)
GEMINI_TIMEOUT = 25
GEMINI_MAX_RETRIES = 1
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


# ============================================================
# INJECTED FUNCTIONS FROM bot.py
# ============================================================

_ask_groq = None
_get_target_text = None
_is_approved = None

_sessions = {}

# Gemini clients (lazy)
_gemini_key_lock = asyncio.Lock()
_gemini_key_cursor = 0
_gemini_clients = []
_gemini_clients_ready = False


def configure(
    ask_groq_func=None,
    get_target_text_func=None,
    is_approved_func=None,
):
    """
    Called from bot.py.
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
    message = update.effective_message
    if not message:
        return ""

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

    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        target = _safe_word(parts[1])
        if target:
            return target

    reply = message.reply_to_message
    if reply:
        reply_text = (reply.text or reply.caption or "").strip()
        if _is_word_like(reply_text):
            return reply_text

        if len(reply_text.split()) <= 5:
            first = reply_text.strip(".,!?;:\"'()[]{}")
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
                "User-Agent": "FixMyEnglish/1.0 (English learning Telegram bot)"
            },
        )
        with urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="ignore")

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

    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + quote(word)
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

        pos = str(meaning.get("partOfSpeech") or "").strip()
        definitions = []

        for item in meaning.get("definitions", []) or []:
            if not isinstance(item, dict):
                continue

            definition = str(item.get("definition") or "").strip()
            example = str(item.get("example") or "").strip()
            synonyms = item.get("synonyms") or []
            antonyms = item.get("antonyms") or []

            definitions.append({
                "definition": definition,
                "example": example,
                "synonyms": _unique(synonyms),
                "antonyms": _unique(antonyms),
            })

        if definitions:
            meanings.append({
                "part_of_speech": pos,
                "definitions": definitions,
            })

    phonetics = []
    for item in entry.get("phonetics", []) or []:
        if not isinstance(item, dict):
            continue

        text = str(item.get("text") or "").strip()
        audio = str(item.get("audio") or "").strip()

        if text or audio:
            phonetics.append({"text": text, "audio": audio})

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
        return {"synonyms": [], "antonyms": [], "hypernyms": [], "hyponyms": [], "lemmas": []}

    try:
        synsets = wn.synsets(word)
    except Exception:
        return {"synonyms": [], "antonyms": [], "hypernyms": [], "hyponyms": [], "lemmas": []}

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

    url = "https://en.wiktionary.org/w/api.php?" + urlencode({
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "redirects": "1",
        "titles": word,
        "format": "json",
    })

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
# GEMINI FALLBACK
# ============================================================

def _load_gemini_keys():
    keys = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        value = (os.getenv(name) or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def _ensure_gemini_clients():
    global _gemini_clients_ready, _gemini_clients
    if _gemini_clients_ready:
        return _gemini_clients

    _gemini_clients_ready = True
    keys = _load_gemini_keys()
    if not keys:
        logger.error("analyze.py: no GEMINI_API_KEY set; analysis AI disabled")
        return _gemini_clients

    try:
        from google import genai
    except ImportError:
        logger.error("analyze.py: google-genai not installed; analysis AI disabled")
        return _gemini_clients

    for index, key in enumerate(keys, start=1):
        try:
            _gemini_clients.append((f"key{index}", genai.Client(api_key=key)))
        except Exception:
            logger.exception("analyze.py: failed to build Gemini client key%s", index)

    return _gemini_clients


def _gemini_status_code(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attr in ("code", "status_code"):
            value = getattr(current, attr, None)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        status = getattr(current, "status", None)
        if isinstance(status, int):
            return status
        if isinstance(status, str) and status.isdigit():
            return int(status)
        response = getattr(current, "response", None)
        if response is not None:
            response_code = getattr(response, "status_code", None)
            if isinstance(response_code, int):
                return response_code
        current = current.__cause__ or current.__context__
    return None


async def _ask_gemini(prompt, system_prompt, max_tokens=500):
    """Round-robin Gemini call for analysis."""
    from google.genai import types

    clients = _ensure_gemini_clients()
    if not clients:
        logger.error("analyze.py: no Gemini clients available")
        return ""

    global _gemini_key_cursor

    async with _gemini_key_lock:
        start = _gemini_key_cursor % len(clients)
        _gemini_key_cursor = (start + 1) % len(clients)

    model = (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL

    # Some Gemini models need higher token budget for JSON
    safe_tokens = max(int(max_tokens or 500), 256)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.2,
        max_output_tokens=safe_tokens,
    )

    order = list(range(start, len(clients))) + list(range(0, start))
    contents = [prompt]

    for index in order:
        label, client = clients[index]

        for attempt in range(GEMINI_MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=contents,
                        config=config,
                    ),
                    timeout=GEMINI_TIMEOUT,
                )
                text = (getattr(response, "text", None) or "").strip()
                if not text:
                    # Some responses put text in candidates
                    try:
                        candidates = getattr(response, "candidates", None) or []
                        if candidates:
                            parts = getattr(candidates[0].content, "parts", None) or []
                            chunks = []
                            for part in parts:
                                part_text = getattr(part, "text", None)
                                if part_text:
                                    chunks.append(part_text)
                            text = "\n".join(chunks).strip()
                    except Exception:
                        text = ""
                if not text:
                    raise RuntimeError("empty gemini response")
                logger.info("analyze Gemini %s succeeded", label)
                return text

            except asyncio.TimeoutError:
                logger.warning("analyze Gemini %s timeout, trying next key", label)
                break

            except Exception as exc:
                code = _gemini_status_code(exc)
                if code in {408, 429, 500, 502, 503, 504}:
                    if attempt < GEMINI_MAX_RETRIES:
                        await asyncio.sleep(0.7)
                        continue
                    logger.warning(
                        "analyze Gemini %s unavailable (%s), trying next key",
                        label,
                        code,
                    )
                    break
                logger.warning(
                    "analyze Gemini %s failed (%s): %s",
                    label,
                    code,
                    str(exc)[:200],
                )
                break

    return ""


# ============================================================
# AI (Gemini only)
# ============================================================

async def _ai(prompt, max_tokens=500):
    """Gemini-only AI for word analysis."""
    system_prompt = (
        "You are an accurate English-learning assistant for Arabic speakers. "
        "Use established English knowledge only. "
        "Never invent facts. "
        "If a detail is uncertain, omit it rather than guessing. "
        "Return clean plain text only. "
        "Do not use Markdown. "
        "Do not use asterisks. "
        "Do not use Markdown tables."
    )

    gemini_text = await _ask_gemini(prompt, system_prompt, max_tokens=max_tokens)
    if gemini_text:
        return _strip_markdown(gemini_text)

    return ""


# Keep old name used throughout the module
async def _groq(prompt, max_tokens=500):
    return await _ai(prompt, max_tokens=max_tokens)


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
# MAIN ANALYSIS (NEW HIGHLY ORGANIZED VERSION)
# ============================================================

def _dictionary_summary(dictionary):
    if not dictionary:
        return ""
    lines = []
    for meaning in dictionary.get("meanings", [])[:5]:
        pos = meaning.get("part_of_speech", "").strip()
        for definition in meaning.get("definitions", [])[:2]:
            text = definition.get("definition", "").strip()
            if text:
                lines.append(f"{pos}: {text}" if pos else text)
    return "\n".join(lines[:8])


async def _build_main_analysis(word, data):
    dictionary = data.get("dictionary") or {}
    dictionary_text = _dictionary_summary(dictionary)

    # Dictionary API first (reliable, no AI needed)
    pos = ""
    meaning = ""
    example = ""

    if dictionary.get("meanings"):
        first_meanings = dictionary["meanings"][:3]
        pos_parts = []
        for m in first_meanings:
            p = m.get("part_of_speech", "").strip()
            if p:
                pos_parts.append(p)
        pos = ", ".join(_unique(pos_parts))

        for m in dictionary["meanings"]:
            for item in m.get("definitions", []):
                d = item.get("definition", "").strip()
                if d and not meaning:
                    meaning = d
                e = item.get("example", "").strip()
                if e and not example:
                    example = e
                if meaning and example:
                    break
            if meaning and example:
                break

    # Gemini for Arabic + fill gaps only
    prompt = f"""
Analyze the English word "{word}" for an Arabic-speaking English learner.

Known dictionary data:
- Part of speech: {pos or "unknown"}
- English definition: {meaning or "unknown"}
- Example: {example or "unknown"}

Extra dictionary notes:
{dictionary_text or "none"}

Return ONLY valid JSON in this exact structure:
{{
  "part_of_speech": "noun, verb, adjective, adverb, etc. (fill if unknown above)",
  "arabic_meaning": "accurate short Arabic translation",
  "main_meaning": "short clear English definition (keep dictionary one if good)",
  "example": "one natural example sentence (keep dictionary one if good)"
}}
Rules:
- Prefer the known dictionary data when it is good.
- Always provide arabic_meaning if possible.
- Keep explanations simple and practical.
- Do not invent false meanings.
"""

    result = await _groq_json(prompt, max_tokens=350)

    if isinstance(result, dict):
        ai_pos = str(result.get("part_of_speech") or "").strip()
        ai_arabic = str(result.get("arabic_meaning") or "").strip()
        ai_meaning = str(result.get("main_meaning") or "").strip()
        ai_example = str(result.get("example") or "").strip()

        if not pos and ai_pos:
            pos = ai_pos
        if ai_meaning and (not meaning or len(ai_meaning) > 8):
            # Prefer AI meaning only when dictionary is empty or AI adds value
            if not meaning:
                meaning = ai_meaning
        if not example and ai_example:
            example = ai_example
        arabic = ai_arabic
    else:
        arabic = ""

    if not arabic:
        # Second short attempt focused only on Arabic
        ar_prompt = f"""
Translate the English word "{word}" into short accurate Arabic.
If the word has a clear common meaning, give it.
Return ONLY valid JSON: {{"arabic_meaning": "..."}}
"""
        ar_result = await _groq_json(ar_prompt, max_tokens=80)
        if isinstance(ar_result, dict):
            arabic = str(ar_result.get("arabic_meaning") or "").strip()

    if not arabic:
        arabic = "لم تتوفر ترجمة دقيقة"
    if not meaning:
        meaning = "No reliable dictionary information found."

    lines = [
        f"🔎 <b>Analysis:</b> {_html(word)}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if pos:
        lines.append(f"🏷 <b>Type:</b> {_html(pos.capitalize())}")

    lines.append(f"🇩🇿 <b>Arabic:</b> {_html(arabic)}")
    lines.append(f"📖 <b>Meaning:</b> {_html(meaning)}")

    if example:
        lines.append(f"📝 <b>Example:</b> {_html(example)}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("👇 <i>Choose an option below:</i>")

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Meaning & Usage", callback_data=_callback(session_id, "meaning")),
         InlineKeyboardButton("🔗 Relations", callback_data=_callback(session_id, "relations"))],
        [InlineKeyboardButton("🔬 Deep Analysis", callback_data=_callback(session_id, "deep")),
         InlineKeyboardButton("💬 Expressions", callback_data=_callback(session_id, "expressions"))],
        [InlineKeyboardButton("🗣️ Slang & Phrasal", callback_data=_callback(session_id, "slang")),
         InlineKeyboardButton("🔊 Pronunciation", callback_data=_callback(session_id, "pronunciation"))],
    ])


# ============================================================
# SUBMENUS
# ============================================================

def _meaning_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Meanings", callback_data=_callback(session_id, "meaning_meanings")),
         InlineKeyboardButton("📝 Usage", callback_data=_callback(session_id, "meaning_usage"))],
        [InlineKeyboardButton("🔗 Collocations", callback_data=_callback(session_id, "meaning_collocations")),
         InlineKeyboardButton("🎚 Register", callback_data=_callback(session_id, "meaning_register"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


def _relations_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Synonyms", callback_data=_callback(session_id, "synonyms")),
         InlineKeyboardButton("↔️ Antonyms", callback_data=_callback(session_id, "antonyms"))],
        [InlineKeyboardButton("🔊 Homophones", callback_data=_callback(session_id, "homophones")),
         InlineKeyboardButton("✍️ Similar Spelling", callback_data=_callback(session_id, "spelling"))],
        [InlineKeyboardButton("🌳 Word Family", callback_data=_callback(session_id, "family")),
         InlineKeyboardButton("📊 Word Levels", callback_data=_callback(session_id, "levels"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


def _deep_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌱 Root & Etymology", callback_data=_callback(session_id, "root")),
         InlineKeyboardButton("🧩 Word Formation", callback_data=_callback(session_id, "formation"))],
        [InlineKeyboardButton("🧠 Semantic Analysis", callback_data=_callback(session_id, "semantic")),
         InlineKeyboardButton("⚠️ Learner Notes", callback_data=_callback(session_id, "learner_notes"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


def _expressions_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Idioms", callback_data=_callback(session_id, "idioms")),
         InlineKeyboardButton("🧱 Fixed Phrases", callback_data=_callback(session_id, "fixed_phrases"))],
        [InlineKeyboardButton("🔗 Common Collocations", callback_data=_callback(session_id, "expression_collocations"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


def _slang_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗣️ Slang", callback_data=_callback(session_id, "slang_words")),
         InlineKeyboardButton("🔀 Phrasal Verbs", callback_data=_callback(session_id, "phrasal"))],
        [InlineKeyboardButton("💬 Informal Uses", callback_data=_callback(session_id, "informal"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


def _pronunciation_keyboard(session_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇸 US Pronunciation", callback_data=_callback(session_id, "pron_us")),
         InlineKeyboardButton("🇬🇧 UK Pronunciation", callback_data=_callback(session_id, "pron_uk"))],
        [InlineKeyboardButton("🎯 Stress", callback_data=_callback(session_id, "stress")),
         InlineKeyboardButton("🗣️ Tips", callback_data=_callback(session_id, "pron_tips"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=_callback(session_id, "back"))],
    ])


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
        return f"<b>🔄 Synonyms</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable synonyms were found for <b>{_html(word)}</b>."

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return f"<b>🔄 Synonyms — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n\n📚 Sources: WordNet / Datamuse / Dictionary API"


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
        return f"<b>↔️ Antonyms</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable antonyms were found for <b>{_html(word)}</b>."

    arabic = await _arabic_glosses(word, words)
    lines = _list_with_arabic(words, arabic)

    return f"<b>↔️ Antonyms — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n\n📚 Sources: WordNet / Datamuse / Dictionary API"


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

Give useful information specifically about "{word}". Use plain text only. Give Arabic meanings/translations where appropriate. No Markdown tables.
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
        lines.append(f"<b>{number}. {_html(pos)}</b>" if pos else f"<b>{number}.</b>")
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
    words = [x for x in _unique(data.get("sound_alikes", [])) if x.lower() != word.lower()][:15]
    if not words:
        return f"<b>🔊 Homophones — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable homophones were found."
    arabic = await _arabic_glosses(word, words)
    return f"<b>🔊 Homophones — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(_list_with_arabic(words, arabic)) + "\n\n📚 Source: Datamuse"


async def _spelling_result(word, data):
    words = [x for x in _unique(data.get("similar_spelling", [])) if x.lower() != word.lower()][:15]
    if not words:
        return f"<b>✍️ Similar Spelling — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable similar-spelling words were found."
    arabic = await _arabic_glosses(word, words)
    return f"<b>✍️ Similar Spelling — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(_list_with_arabic(words, arabic)) + "\n\n📚 Source: Datamuse"


# ============================================================
# WORD FAMILY
# ============================================================

async def _family_result(word, data):
    wordnet = data.get("wordnet") or {}
    source_words = [x for x in _unique(wordnet.get("lemmas", []) + wordnet.get("synonyms", [])) if x.lower() != word.lower()][:30]

    prompt = f"""
For the English word "{word}", identify its genuine English word family. Return JSON only:
{{
  "items": [
    {{ "word": "form", "part_of_speech": "noun/verb/adjective/adverb", "arabic": "Arabic meaning" }}
  ]
}}
"""
    data_json = await _groq_json(prompt, max_tokens=600)
    items = []

    if isinstance(data_json, dict):
        for item in data_json.get("items", []) or []:
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
        if pos:
            line += f" — {_html(pos)}"
        if arabic:
            line += f" — {_html(arabic)}"
        lines.append(line)

    return f"<b>🌳 Word Family — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)


# ============================================================
# WORD LEVELS
# ============================================================

async def _levels_result(word, data):
    dictionary_text = _dictionary_summary(data.get("dictionary") or {})
    prompt = f"""
Estimate the CEFR level of the English word "{word}". Return JSON only:
{{ "level": "A1/A2/B1/B2/C1/C2/unknown", "confidence": "high/medium/low", "note": "short explanation" }}
"""
    result = await _groq_json(prompt, max_tokens=300)

    if not isinstance(result, dict):
        return f"<b>📊 Word Level — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable CEFR level was found."

    level = str(result.get("level") or "").strip()
    if level.lower() == "unknown" or level not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
        return f"<b>📊 Word Level — {_html(word)}</b>\n━━━━━━━━━━━━━━━━━━\nNo reliable CEFR level was found."

    lines = [f"<b>📊 Word Level — {_html(word)}</b>", "━━━━━━━━━━━━━━━━━━", f"• <b>CEFR:</b> {_html(level)}"]
    if result.get("confidence"):
        lines.append(f"• <b>Confidence:</b> {_html(result.get('confidence'))}")
    if result.get("note"):
        lines.append(f"• {_html(result.get('note'))}")

    return "\n".join(lines)


# ============================================================
# PRONUNCIATION
# ============================================================

async def _pronunciation_result(word, data, variant="both"):
    dictionary = data.get("dictionary") or {}
    phonetic = dictionary.get("phonetic", "").strip()
    texts = _unique([str(item.get("text") or "").strip() for item in dictionary.get("phonetics", []) if str(item.get("text") or "").strip()])

    prompt_variant = (
        "American English pronunciation"
        if variant == "us"
        else "British English pronunciation"
        if variant == "uk"
        else "American and British English pronunciation"
    )
    prompt = f"""
Give reliable pronunciation for "{word}" in {prompt_variant}. Return JSON only:
{{ "us": "IPA or unknown", "uk": "IPA or unknown", "stress": "short stress description", "note": "short pronunciation note" }}
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
        if stress:
            lines.append(f"🎯 <b>Stress:</b> {_html(stress)}")
        if note:
            lines.append(f"💡 {_html(note)}")

    if len(lines) == 2:
        if phonetic:
            lines.append(f"• {_html(phonetic)}")
        elif texts:
            for text in texts[:2]:
                lines.append(f"• {_html(text)}")

    if len(lines) == 2:
        lines.append(_html(NO_INFO))
    return "\n".join(lines)


# ============================================================
# GENERIC FINAL SECTIONS
# ============================================================

async def _usage_result(word, data):
    return await _ai_section(
        word,
        "📝 Usage",
        "Explain how native speakers commonly use this word. Give 2–3 natural example sentences and include Arabic translations.",
        data,
        max_tokens=600,
    )


async def _collocations_result(word, data):
    return await _ai_section(
        word,
        "🔗 Collocations",
        "Give the most common natural collocations with this word. Group them briefly when useful. Give Arabic meanings.",
        data,
        max_tokens=600,
    )


async def _register_result(word, data):
    return await _ai_section(
        word,
        "🎚 Register",
        "Explain whether this word is neutral, formal, informal, or slang. Explain contexts and give Arabic clarification.",
        data,
        max_tokens=450,
    )


async def _root_result(word, data):
    return await _ai_section(
        word,
        "🌱 Root & Etymology",
        "Explain the reliable etymology/root. Include Arabic explanation.",
        data,
        max_tokens=550,
    )


async def _formation_result(word, data):
    return await _ai_section(
        word,
        "🧩 Word Formation",
        "Explain how this word is formed (prefix, suffix, root). Give Arabic explanation.",
        data,
        max_tokens=500,
    )


async def _semantic_result(word, data):
    return await _ai_section(
        word,
        "🧠 Semantic Analysis",
        "Explain semantic differences between main meanings. Give short examples and Arabic clarification.",
        data,
        max_tokens=650,
    )


async def _learner_notes_result(word, data):
    return await _ai_section(
        word,
        "⚠️ Learner Notes",
        "Give the most useful learner warnings (common mistakes, confusing words). Give Arabic clarification.",
        data,
        max_tokens=550,
    )


async def _idioms_result(word, data):
    return await _ai_section(
        word,
        "💬 Idioms",
        "List common English idioms containing this word. Give Arabic meanings and short examples.",
        data,
        max_tokens=600,
    )


async def _fixed_phrases_result(word, data):
    return await _ai_section(
        word,
        "🧱 Fixed Phrases",
        "Give common fixed phrases containing this word. Include Arabic meanings and examples.",
        data,
        max_tokens=600,
    )


async def _expression_collocations_result(word, data):
    return await _collocations_result(word, data)


async def _slang_result(word, data):
    return await _ai_section(
        word,
        "🗣️ Slang",
        "Identify genuine slang meanings. Distinguish from informal. Give Arabic meanings.",
        data,
        max_tokens=550,
    )


async def _phrasal_result(word, data):
    return await _ai_section(
        word,
        "🔀 Phrasal Verbs",
        "List genuine common phrasal verbs formed with this word. Give Arabic translation and examples.",
        data,
        max_tokens=600,
    )


async def _informal_result(word, data):
    return await _ai_section(
        word,
        "💬 Informal Uses",
        "Explain genuine informal uses differing from neutral. Give Arabic meanings and examples.",
        data,
        max_tokens=550,
    )


async def _stress_result(word, data):
    return await _ai_section(
        word,
        "🎯 Stress",
        "Explain word stress. If it changes between noun/verb, explain. Give IPA only when reliable. Include Arabic explanation.",
        data,
        max_tokens=450,
    )


async def _pron_tips_result(word, data):
    return await _ai_section(
        word,
        "🗣️ Pronunciation Tips",
        "Give useful pronunciation tips (silent letters, connected speech). Include Arabic explanation.",
        data,
        max_tokens=500,
    )


# ============================================================
# RESULT ROUTER
# ============================================================

async def _final_result(action, word, data):
    if action == "meaning_meanings":
        return await _meaning_result(word, data)
    if action == "meaning_usage":
        return await _usage_result(word, data)
    if action == "meaning_collocations":
        return await _collocations_result(word, data)
    if action == "meaning_register":
        return await _register_result(word, data)
    if action == "synonyms":
        return await _synonyms_result(word, data)
    if action == "antonyms":
        return await _antonyms_result(word, data)
    if action == "homophones":
        return await _homophones_result(word, data)
    if action == "spelling":
        return await _spelling_result(word, data)
    if action == "family":
        return await _family_result(word, data)
    if action == "levels":
        return await _levels_result(word, data)
    if action == "root":
        return await _root_result(word, data)
    if action == "formation":
        return await _formation_result(word, data)
    if action == "semantic":
        return await _semantic_result(word, data)
    if action == "learner_notes":
        return await _learner_notes_result(word, data)
    if action == "idioms":
        return await _idioms_result(word, data)
    if action == "fixed_phrases":
        return await _fixed_phrases_result(word, data)
    if action == "expression_collocations":
        return await _expression_collocations_result(word, data)
    if action == "slang_words":
        return await _slang_result(word, data)
    if action == "phrasal":
        return await _phrasal_result(word, data)
    if action == "informal":
        return await _informal_result(word, data)
    if action == "pron_us":
        return await _pronunciation_result(word, data, "us")
    if action == "pron_uk":
        return await _pronunciation_result(word, data, "uk")
    if action == "stress":
        return await _stress_result(word, data)
    if action == "pron_tips":
        return await _pron_tips_result(word, data)

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
        await update.effective_message.reply_text(
            "🔎 Please use a word or a short expression for Word Analysis."
        )
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
        await query.edit_message_reply_markup(reply_markup=_main_keyboard(session_id))
    except Exception:
        pass
