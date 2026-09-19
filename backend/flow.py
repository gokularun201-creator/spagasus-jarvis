"""SPAGASUS JARVIS - Wispr Flow Speech Engine.

Provides AI-powered natural speech dictation:
  - Real-time filler word removal (um, uh, ah, like, you know, etc.)
  - Self-correction resolution (e.g. 'open chrome no wait open youtube' -> 'open youtube')
  - Command stutter de-duplication and wake prefix cleanup
  - Fast cloud Wispr Flow / Whisper API client with offline fallback
"""
from __future__ import annotations

import io
import json
import re
import urllib.request
import wave
from pathlib import Path

from .config import SAMPLE_RATE, WISPR_FLOW_API_KEY, WISPR_FLOW_ENABLED, WISPR_FLOW_ENDPOINT

# Common filler words and speech hesitation phrases
_FILLER_PATTERNS = [
    r"\b(um+h*|uh+h*|ah+h*|er+h*|eh+h*)\b",
    r"\b(you know|i mean|sort of|kind of|basically|actually|let'?s see)\b",
    r"\b(like\s+so|so\s+yeah|just\s+like)\b",
]

# Self-correction trigger patterns (the speaker correcting themselves mid-sentence)
_CORRECTION_PATTERNS = [
    r"^.*?\b(?:no wait|wait no|no actually|actually no|sorry no|no sorry|i mean|not that)\s+(.+)$",
    r"^.*?\b(?:wait|sorry|no)\s*,\s*(.+)$",
]


def clean_flow_speech(text: str) -> str:
    """Wispr Flow natural speech cleaner.

    Transforms conversational, hesitated, or self-corrected speech into
    clean, actionable, and polished commands.
    """
    if not text:
        return ""

    cleaned = text.strip()

    # 1. Resolve self-corrections (e.g. "open chrome no wait open youtube" -> "open youtube")
    for pattern in _CORRECTION_PATTERNS:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
            break

    # 2. Remove filler words (um, uh, ah, you know, etc.)
    for pattern in _FILLER_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # 3. Remove repeated words / stutters (e.g. "open open chrome" -> "open chrome")
    cleaned = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)

    # 4. Fix common speech-to-text phonetic mishearings and accent variants
    phonetic_fixes = [
        (r"\b(?:hey\s+|hi\s+|ok\s+|okay\s+|hello\s+)?(darlis|darvis|johnny|johnnys|johnny\'s|davis|service|travis|harvest|starfish|spagasus|pegasus|spagas|bagas|jarvis|jarvees|jarv)\b", "Jarvis"),
        (r"\b(on\s*lock|un\s*lock|and\s*lock|in\s*lock|unblock|unlocked|unlocking)\b", "unlock"),
        (r"\b(wake\s*up|wakeup|wake\s*the|waking)\b", "wake up"),
        (r"\b(second\s+mobile|2nd\s+mobile|second\s+phone|2nd\s+phone|second\s+one)\b", "second mobile"),
        (r"\b(first\s+mobile|1st\s+mobile|first\s+phone|1st\s+phone|first\s+one)\b", "first mobile"),
        (r"\b(open\s+ut|open\s+utip|open\s+utape|open\s+u\s*tube|open\s+you\s*tube)\b", "open YouTube"),
        (r"\b(u\s*tape|utape|you\s*tube|u\s*tube|yutube|utip)\b", "YouTube"),
        (r"\b(what\s*app|whatsup|watsp|watsapp|what\s+zap)\b", "WhatsApp"),
        (r"\b(chorme|chrom|crome)\b", "Chrome"),
        (r"\b(notepade|not pad)\b", "Notepad"),
        (r"\b(calculater)\b", "Calculator"),
        (r"\b(one\s+two\s+three\s+four|1\s*2\s*3\s*4)\b", "1234"),
        (r"\b(five\s+four\s+four\s+four|5\s*4\s*4\s*4)\b", "5444"),
    ]
    for pattern, repl in phonetic_fixes:
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)

    # 5. Clean up repeated wake words
    if not re.fullmatch(r"(?:(?:hey|ok|okay|hi|hello)?\s*(?:spagasus\s+)?jarvis)", cleaned, flags=re.IGNORECASE):
        cleaned = re.sub(
            r"^(?:(?:hey|ok|okay|hi|hello)?\s*(?:spagasus\s+)?jarvis\s*[, .]*)+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    # 6. Clean up whitespace and punctuation
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,;.!?])", r"\1", cleaned)
    cleaned = cleaned.strip(" ,.-")

    # Capitalize the first letter if clean text exists
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]

    return cleaned


def recognize_wispr_flow(raw_pcm: bytes) -> str | None:
    """Transcribe audio with Wispr Flow / Whisper API if configured."""
    if not WISPR_FLOW_API_KEY or not WISPR_FLOW_ENABLED or not raw_pcm:
        return None

    try:
        # Build in-memory WAV
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(raw_pcm)
        wav_bytes = buf.getvalue()

        # Multipart form data
        boundary = "----WisprFlowFormBoundary" + str(int(SAMPLE_RATE))
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n')
        body.extend(b"Content-Type: audio/wav\r\n\r\n")
        body.extend(wav_bytes)
        body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        body.extend(b"whisper-1\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(b'Content-Disposition: form-data; name="response_format"\r\n\r\n')
        body.extend(b"json\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            WISPR_FLOW_ENDPOINT,
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {WISPR_FLOW_API_KEY}",
                "User-Agent": "Spagasus-Jarvis-WisprFlow/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))

        text = data.get("text", "").strip()
        if text:
            cleaned = clean_flow_speech(text)
            print(f"[flow] Wispr Flow API transcribed: {text!r} -> {cleaned!r}", flush=True)
            return cleaned
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[flow] Wispr Flow API error ({exc}) - using local STT engine", flush=True)
        return None


def wispr_status() -> dict:
    """Return current Wispr Flow engine status."""
    return {
        "enabled": WISPR_FLOW_ENABLED,
        "has_api_key": bool(WISPR_FLOW_API_KEY),
        "endpoint": WISPR_FLOW_ENDPOINT,
        "cleaner_active": True,
    }
