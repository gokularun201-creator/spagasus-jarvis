"""SPAGASUS JARVIS - configuration.

Loads settings from environment variables or a .env file in the project
root. Secret keys are never hardcoded and never shipped to the frontend.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# --- server ---------------------------------------------------------
# SPAGASUS_-prefixed names only: generic PORT/HOST are often set globally
# on this machine (PORT=60399) and would hijack the server port.
HOST = _env("SPAGASUS_HOST", "0.0.0.0")
PORT = int(_env("SPAGASUS_PORT", "8790"))

# --- security -------------------------------------------------------
# Token the mobile companion must present. Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(24))"
PHONE_TOKEN = _env("PHONE_TOKEN", "spagasus-local-dev-token")

# Lock screen PINs: default, per-device PINs, and mapping helper
PHONE_UNLOCK_PIN = _env("PHONE_UNLOCK_PIN", "1234")
PHONE_UNLOCK_PIN_2 = _env("PHONE_UNLOCK_PIN_2", _env("PHONE_UNLOCK_PIN_SECOND", "1234"))


def get_phone_pin(phone_name_or_serial: str = "") -> str:
    """Return the configured PIN for a specific phone (defaults to '1234')."""
    target = (phone_name_or_serial or "").lower()
    if "second" in target or "2" in target:
        return PHONE_UNLOCK_PIN_2 or PHONE_UNLOCK_PIN or "1234"
    if "first" in target or "1" in target:
        return PHONE_UNLOCK_PIN or "1234"
    try:
        import json
        mapping = json.loads(_env("PHONE_PINS", "{}"))
        for k, v in mapping.items():
            if k.lower() in target:
                return str(v)
    except Exception:
        pass
    return PHONE_UNLOCK_PIN or "1234"

# --- automation -----------------------------------------------------
# Full voice automation: dangerous actions (shutdown, restart, delete
# file, run command) execute immediately instead of asking for a yes/no
# confirmation. Set SPAGASUS_AUTOMATION=false to go back to asking.
AUTOMATION = _env("SPAGASUS_AUTOMATION", "true").lower() in ("1", "true", "yes", "on")

# --- AI adapters (optional - the assistant runs without them) -------
# OpenAI-compatible chat completions endpoint (function-calling capable)
LLM_API_KEY = _env("LLM_API_KEY")
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")

# --- Local AI (Ollama / Gemma 4 12B) --------------------------------
# When LOCAL_LLM_ENABLED is True, queries are routed locally to Gemma
# first. If Ollama is offline or busy, it gracefully falls back to the
# cloud LLM.
LOCAL_LLM_ENABLED = _env("LOCAL_LLM_ENABLED", "1") in ("1", "true", "yes", "on")
LOCAL_LLM_BASE_URL = _env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LOCAL_LLM_MODEL = _env("LOCAL_LLM_MODEL", "gemma4:12b")
LOCAL_LLM_FALLBACK = _env("LOCAL_LLM_FALLBACK", "1") in ("1", "true", "yes", "on")


# Vision: POST a screenshot PNG to this URL, expects {"description": "..."}
VISION_API_URL = _env("VISION_API_URL")

# --- Wispr Flow (Natural Speech Dictation & Flow Processing) -------
WISPR_FLOW_ENABLED = _env("WISPR_FLOW_ENABLED", "1") == "1"
WISPR_FLOW_API_KEY = _env("WISPR_FLOW_API_KEY")
WISPR_FLOW_ENDPOINT = _env("WISPR_FLOW_ENDPOINT", "https://api.openai.com/v1/audio/transcriptions")

# --- speech ---------------------------------------------------------
STT_ENGINE = _env("STT_ENGINE", "auto")     # auto | parakeet | whisper | google
STT_LANGUAGE = _env("STT_LANGUAGE", "en-IN") # en-IN | en-US
WHISPER_MODEL = _env("WHISPER_MODEL", "small")  # tiny|base|small (downloaded on demand)
TTS_VOICE = _env("TTS_VOICE", "en-US-ChristopherNeural")
# Parakeet v2 (NVIDIA) runs via sherpa-onnx; model can be in project or installed AppData
MODELS_DIR = BASE_DIR / "models"
_APP_DATA_MODELS = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "SpagasusJarvis" / "models"
if not (MODELS_DIR / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8").exists() and (_APP_DATA_MODELS / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8").exists():
    MODELS_DIR = _APP_DATA_MODELS
MODELS_DIR.mkdir(parents=True, exist_ok=True)
PARAKEET_MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
# Real-time spectral noise gate on the mic - only your voice gets through.
# Set NOISE_CANCEL=0 to disable.
NOISE_CANCEL = _env("NOISE_CANCEL", "1") == "1"

# --- lifecycle ------------------------------------------------------
# When the dashboard tab/window closes, keep JARVIS running in the
# background (wake word stays armed, localhost keeps answering). Set
# EXIT_ON_CLOSE=1 to restore the old behavior of shutting down with it.
EXIT_ON_CLOSE = _env("EXIT_ON_CLOSE", "0") == "1"

# --- paths ----------------------------------------------------------
DB_PATH = BASE_DIR / "database" / "spagasus.db"
SCREENSHOTS_DIR = BASE_DIR / "database" / "screenshots"
DATA_DIR = BASE_DIR / "database"
for _d in (DB_PATH.parent, SCREENSHOTS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Audio streams use 16 kHz mono
SAMPLE_RATE = 16000
