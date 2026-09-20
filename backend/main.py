"""SPAGASUS JARVIS - API server (FastAPI + WebSocket).

Endpoints:
  GET  /              dashboard (desktop UI)
  GET  /m             mobile companion app
  GET  /api/status    current status, stats, devices, recent actions
  POST /api/command   run a command (text) - auth: ?token=
  GET  /api/memory    saved facts + recent turns
  GET  /api/routines  list routines
  POST /api/routines  create routine
  PATCH /api/routines/{id}  enable/disable
  DELETE /api/routines/{id}
  GET  /api/devices   connected devices
  WS   /ws?token=     realtime feed (status, stats, devices, transcript)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import memory, speech, tools
from .config import BASE_DIR, EXIT_ON_CLOSE, PHONE_TOKEN, TTS_VOICE
from .core import Core, STATUS_STANDBY, STATUS_THINKING
from .routines import RoutineScheduler

app = FastAPI(title="SPAGASUS JARVIS", version="1.0.0")
# Wide-open CORS so the Android companion app (a Capacitor WebView served
# from https://localhost) can call this backend directly over the LAN.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
core = Core()


def _adb_keepalive() -> None:
    """Background thread: keep every phone attached to ADB automatically.

    Wireless-debugging drops are common (Wi-Fi blips, the phone re-enabling
    wireless debugging, which changes its port) - so this runs a first pass
    right at startup and then every 30s:
      * nothing attached  -> try remembered addresses, then mDNS discovery
        (the phone advertises its CURRENT address, so stale ports heal);
      * fewer attached than remembered -> same re-attach pass;
    and keeps the dashboard's device list in sync with the named phones.
    """
    while True:
        try:
            attached = tools.adb_devices()
            remembered = tools._load_adb_addrs()
            # If a phone was connected before, keep probing remembered
            # addresses and mDNS so Android wireless-debugging port changes
            # heal automatically after Wi-Fi/debugging comes back.
            if remembered or not attached:
                attached = tools._ensure_phones()
            for serial in attached:
                memory.upsert_device(tools._phone_name_of(serial), "phone")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


threading.Thread(target=_adb_keepalive, name="adb-keepalive", daemon=True).start()


def _level_stream() -> None:
    """Push the live mic level ~10x/sec for voice reactivity."""
    last_lvl = -1.0
    while True:
        try:
            lvl = core.mic.level
            if abs(lvl - last_lvl) > 0.005 or lvl > 0.02:
                last_lvl = lvl
                broadcast({"type": "level", "level": lvl,
                           "ambient": core.mic.ambient})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)


threading.Thread(target=_level_stream, name="level-stream", daemon=True).start()


# ---------------------------------------------------------------------
# realtime broadcast
# ---------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, str] = {}   # ws -> kind (pc | phone)
        self.lock = threading.Lock()

    async def connect(self, ws: WebSocket, kind: str) -> None:
        await ws.accept()
        with self.lock:
            self.clients[ws] = kind

    def disconnect(self, ws: WebSocket) -> None:
        with self.lock:
            self.clients.pop(ws, None)

    def send(self, payload: dict, kind: str | None = None) -> int:
        """Broadcast; kind=None sends to everyone. Returns how many got it."""
        if _loop is None or not self.clients:
            return 0
        data = json.dumps(payload)
        with self.lock:
            targets = [ws for ws, k in self.clients.items() if kind is None or k == kind]
        for ws in targets:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(data), _loop)
            except Exception:  # noqa: BLE001
                pass
        return len(targets)


hub = Hub()
_loop = None


def broadcast(payload: dict) -> None:
    hub.send(payload)


def on_state_change(payload: dict) -> None:
    # always ship the full current state so every client stays in sync
    payload["type"] = "state"
    payload["status"] = core.status
    payload["task"] = core.current_task
    payload["stats"] = tools.system_stats()
    payload["devices"] = memory.get_devices()
    payload["turns"] = memory.recent_turns(20)
    payload["actions"] = memory.recent_actions(10)
    payload["level"] = core.mic.level
    payload["ambient"] = core.mic.ambient
    payload["media_busy"] = tools.media_is_playing()
    payload["volume"] = speech.get_playback_volume()
    payload["is_headset"] = speech.find_best_output_device() is not None
    broadcast(payload)


core.broadcast = on_state_change
# live 'you said' caption - pushed the moment speech is recognized, far
# faster than the full state broadcast that follows on the next status change
core.heard = lambda text: broadcast({"type": "heard", "text": text})
# reply-stream events (reply_begin/reply_seg/reply_play/reply_done) go out
# RAW - not wrapped by on_state_change, so ordering and payload stay intact
core.stream_feed = lambda payload: broadcast(payload)


def check_token(token: str | None) -> bool:
    return bool(token) and token == PHONE_TOKEN


# ---------------------------------------------------------------------
# static pages
# ---------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """The dashboard is updated often - never let the browser serve a stale
    app.js/style.css/index.html (a cached old UI silently breaks new
    features like touch gestures)."""
    resp = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path in ("/", "/m"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "frontend" / "index.html")


@app.get("/m")
async def mobile() -> FileResponse:
    return FileResponse(BASE_DIR / "web" / "mobile.html")


@app.get("/keys")
async def keys_dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "web" / "keys.html")


@app.get("/yt/{video_id}")
async def yt_player(video_id: str):
    """Open a YouTube video so it AUTOPLAYS without "Error 153".

    Error 153 happens when youtube.com/embed/... is opened directly in a
    browser tab (as an external app does): no Referer is sent, so YouTube
    refuses the player. A 302 redirect FROM this page sends a proper
    Referer, the browser lands on the real youtube.com/embed/<id> page
    (no local page stays open), and ?autoplay=1&mute=1 starts the video
    immediately (muted autoplay is the only kind Chrome always allows).
    The app then auto-unmutes with a real click - nothing needs touching.

    NOTE: this used to redirect to youtube.com/embed/<id>, but many labels
    (T-Series etc.) disable third-party EMBEDDING on their uploads - that
    gives Error 153 no matter what Referer is sent, it's an owner-side
    embed lock, not a referrer problem. The real /watch page is NOT an
    embed and isn't subject to that restriction, so redirect there instead.
    """
    from fastapi.responses import RedirectResponse
    target = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
    resp = RedirectResponse(target, status_code=302)
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# ---------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> dict:
    _cancel_shutdown_if_back()
    out_dev = speech.find_best_output_device()
    is_headset = out_dev is not None
    return {
        "status": core.status,
        "task": core.current_task,
        "stats": tools.system_stats(),
        "devices": memory.get_devices(),
        "actions": memory.recent_actions(10),
        "turns": memory.recent_turns(20),
        "level": core.mic.level,
        "ambient": core.mic.ambient,
        "media_busy": tools.media_is_playing(),
        "wake_count": core._wake_count,
        "volume": speech.get_playback_volume(),
        "is_headset": is_headset,
    }


class CommandIn(BaseModel):
    text: str


# Closing the dashboard shuts the app down - but a simple refresh must NOT.
# The page sends /api/shutdown on unload; we wait a grace period and only
# really exit if nothing reconnects (a reload reconnects within seconds and
# cancels the shutdown, a true close does not).
_shutdown_at: float | None = None
SHUTDOWN_GRACE = 15.0


@app.post("/api/shutdown")
async def api_shutdown() -> dict:
    global _shutdown_at
    _shutdown_at = time.monotonic()
    return {"ok": True}


# ---------------------------------------------------------------------
# AI provider keys (Gemini / Grok / OpenAI / custom) - dashboard UI at /keys
# ---------------------------------------------------------------------
PROVIDER_PRESETS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "label": "Google Gemini"},
    "grok":   {"base_url": "https://api.x.ai/v1", "model": "grok-4-fast",
               "label": "xAI Grok"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "label": "OpenAI"},
    "wispr":  {"base_url": "https://api.openai.com/v1/audio/transcriptions", "model": "whisper-1",
               "label": "Wispr Flow"},
}


class KeyActivateIn(BaseModel):
    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None


def _write_env_vars(updates: dict[str, str]) -> None:
    """Update (or append) KEY=value lines in .env, preserving everything else."""
    path = BASE_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _test_llm_key(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Live-fire one tiny chat completion so a bad key is caught before
    it's saved and the server restarts on it."""
    import urllib.error
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps({"model": model,
                             "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 5}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
        return True, "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        return False, f"HTTP {exc.code}: {body}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@app.get("/api/flow/status")
def api_flow_status() -> dict:
    from . import flow
    return flow.wispr_status()


@app.get("/api/keys/status")
def api_keys_status() -> dict:
    from . import config
    key = config.LLM_API_KEY
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("set" if key else "")
    provider = "custom"
    for name, preset in PROVIDER_PRESETS.items():
        if preset["base_url"].rstrip("/") in (config.LLM_BASE_URL or "").rstrip("/"):
            provider = name
            break
    return {"configured": bool(key), "provider": provider,
            "base_url": config.LLM_BASE_URL, "model": config.LLM_MODEL,
            "masked_key": masked, "presets": PROVIDER_PRESETS,
            "wispr_flow": config.WISPR_FLOW_ENABLED,
            "wispr_key_set": bool(config.WISPR_FLOW_API_KEY)}


@app.post("/api/keys/activate")
def api_keys_activate(body: KeyActivateIn) -> dict:
    provider = (body.provider or "").strip().lower()
    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "API key required")
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        base_url = (body.base_url or preset["base_url"]).strip()
        model = (body.model or preset["model"]).strip()
    else:
        if not body.base_url:
            raise HTTPException(400, "base_url required for a custom provider")
        base_url = body.base_url.strip()
        model = (body.model or "gpt-4o-mini").strip()

    if provider == "wispr":
        _write_env_vars({
            "WISPR_FLOW_API_KEY": api_key,
            "WISPR_FLOW_ENDPOINT": base_url,
            "WISPR_FLOW_ENABLED": "1",
        })
        def _restart_soon_wispr() -> None:
            time.sleep(1.0)
            os._exit(0)
        threading.Thread(target=_restart_soon_wispr, daemon=True).start()
        return {"ok": True, "provider": provider, "base_url": base_url, "model": model,
                "message": "Wispr Flow voice engine activated! JARVIS is applying it (~5s)."}

    ok, detail = _test_llm_key(base_url, api_key, model)
    if not ok:
        raise HTTPException(400, f"Key test failed - {detail}")

    _write_env_vars({"LLM_API_KEY": api_key, "LLM_BASE_URL": base_url, "LLM_MODEL": model})

    def _restart_soon() -> None:
        time.sleep(1.0)   # let the HTTP response flush first
        # Safety net: don't rely on a supervisor already watching this
        # process (some launchers on this machine run uvicorn directly,
        # with nothing to bring it back if it exits). Spawn a DETACHED
        # delayed launcher that waits for OUR port to actually free up
        # before starting run.py - starting it while we're still alive
        # would just see the port taken and immediately bow out (that's
        # run.py's own single-instance / "already running" guard).
        try:
            import subprocess as _sp
            pyw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
            cmd = (f'"{pyw}" -c "'
                   'import socket,subprocess,time,sys;'
                   'p=8790;'
                   '[time.sleep(0.3) for _ in range(40) '
                   'if not __import__(\'contextlib\').suppress(OSError).__enter__() '
                   'or True]"')
            # simpler & more reliable than a one-liner: write a tiny helper script
            watcher = BASE_DIR / "_post_restart_watch.py"
            watcher.write_text(
                "import socket, subprocess, sys, time\n"
                "for _ in range(50):\n"
                "    try:\n"
                "        socket.create_connection(('127.0.0.1', 8790), timeout=0.5).close()\n"
                "        time.sleep(0.3)\n"
                "    except OSError:\n"
                "        break\n"
                "subprocess.Popen([sys.executable.replace('python.exe','pythonw.exe'), 'run.py'],\n"
                "                 cwd=r'" + str(BASE_DIR) + "')\n",
                encoding="utf-8")
            _sp.Popen([str(pyw), str(watcher)], cwd=BASE_DIR,
                      creationflags=0x00000008 | 0x08000000)  # DETACHED_PROCESS | CREATE_NO_WINDOW
        except Exception as exc:  # noqa: BLE001
            print(f"[keys] could not arm restart watcher: {exc}", flush=True)
        os._exit(0)
    threading.Thread(target=_restart_soon, daemon=True).start()
    return {"ok": True, "provider": provider, "base_url": base_url, "model": model,
            "message": "Key verified and saved. JARVIS is restarting to apply it (~5s)."}


def _cancel_shutdown_if_back() -> None:
    """A dashboard reload reconnects within the grace window -> cancel."""
    global _shutdown_at
    if _shutdown_at is not None and time.monotonic() - _shutdown_at < SHUTDOWN_GRACE:
        _shutdown_at = None


def _shutdown_watcher() -> None:
    global _shutdown_at
    while True:
        if _shutdown_at is not None and time.monotonic() - _shutdown_at >= SHUTDOWN_GRACE:
            if EXIT_ON_CLOSE:
                try:
                    (BASE_DIR / "stop.flag").write_text("1", encoding="utf-8")
                except OSError:
                    pass
                _shutdown_at = None
                time.sleep(0.5)
                os._exit(0)
            else:
                _shutdown_at = None
        time.sleep(1)


@app.post("/api/wake")
async def api_wake() -> dict:
    threading.Thread(target=core.wake, daemon=True).start()
    return {"ok": True}


_text_exec_lock = threading.Lock()


def _run_text_command(text: str) -> None:
    """Typed dashboard commands run off the event loop so the WS keeps
    streaming live status to the dashboard (radar, pings, charge rings)."""
    core._set_status(STATUS_THINKING, "Working on it…")
    try:
        with _text_exec_lock:
            reply = core.execute(text, source="text")
    except Exception as exc:  # noqa: BLE001
        reply = "Something went wrong: " + str(exc)
    memory.add_turn("jarvis", reply)
    # progressive reveal: the typed reply appears at voice pace (no audio
    # for typed commands - just the text itself appearing progressively)
    core._speak_streamed(reply, speak=False)
    on_state_change({})
    core._set_status(STATUS_STANDBY, "")


@app.post("/api/command")
async def api_command(cmd: CommandIn, token: str | None = Query(default=None)) -> JSONResponse:
    if token is not None and not check_token(token):
        raise HTTPException(401, "invalid token")
    source = "phone" if check_token(token) else "text"
    if source == "phone":
        # run off the event loop - core.execute (browser automation, window
        # search, etc.) can take many seconds and was freezing the WHOLE
        # server (mic/status/ws) while it ran. Same fix as the text path.
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, core.execute, cmd.text, "phone")
        memory.add_turn("jarvis", reply)
        on_state_change({})
        return JSONResponse({"reply": reply})
    threading.Thread(target=_run_text_command, args=(cmd.text,), daemon=True).start()
    return JSONResponse({"queued": True, "reply": ""})


@app.post("/api/voice")
async def api_voice(request: Request, token: str | None = Query(default=None),
                    voice: str | None = Query(default=None)) -> JSONResponse:
    """Phone voice mode: raw int16 PCM @16 kHz -> STT -> command -> JARVIS
    reply -> TTS audio. The Android companion records with the Web Audio
    API, downsamples to 16 kHz and POSTs the raw bytes here. The reply is
    returned BOTH as text and as base64 MP3 (edge-tts) so the phone can
    play it aloud; `voice` selects the edge-tts voice (default TTS_VOICE)."""
    if not check_token(token):
        raise HTTPException(401, "invalid token")
    body = await request.body()
    if len(body) < 3200:   # ~0.1 s @16 kHz - too short to be speech
        return JSONResponse({"reply": "I didn't catch that - the clip was too short.", "plain": True})
    from . import speech  # local import: keeps module load fast
    try:
        text = speech.recognize(body)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"reply": "Voice recognition failed: " + str(exc)[:200], "plain": True})
    if not text:
        return JSONResponse({"reply": "I didn't hear anything - try again.", "plain": True})
    try:
        if core.heard:
            core.heard(text)   # show what the phone said on the dashboard too
    except Exception:  # noqa: BLE001
        pass
    reply = core.execute(text, source="phone")
    memory.add_turn("jarvis", reply)
    on_state_change({})
    print(f"[voice] text={text!r} reply={reply[:120]!r}", flush=True)

    # LOW-LATENCY: push the reply text to the phone over its live WebSocket
    # the moment it's ready - the phone shows the words while the audio is
    # still being synthesized below, instead of waiting for the full TTS.
    try:
        broadcast_to_devices("phone", {"type": "reply", "reply": reply,
                                       "stream": True})
    except Exception:  # noqa: BLE001
        pass

    # TTS: synthesize the reply with the selected edge-tts voice, capturing
    # per-word timings so the phone can reveal the text at the same speed
    # the voice speaks it. Failures are surfaced (audio=null + ttsError)
    # instead of silently dropping audio.
    audio: str | None = None
    words: list[dict] | None = None
    tts_error: str | None = None
    selected_voice = (voice or TTS_VOICE).strip()
    try:
        from .speech import edge_tts_stream
        mp3, words = await edge_tts_stream(reply, selected_voice)
        if mp3 and len(mp3) > 500:
            audio = base64.b64encode(mp3).decode("ascii")
        else:
            tts_error = "TTS produced no audio"
    except Exception as exc:  # noqa: BLE001
        tts_error = str(exc)[:160]
    print(f"[voice] ttsError={tts_error!r} audio_bytes={len(audio) if audio else 0} "
          f"words={len(words) if words else 0}", flush=True)
    return JSONResponse({"reply": reply, "text": text, "audio": audio,
                         "words": words, "voice": selected_voice,
                         "ttsError": tts_error})


@app.post("/api/stt")
async def api_stt(request: Request, token: str | None = Query(default=None)) -> JSONResponse:
    """Phone speech-to-text: raw int16 PCM @16 kHz -> text.

    Used by the app's Voice Mode: the phone records and downsamples, POSTs
    the raw bytes here, and the PC transcribes with Parakeet v2 (sherpa-onnx)
    once its model is installed, falling back to faster-whisper, then Google.
    The transcribed text is then answered by the app's OWN chat responder
    (same session/model as the Send button), so Voice Mode behaves like a
    different input method - not a separate agent.
    """
    if not check_token(token):
        raise HTTPException(401, "invalid token")
    body = await request.body()
    if len(body) < 3200:   # ~0.1 s @16 kHz - too short to be speech
        return JSONResponse({"text": ""})
    from . import speech
    try:
        text = speech.recognize(body)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"text": "", "error": str(exc)[:200]})
    print(f"[stt] {len(body)} bytes -> {text!r}", flush=True)
    return JSONResponse({"text": text})


@app.get("/api/stt/model")
async def api_stt_model() -> JSONResponse:
    """Parakeet v2 model status + live install progress.

    The Settings -> Voice page in the app polls this while the download
    is running so it can show a progress bar.
    """
    from . import speech
    st = speech.parakeet_status()
    st["install"] = speech.parakeet_install_state()
    return JSONResponse(st)


@app.post("/api/stt/model/install")
async def api_stt_model_install(token: str | None = Query(default=None)) -> JSONResponse:
    """Start downloading + extracting the Parakeet v2 STT model."""
    if not check_token(token):
        raise HTTPException(401, "invalid token")
    from . import speech
    if speech.parakeet_status()["installed"]:
        return JSONResponse({"ok": True, "installed": True})
    threading.Thread(target=speech.install_parakeet, daemon=True).start()
    return JSONResponse({"ok": True, "downloading": True})


class TtsIn(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/tts")
async def api_tts(body: TtsIn, token: str | None = Query(default=None)) -> JSONResponse:
    """Phone text-to-speech: reply text -> base64 MP3 with the selected
    edge-tts voice. The app's Voice Mode sends the assistant's text reply
    here and plays the returned audio on the phone."""
    if not check_token(token):
        raise HTTPException(401, "invalid token")
    text = (body.text or "").strip()
    if not text:
        return JSONResponse({"audio": None, "ttsError": "empty text"})
    selected_voice = (body.voice or TTS_VOICE).strip()
    audio: str | None = None
    words: list[dict] | None = None
    tts_error: str | None = None
    try:
        from .speech import edge_tts_stream
        mp3, words = await edge_tts_stream(text, selected_voice)
        if mp3 and len(mp3) > 500:
            audio = base64.b64encode(mp3).decode("ascii")
        else:
            tts_error = "TTS produced no audio"
    except Exception as exc:  # noqa: BLE001
        tts_error = str(exc)[:160]
    print(f"[tts] voice={selected_voice} audio_bytes={len(audio) if audio else 0} "
          f"words={len(words) if words else 0} err={tts_error!r}", flush=True)
    return JSONResponse({"audio": audio, "words": words, "voice": selected_voice, "ttsError": tts_error})


@app.get("/api/memory")
async def api_memory(token: str | None = Query(default=None)) -> JSONResponse:
    if token is not None and not check_token(token):
        raise HTTPException(401, "invalid token")
    return JSONResponse({"facts": memory.recall(), "turns": memory.recent_turns(30),
                         "actions": memory.recent_actions(20)})
class RoutineIn(BaseModel):
    name: str
    time: str
    actions: list[str]


@app.get("/api/routines")
async def api_routines() -> list:
    return memory.get_routines()


@app.post("/api/routines")
async def api_routines_create(r: RoutineIn) -> dict:
    try:
        rid = memory.add_routine(r.name, r.time, r.actions)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a routine named {r.name!r} already exists")
    return {"ok": True, "id": rid}


@app.patch("/api/routines/{rid}")
async def api_routines_toggle(rid: int, enabled: bool = Query(default=True)) -> dict:
    memory.set_routine_enabled(rid, enabled)
    return {"ok": True}


@app.delete("/api/routines/{rid}")
async def api_routines_delete(rid: int) -> dict:
    memory.delete_routine(rid)
    return {"ok": True}


@app.get("/api/devices")
async def api_devices() -> list:
    return memory.get_devices()


@app.get("/api/ping")
async def api_ping(token: str = "", battery: int = -1) -> dict:
    if not check_token(token):
        return {"ok": False}
    memory.upsert_device("Android Phone", "phone", battery=battery, cpu=0, mem=0)
    return {"ok": True}


# ---------------------------------------------------------------------
# Enhanced AI Vision & System Action Endpoints
# ---------------------------------------------------------------------

@app.get("/api/vision/analyze")
async def api_vision_analyze(question: str = "Describe what is on this screen in 2 concise sentences.") -> dict:
    """Analyze active screen with Gemini multimodal vision model."""
    from . import vision
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, vision.analyze_screen, question)


class ActionMediaIn(BaseModel):
    action: str


@app.post("/api/action/media")
async def api_action_media(body: ActionMediaIn) -> dict:
    msg = tools.media_control(body.action)
    return {"ok": True, "message": msg}


class ActionBrightnessIn(BaseModel):
    level: int


@app.post("/api/action/brightness")
async def api_action_brightness(body: ActionBrightnessIn) -> dict:
    msg = tools.set_brightness(body.level)
    return {"ok": True, "message": msg}


class ActionWindowIn(BaseModel):
    action: str


@app.post("/api/action/window")
async def api_action_window(body: ActionWindowIn) -> dict:
    msg = tools.window_management(body.action)
    return {"ok": True, "message": msg}


@app.get("/api/timers")
async def api_timers() -> list:
    return tools.get_timers()


class TimerIn(BaseModel):
    seconds: int
    label: str = "Timer"


@app.post("/api/timers")
async def api_timers_create(body: TimerIn) -> dict:
    msg = tools.set_timer(body.seconds, body.label)
    return {"ok": True, "message": msg}


class ActionUnlockPhoneIn(BaseModel):
    phone: str | None = None
    pin: str | None = None


@app.post("/api/action/phone_unlock")
async def api_action_phone_unlock(body: ActionUnlockPhoneIn) -> dict:
    msg = tools.unlock_phone(phone=body.phone, pin=body.pin)
    return {"ok": True, "message": msg}


@app.get("/api/volume")
async def api_get_volume() -> dict:
    vol = speech.get_playback_volume()
    out_dev = speech.find_best_output_device()
    dev_name = "Default Speakers"
    is_headset = False
    if out_dev is not None:
        try:
            import sounddevice as sd
            d = sd.query_devices(out_dev)
            dev_name = d.get("name", "Earbuds")
            is_headset = True
        except Exception:
            pass
    return {"volume": vol, "device": dev_name, "is_headset": is_headset}


class VolumeIn(BaseModel):
    volume: int | None = None
    delta: int | None = None


@app.post("/api/volume")
async def api_set_volume(body: VolumeIn) -> dict:
    cur = speech.get_playback_volume()
    if body.delta is not None:
        new_vol = cur + body.delta
    elif body.volume is not None:
        new_vol = body.volume
    else:
        new_vol = cur
    res_vol = speech.set_playback_volume(new_vol)
    out_dev = speech.find_best_output_device()
    dev_name = "Default Speakers"
    is_headset = False
    if out_dev is not None:
        try:
            import sounddevice as sd
            d = sd.query_devices(out_dev)
            dev_name = d.get("name", "Earbuds")
            is_headset = True
        except Exception:
            pass
    resp = {"type": "volume", "volume": res_vol, "device": dev_name, "is_headset": is_headset}
    broadcast(resp)
    return resp


# ---------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    params0 = dict(ws.query_params)
    kind = "phone" if check_token(params0.get("token")) else "pc"
    if kind == "pc":
        _cancel_shutdown_if_back()
    await hub.connect(ws, kind)
    try:
        params = dict(ws.query_params)
        kind = "phone" if check_token(params.get("token")) else "pc"
        name = params.get("name") or ("Android Phone" if kind == "phone" else tools.system_stats()["host"])
        try:
            battery = int(params.get("battery", "-1"))
        except ValueError:
            battery = -1
        memory.upsert_device(name, kind, battery=battery, cpu=0, mem=0)
        await ws.send_text(json.dumps({
            "type": "state",
            "status": core.status,
            "task": core.current_task,
            "stats": tools.system_stats(),
            "devices": memory.get_devices(),
            "turns": memory.recent_turns(20),
            "actions": memory.recent_actions(10),
            "media_busy": tools.media_is_playing(),
        }))
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "ping":
                # keep-alive also refreshes device status
                try:
                    battery = int(data.get("battery", "-1"))
                except ValueError:
                    battery = -1
                memory.upsert_device(name, kind, battery=battery, cpu=0, mem=0)
                await ws.send_text(json.dumps({"type": "pong"}))
            elif data.get("type") == "command" and kind == "phone":
                reply = core.execute(data.get("text", ""), source="phone")
                memory.add_turn("jarvis", reply)
                on_state_change({})
                await ws.send_text(json.dumps({"type": "reply", "reply": reply}))
    except WebSocketDisconnect:
        hub.disconnect(ws)
        # NOT marking the device offline here: a momentary socket drop (Wi-Fi
        # blip, locked screen suspending the tab) would flicker it offline.
        # The 5-minute grace sweep in memory.upsert_device handles true
        # disappearances instead, so paired phones stay 'online' through
        # brief disconnects and the companion's auto-reconnect.


# ---------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------

def start() -> None:
    import uvicorn

    global _loop
    memory.init_db()
    memory.upsert_device(tools.system_stats()["host"], "pc",
                         battery=tools.system_stats()["battery"])

    def broadcast_to_devices(kind: str, payload: dict) -> int:
        return hub.send(payload, kind=kind)

    core.action_broadcast = broadcast_to_devices

    scheduler = RoutineScheduler(core.execute)
    scheduler.start()

    threading.Thread(target=_shutdown_watcher, daemon=True).start()

    threading.Thread(target=core.run_forever, daemon=True).start()

    # Preload the Parakeet v2 STT model in the background so the app's very
    # first voice turn is fast instead of paying the ~10 s cold-load cost
    # inside the phone's STT timeout.
    from . import speech
    threading.Thread(target=speech.warmup_parakeet, daemon=True).start()
    threading.Thread(target=speech.warmup_pyttsx3, daemon=True).start()

    # periodic device sweep + stats broadcast
    def tick() -> None:
        while True:
            time.sleep(5)
            memory.upsert_device(tools.system_stats()["host"], "pc",
                                 battery=tools.system_stats()["battery"],
                                 cpu=tools.system_stats()["cpu"],
                                 mem=tools.system_stats()["ram"])
            on_state_change({})

    threading.Thread(target=tick, daemon=True).start()

    from .config import HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    start()
