"""Gemini media helpers for FixMyEnglish.

Standalone module. Register with:

    from media_transcribe import register_gemini_media
    register_gemini_media(application)

Does not touch Groq, explain/interpret commands, media_download, or media_play.
Media is never processed unless the user explicitly replies with a trigger.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from telegram import Message, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

HANDLER_GROUP = 50
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_TG_LEN = 4000

VOICE_US = "en-US-AriaNeural"
VOICE_UK = "en-GB-SoniaNeural"
DEFAULT_MODEL = "gemini-2.5-flash"

FAIL_USER = "تعذر تنفيذ العملية حاليًا، حاول مرة أخرى."
NO_TEXT_USER = "لم أجد كتابة واضحة في الصورة."

EXTRACT_TRIGGERS = {"text", "نص"}
US_TRIGGERS = {"امريكي", "أمريكي", "us", "american"}
UK_TRIGGERS = {"بريطاني", "uk", "british"}
PRONOUNCE_TRIGGERS = {"pronounce", "نطق", "قيم", "قيّم", "تقييم"}
EXPAND_TRIGGERS = {"expand", "وسع", "وسّع"}
REWRITE_TRIGGERS = {"rewrite", "اعد الصياغة", "أعد الصياغة"}

ALL_TRIGGERS = (
    EXTRACT_TRIGGERS
    | US_TRIGGERS
    | UK_TRIGGERS
    | PRONOUNCE_TRIGGERS
    | EXPAND_TRIGGERS
    | REWRITE_TRIGGERS
)

_key_lock = asyncio.Lock()
_key_cursor = 0
_clients: list[tuple[str, Any]] = []
_clients_ready = False


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    cleaned = text.strip().casefold()
    cleaned = cleaned.strip(".,!?:;،۔\"'«»()[]{}")
    return " ".join(cleaned.split())


def _load_keys() -> list[str]:
    keys: list[str] = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        value = (os.getenv(name) or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def _ensure_clients() -> list[tuple[str, Any]]:
    global _clients_ready, _clients
    if _clients_ready:
        return _clients
    _clients_ready = True
    keys = _load_keys()
    if not keys:
        logger.error(
            "Gemini media module: no GEMINI_API_KEY / GEMINI_API_KEY_2 / "
            "GEMINI_API_KEY_3 set. Related commands will fail closed."
        )
        return _clients
    try:
        from google import genai
    except ImportError:
        logger.error("google-genai is not installed; Gemini media commands disabled.")
        return _clients
    for index, key in enumerate(keys, start=1):
        try:
            _clients.append((f"key{index}", genai.Client(api_key=key)))
        except Exception:
            logger.exception("Failed to build Gemini client key%s", index)
    if not _clients:
        logger.error("Gemini media module: all client builds failed.")
    return _clients


def _status_code(exc: BaseException) -> int | None:
    seen: set[int] = set()
    current: BaseException | None = exc
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


def _error_blob(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    for attr in ("message", "details", "reason", "status"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        parts.append(type(cause).__name__)
        parts.append(str(cause))
    return " ".join(parts).lower()


def _is_quota_or_transient(exc: BaseException) -> bool:
    """Rotate keys on quota/rate-limit and transient server failures.

    Prefers numeric status and exception class. Text matching is only a fallback.
    """
    code = _status_code(exc)
    if code in {408, 429, 500, 502, 503, 504}:
        return True
    blob = _error_blob(exc)
    if any(name in blob for name in ("servererror", "ratelimit", "toomanyrequests")):
        return True
    markers = (
        "resource_exhausted",
        "resource exhausted",
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "unavailable",
        "overloaded",
        "deadline exceeded",
        "temporarily unavailable",
    )
    return any(marker in blob for marker in markers)


async def gemini_generate(*, contents: list[Any], system_instruction: str) -> str:
    """Round-robin Gemini call. Rotates on quota, rate limit, and transient errors."""
    clients = _ensure_clients()
    if not clients:
        raise RuntimeError("no gemini keys")

    from google.genai import types

    global _key_cursor
    async with _key_lock:
        start = _key_cursor % len(clients)
        _key_cursor = (start + 1) % len(clients)

    model = (os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.2,
    )
    last_error: BaseException | None = None
    order = list(range(start, len(clients))) + list(range(0, start))
    for index in order:
        label, client = clients[index]
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=contents,
                config=config,
            )
            text = (getattr(response, "text", None) or "").strip()
            if not text:
                raise RuntimeError("empty gemini response")
            return text
        except Exception as exc:
            last_error = exc
            if _is_quota_or_transient(exc):
                logger.warning("Gemini %s transient/quota failure, trying next key", label)
                continue
            logger.exception("Gemini %s failed with non-transient error", label)
            break
    raise RuntimeError("gemini failed") from last_error


def _image_part(path: Path, mime: str) -> Any:
    from google.genai import types

    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def _media_part(path: Path, mime: str) -> Any:
    from google.genai import types

    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


async def _download(bot: Any, file_id: str, suffix: str) -> Path:
    tg_file = await bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > MAX_MEDIA_BYTES:
        raise ValueError("too_large")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    path = Path(handle.name)
    await tg_file.download_to_drive(custom_path=str(path))
    if path.stat().st_size > MAX_MEDIA_BYTES:
        path.unlink(missing_ok=True)
        raise ValueError("too_large")
    return path


def _cleanup(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete temp file %s", path)


def _reply_target(message: Message) -> Message | None:
    return message.reply_to_message


def _photo_file(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, ".jpg"
    document = message.document
    if document and document.mime_type and document.mime_type.startswith("image/"):
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(document.mime_type, ".img")
        return document.file_id, ext
    return None


def _audio_file(message: Message) -> tuple[str, str, str] | None:
    if message.voice:
        return message.voice.file_id, ".ogg", "audio/ogg"
    if message.audio:
        mime = message.audio.mime_type or "audio/mpeg"
        ext = ".mp3" if "mpeg" in mime or "mp3" in mime else ".audio"
        return message.audio.file_id, ext, mime
    return None


def _video_file(message: Message) -> tuple[str, str, str] | None:
    if message.video:
        mime = message.video.mime_type or "video/mp4"
        return message.video.file_id, ".mp4", mime
    if message.video_note:
        return message.video_note.file_id, ".mp4", "video/mp4"
    document = message.document
    if document and document.mime_type and document.mime_type.startswith("video/"):
        return document.file_id, ".mp4", document.mime_type
    return None


async def _extract_speech_audio(src: Path) -> Path:
    """Turn a video into a small mono mp3 so Gemini receives audio, not a video container."""
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    handle.close()
    dest = Path(handle.name)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        _cleanup(dest)
        logger.error("ffmpeg audio extract failed: %s", err[-400:].decode("utf-8", "replace"))
        raise RuntimeError("audio_extract_failed")
    return dest


async def _speech_payload(path: Path, mime: str) -> tuple[Path, str, Path | None]:
    if mime.startswith("video/") or path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}:
        audio = await _extract_speech_audio(path)
        return audio, "audio/mpeg", audio
    return path, mime, None


async def _send_chunks(message: Message, title: str, body: str) -> None:
    safe = html.escape(body.strip())
    header = f"<b>{html.escape(title)}</b>\n"
    payload = header + safe
    if len(payload) <= MAX_TG_LEN:
        await message.reply_text(payload, parse_mode="HTML")
        return
    await message.reply_text(header + safe[: MAX_TG_LEN - len(header)], parse_mode="HTML")
    rest = safe[MAX_TG_LEN - len(header) :]
    while rest:
        await message.reply_text(rest[:MAX_TG_LEN], parse_mode="HTML")
        rest = rest[MAX_TG_LEN:]


async def _extract_image_text(path: Path, mime: str) -> str:
    system = (
        "Extract every visible writing in this image only. "
        "Keep paragraph, heading, and list order. Do not translate. "
        "Do not explain. Do not invent words that are not visible. "
        "If there is no clear writing, reply with exactly NO_TEXT."
    )
    return await gemini_generate(
        contents=[_image_part(path, mime), "Extract the visible text."],
        system_instruction=system,
    )


async def _transcribe_media(path: Path, mime: str) -> str:
    system = (
        "Transcribe the audible speech only. Do not translate, summarize, or correct meaning. "
        "Write unclear parts as [غير واضح]. Do not invent speech."
    )
    return await gemini_generate(
        contents=[_media_part(path, mime), "Transcribe the speech."],
        system_instruction=system,
    )


def _mime_for_image(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix.lower(), "image/jpeg")


async def _handle_extract(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _reply_target(message)
    if target is None:
        return
    path: Path | None = None
    try:
        photo = _photo_file(target)
        if photo:
            file_id, suffix = photo
            path = await _download(context.bot, file_id, suffix)
            raw = await _extract_image_text(path, _mime_for_image(suffix))
            if raw.strip() == "NO_TEXT":
                await message.reply_text(NO_TEXT_USER)
                return
            await _send_chunks(message, "النص المستخرج", raw)
            return
        audio = _audio_file(target)
        video = None if audio else _video_file(target)
        media = audio or video
        if not media:
            await message.reply_text("رد بهذا الأمر على صورة أو صوت أو فيديو.")
            return
        file_id, suffix, mime = media
        path = await _download(context.bot, file_id, suffix)
        speech_path, speech_mime, extracted = await _speech_payload(path, mime)
        try:
            raw = await _transcribe_media(speech_path, speech_mime)
        finally:
            if extracted is not None:
                _cleanup(extracted)
        await _send_chunks(message, "النص المستخرج", raw)
    except ValueError as exc:
        if str(exc) == "too_large":
            await message.reply_text("الملف كبير جدًا للمعالجة.")
            return
        raise
    except RuntimeError as exc:
        if str(exc) == "audio_extract_failed":
            await message.reply_text("تعذر استخراج الصوت من الفيديو.")
            return
        raise
    finally:
        _cleanup(path)


async def _handle_read_image(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    voice_name: str,
) -> None:
    target = _reply_target(message)
    photo = _photo_file(target) if target else None
    if not photo:
        await message.reply_text("رد بهذا الأمر على صورة فيها كتابة.")
        return
    image_path: Path | None = None
    audio_path: Path | None = None
    try:
        file_id, suffix = photo
        image_path = await _download(context.bot, file_id, suffix)
        raw = await _extract_image_text(image_path, _mime_for_image(suffix))
        if raw.strip() == "NO_TEXT" or not raw.strip():
            await message.reply_text(NO_TEXT_USER)
            return
        import edge_tts

        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        handle.close()
        audio_path = Path(handle.name)
        communicate = edge_tts.Communicate(raw[:4000], voice_name)
        with audio_path.open("wb") as audio_handle:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    audio_handle.write(chunk["data"])
        if audio_path.stat().st_size == 0:
            raise RuntimeError("edge-tts produced empty audio")
        with audio_path.open("rb") as audio_file:
            await message.reply_voice(voice=audio_file)
    except ValueError as exc:
        if str(exc) == "too_large":
            await message.reply_text("الملف كبير جدًا للمعالجة.")
            return
        raise
    finally:
        _cleanup(image_path)
        _cleanup(audio_path)


async def _handle_pronounce(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _reply_target(message)
    audio = _audio_file(target) if target else None
    video = _video_file(target) if target and not audio else None
    media = audio or video
    if not media:
        await message.reply_text("رد بـ pronounce على تسجيل صوتي.")
        return
    path: Path | None = None
    extracted: Path | None = None
    try:
        file_id, suffix, mime = media
        path = await _download(context.bot, file_id, suffix)
        speech_path, speech_mime, extracted = await _speech_payload(path, mime)
        system = (
            "You are an English pronunciation teacher. Score pronunciation only. "
            "Identify the words the learner said. Report only clear pronunciation errors. "
            "Never invent errors. If the recording is unclear, set unclear true and do not list errors. "
            "Do not focus on grammar unless pronunciation changed the word. "
            "Use IPA for the correct pronunciation when possible. "
            "This is an educational estimate, not a medical or laboratory assessment. "
            "Reply with JSON only, keys: unclear (bool), overall (str), "
            "needs_fix (list of {word, ipa, problem, practice}), good (list of str)."
        )
        raw = await gemini_generate(
            contents=[_media_part(speech_path, speech_mime), "Assess English pronunciation."],
            system_instruction=system,
        )
        await message.reply_text(_format_pronounce(raw), parse_mode="HTML")
    except ValueError as exc:
        if str(exc) == "too_large":
            await message.reply_text("الملف كبير جدًا للمعالجة.")
            return
        raise
    except RuntimeError as exc:
        if str(exc) == "audio_extract_failed":
            await message.reply_text("تعذر استخراج الصوت من الفيديو.")
            return
        raise
    finally:
        _cleanup(extracted)
        _cleanup(path)


def _format_pronounce(raw: str) -> str:
    import json

    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        payload = payload.removeprefix("json").strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        start = payload.find("{")
        end = payload.rfind("}")
        if start == -1 or end == -1:
            return f"<b>تقييم النطق</b>\n{html.escape(raw[:3500])}"
        data = json.loads(payload[start : end + 1])
    if data.get("unclear"):
        return "<b>تقييم النطق</b>\nالصوت غير واضح، لا يمكن تقييم النطق."
    lines = ["<b>تقييم النطق</b>", f"التقييم العام: {html.escape(str(data.get('overall') or ''))}"]
    fixes = data.get("needs_fix") or []
    lines.append("<b>الكلمات التي تحتاج تصحيحًا</b>")
    if not fixes:
        lines.append("لا توجد أخطاء نطق واضحة.")
    for item in fixes:
        if not isinstance(item, dict):
            continue
        lines.append(f"الكلمة: {html.escape(str(item.get('word') or ''))}")
        lines.append(f"النطق الصحيح: {html.escape(str(item.get('ipa') or ''))}")
        lines.append(f"المشكلة: {html.escape(str(item.get('problem') or ''))}")
        lines.append(f"طريقة التدريب: {html.escape(str(item.get('practice') or ''))}")
        lines.append("")
    lines.append("<b>النطق الجيد</b>")
    good = data.get("good") or []
    if not good:
        lines.append("—")
    else:
        lines.append(html.escape("، ".join(str(word) for word in good)))
    lines.append("")
    lines.append("تقييم تعليمي تقريبي، وليس تقييمًا طبيًا.")
    return "\n".join(lines)[:MAX_TG_LEN]


def _source_text(message: Message, triggers: set[str]) -> str | None:
    own = _normalize(message.text or message.caption)
    if own in triggers:
        target = _reply_target(message)
        if target and (target.text or target.caption):
            return (target.text or target.caption or "").strip()
        return None
    raw = (message.text or message.caption or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) >= 2 and _normalize(lines[-1]) in triggers:
        return "\n".join(lines[:-1]).strip()
    return None


async def _handle_expand(message: Message, text: str) -> None:
    system = (
        "Expand the short idea into a clearer, connected text. "
        "Keep the original meaning. Add only related detail. No filler. No unrelated facts. "
        "Write in the same language as the input. Return only the expanded text."
    )
    raw = await gemini_generate(contents=[text], system_instruction=system)
    await _send_chunks(message, "النص الموسّع", raw)


async def _handle_rewrite(message: Message, text: str) -> None:
    system = (
        "Rewrite the text. Improve clarity, wording, and sentence structure. "
        "Keep the same meaning. Do not add new ideas. Do not drop important information. "
        "Do not explain the edits. Write in the same language as the input. "
        "Return only the rewritten text."
    )
    raw = await gemini_generate(contents=[text], system_instruction=system)
    await _send_chunks(message, "إعادة الصياغة", raw)


async def on_gemini_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    trigger = _normalize(message.text or message.caption)
    try:
        if trigger in EXTRACT_TRIGGERS:
            await _handle_extract(message, context)
            return
        if trigger in US_TRIGGERS:
            await _handle_read_image(message, context, VOICE_US)
            return
        if trigger in UK_TRIGGERS:
            await _handle_read_image(message, context, VOICE_UK)
            return
        if trigger in PRONOUNCE_TRIGGERS:
            await _handle_pronounce(message, context)
            return
        expand_text = _source_text(message, EXPAND_TRIGGERS)
        if expand_text and (trigger in EXPAND_TRIGGERS or _normalize((message.text or "").splitlines()[-1]) in EXPAND_TRIGGERS):
            await _handle_expand(message, expand_text)
            return
        rewrite_text = _source_text(message, REWRITE_TRIGGERS)
        if rewrite_text and (
            trigger in REWRITE_TRIGGERS or _normalize((message.text or "").splitlines()[-1]) in REWRITE_TRIGGERS
        ):
            await _handle_rewrite(message, rewrite_text)
            return
    except Exception:
        logger.exception("Gemini media command failed")
        try:
            await message.reply_text(FAIL_USER)
        except Exception:
            logger.exception("Could not send Gemini failure notice")


def register_gemini_media(application: Application) -> None:
    """Attach Gemini media commands without replacing existing handlers."""
    _ensure_clients()
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            on_gemini_command,
        ),
        group=HANDLER_GROUP,
    )
