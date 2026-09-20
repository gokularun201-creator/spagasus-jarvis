"""SPAGASUS JARVIS - assistant core.

Routes natural-language requests to real tools, answers knowledge
questions with an optional LLM (rule fallback), remembers facts, logs
every action, and runs the wake -> listen -> act -> speak loop.
"""
from __future__ import annotations

import datetime
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

from . import memory, tools, vision
from .config import (
    AUTOMATION,
    DATA_DIR,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_ENABLED,
    LOCAL_LLM_FALLBACK,
    LOCAL_LLM_MODEL,
    SAMPLE_RATE,
)
from .speech import MicStream, Tts

# words that follow "call/ring" but are NOT a contact name
_CALL_SKIP = {"this", "that", "it", "me", "you", "him", "her", "them", "us",
              "of", "off", "on", "in", "up", "out", "down", "back", "later",
              "center", "history", "recording", "option", "duty", "the", "my"}

# named phones: "on second mobile" / "on my first phone" -> target phone
_PH_ORD = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
_PH_PHR = r"(?:(?:the|my)\s+)?(?:" + _PH_ORD + r"\s+)?(?:phone|mobile)"


def _phone_target(t: str) -> str | None:
    """Extract the named phone from a command ('on second mobile' ->
    'second mobile'); None means the first/default phone."""
    m = re.search(r"\b(" + _PH_ORD + r")\s+(?:mobile|phone)", t, re.I)
    return (m.group(1).lower() + " mobile") if m else None

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break. Now it won't stop sending me KitKat ads.",
    "Why did the Wi-Fi go to therapy? It had too many connection issues.",
    "There are only 10 types of people: those who understand binary and those who don't.",
    "My love for you is like pi - rational, real, and never ending.",
]

SYSTEM_PROMPT = (
    "You are SPAGASUS JARVIS, an advanced, autonomous AI assistant powered by Gemma 4 12B "
    "running directly on Gokul's Windows PC. You have your own autonomous ideas and full automation authority. "
    "You can hear Gokul through his wireless earbuds mic and speak directly into his earbuds. "
    "When answering questions or suggesting ideas, you can trigger REAL computer automations by appending action tags:\n"
    "- [ACTION:open_app(\"app_name\")] (e.g. 'code', 'chrome', 'spotify', 'notepad')\n"
    "- [ACTION:close_app(\"app_name\")]\n"
    "- [ACTION:play_song(\"song or artist\")] (plays on YouTube/music)\n"
    "- [ACTION:volume(50)] (sets volume 0-100%)\n"
    "- [ACTION:set_brightness(75)] (sets screen brightness 0-100%)\n"
    "- [ACTION:media_control(\"playpause\" | \"next\" | \"prev\" | \"mute\")]\n"
    "- [ACTION:window_management(\"minimize_all\" | \"maximize\" | \"snap_left\" | \"snap_right\")]\n"
    "- [ACTION:clean_temp_files()] (cleans temporary cache & frees memory)\n"
    "- [ACTION:set_timer(seconds=300, label=\"Focus\")]\n"
    "- [ACTION:lock()]\n"
    "- [ACTION:unlock_phone()]\n"
    "- [ACTION:open_site(\"url\")]\n"
    "- [ACTION:run_command(\"powershell_command\")]\n"
    "When Gokul asks for ideas, what to do, or commands an automation, proactively decide what needs doing, "
    "explain your reasoning in 1-2 calm, confident, futuristic sentences, and append the appropriate [ACTION:...] tags. "
    "Keep replies concise and speakable for voice."
)

STATUS_STANDBY = "STANDBY"
STATUS_LISTENING = "LISTENING"
STATUS_THINKING = "THINKING"
STATUS_SPEAKING = "SPEAKING"
STATUS_EXECUTING = "EXECUTING"
STATUS_ERROR = "ERROR"


def time_greeting(now: datetime.datetime | None = None) -> str:
    """Greeting matched to the time of day, not hardcoded."""
    h = (now or datetime.datetime.now()).hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 21:
        return "Good evening"
    return "Good night"


def _map_role(role: str) -> str:
    r = (role or "").lower().strip()
    if r in ("jarvis", "assistant", "model", "bot"):
        return "assistant"
    if r in ("user", "human"):
        return "user"
    if r in ("system",):
        return "system"
    return "assistant"


def _query_chat_completions(base_url: str, model: str, api_key: str | None, messages: list[dict], stream: bool = False, timeout: int = 30):
    """Dispatch an OpenAI-compatible chat completion request."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 400,
    }
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=timeout)


def llm_chat(history: list[dict], user_text: str) -> str | None:
    """Chat completions with Local Gemma 4 / Ollama prioritization and cloud fallback."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": _map_role(h.get("role", "assistant")), "content": h.get("text", "")} for h in history[-8:]]
    messages.append({"role": "user", "content": user_text})

    # 1. Prioritize Local Gemma 4 (Ollama) if enabled
    if LOCAL_LLM_ENABLED:
        try:
            with _query_chat_completions(LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL, None, messages, stream=False, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                msg = (data.get("choices") or [{}])[0].get("message", {})
                content = (msg.get("content") or msg.get("reasoning") or "").strip()
                if content:
                    return content
        except Exception:
            if not LOCAL_LLM_FALLBACK or not LLM_API_KEY:
                pass

    # 2. Cloud LLM (Gemini / OpenAI) fallback
    if LLM_API_KEY:
        try:
            with _query_chat_completions(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, messages, stream=False, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                msg = (data.get("choices") or [{}])[0].get("message", {})
                return (msg.get("content") or msg.get("reasoning") or "").strip()
        except Exception as exc:  # noqa: BLE001
            return f"(LLM unavailable: {exc})"

    return None


def llm_chat_stream(history: list[dict], user_text: str):
    """OpenAI-compatible chat with SSE streaming. Prioritizes local Gemma 4
    with graceful cloud fallback."""
    if not LOCAL_LLM_ENABLED and not LLM_API_KEY:
        return None
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": _map_role(h.get("role", "assistant")), "content": h.get("text", "")} for h in history[-8:]]
    messages.append({"role": "user", "content": user_text})

    def _stream():
        # 1. Try local Gemma 4 (Ollama)
        if LOCAL_LLM_ENABLED:
            try:
                resp = _query_chat_completions(LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL, None, messages, stream=True, timeout=90)
                yielded_any = False
                with resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if not data:
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content") or delta.get("reasoning")
                        if content:
                            yielded_any = True
                            yield content
                if yielded_any:
                    return
            except Exception:
                if not LOCAL_LLM_FALLBACK or not LLM_API_KEY:
                    return

        # 2. Fallback to Cloud LLM (Gemini / OpenAI)
        if LLM_API_KEY:
            try:
                resp = _query_chat_completions(LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, messages, stream=True, timeout=60)
                with resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if not data:
                            continue
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield content
            except Exception:
                return

    return _stream()


def _split_segments(text: str, max_len: int = 140) -> list[str]:
    """Split a reply into short, speakable segments (sentences). The UI
    reveals the text and the TTS speaks it segment by segment, so text and
    voice move together. Long runs without sentence breaks are hard-split."""
    pieces = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    segs: list[str] = []
    cur = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if cur and len(cur) + len(p) + 1 <= max_len:
            cur += " " + p
        else:
            if cur:
                segs.append(cur)
            cur = p
        if len(cur) >= max_len:
            segs.append(cur)
            cur = ""
    if cur:
        segs.append(cur)
    out: list[str] = []
    for s in segs:
        while len(s) > max_len:
            out.append(s[:max_len])
            s = s[max_len:]
        if s:
            out.append(s)
    return out


def execute_embedded_action(action_str: str) -> str:
    """Execute a single parsed action tag such as open_app("chrome") or volume(60)."""
    action_str = action_str.strip()
    m = re.match(r"^([a-zA-Z0-9_]+)\s*\((.*)\)$", action_str, re.DOTALL)
    if not m:
        return f"Unknown action syntax: {action_str}"
    fn = m.group(1).strip()
    raw_args = m.group(2).strip()

    args = []
    kwargs = {}
    if raw_args:
        parts = [p.strip() for p in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw_args)]
        for p in parts:
            if "=" in p and not p.startswith("{"):
                k, _, v = p.partition("=")
                kwargs[k.strip()] = v.strip().strip('"').strip("'")
            else:
                args.append(p.strip().strip('"').strip("'"))

    try:
        if fn == "open_app":
            target = kwargs.get("name") or (args[0] if args else "")
            return tools.open_app(target)
        elif fn == "close_app":
            target = kwargs.get("name") or (args[0] if args else "")
            return tools.close_app(target)
        elif fn == "play_song":
            target = kwargs.get("query") or (args[0] if args else "")
            return tools.play_song(target)
        elif fn == "volume":
            lvl = kwargs.get("level") or (args[0] if args else "50")
            return tools.volume(f"volume {lvl}") or f"Volume set to {lvl}%."
        elif fn == "set_brightness":
            lvl = int(kwargs.get("level") or (args[0] if args else 70))
            return tools.set_brightness(lvl)
        elif fn == "media_control":
            act = kwargs.get("action") or (args[0] if args else "playpause")
            return tools.media_control(act)
        elif fn == "window_management":
            act = kwargs.get("action") or (args[0] if args else "minimize_all")
            return tools.window_management(act)
        elif fn == "clean_temp_files":
            return tools.clean_temp_files()
        elif fn == "set_timer":
            secs = int(kwargs.get("seconds") or (args[0] if args else 300))
            lbl = kwargs.get("label") or (args[1] if len(args) > 1 else "Timer")
            return tools.set_timer(secs, lbl)
        elif fn == "lock":
            return tools.lock()
        elif fn == "unlock_phone":
            return tools.unlock_phone()
        elif fn == "wake_phone":
            return tools.wake_phone()
        elif fn == "open_site":
            url = kwargs.get("url") or (args[0] if args else "")
            return tools.open_site(url) or f"Opened {url}."
        elif fn == "run_command":
            cmd = kwargs.get("cmd") or (args[0] if args else "")
            return tools._run(cmd)
        elif hasattr(tools, fn):
            t_func = getattr(tools, fn)
            return str(t_func(*args, **kwargs))
        return f"Unknown tool: {fn}"
    except Exception as exc:  # noqa: BLE001
        return f"Action {fn} failed: {exc}"


def process_autonomous_actions(reply: str) -> tuple[str, list[tuple[str, str]]]:
    """Extract and execute all [ACTION:...] tags from Gemma's response.
    Returns (cleaned_text_for_speech, list_of_(action, result))."""
    if not reply:
        return reply, []

    pattern = r"\[ACTION:\s*([^\]]+)\]"
    matches = re.findall(pattern, reply)
    results = []
    for action_str in matches:
        res = execute_embedded_action(action_str)
        results.append((action_str, res))
        try:
            memory.audit("autonomous_action", f"{action_str} -> {res}")
        except Exception:
            pass

    cleaned = re.sub(pattern, "", reply).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned, results


class Core:
    def __init__(self) -> None:
        self.mic = MicStream()
        self.tts = Tts()
        self.status = STATUS_STANDBY
        self.current_task = ""
        self.busy = False
        self.pending_confirm: dict | None = None
        self.broadcast: callable | None = None      # state feed, set by the server
        self.heard: callable | None = None           # live 'you said' feed, set by the server
        self.automation: bool = AUTOMATION           # do everything without asking
        self._shutdown_at: float = 0.0               # when a shutdown was scheduled
        self.action_broadcast: callable | None = None  # targeted device actions
        self._wake_count = 0
        self._wake_lock = threading.Lock()          # one wake session at a time
        self._interrupt = threading.Event()         # barge-in signal for a live session
        self._reply_seq = 0                         # reply-stream session id
        self._spoken = False                        # reply was already spoken during execute
        self.stream_feed: callable | None = None    # raw broadcast for reply chunks, set by the server

    # ---- status/UI --------------------------------------------------
    def _set_status(self, status: str, task: str = "") -> None:
        self.status = status
        self.current_task = task
        if self.broadcast:
            try:
                self.broadcast({"type": "status", "status": status, "task": task,
                                "devices": memory.get_devices(),
                                "stats": tools.system_stats()})
            except Exception:  # noqa: BLE001
                pass

    def _announce(self, text: str) -> None:
        """Push what SG just heard to the dashboard instantly (the 'You
        said' caption) - before the full state broadcast catches up."""
        try:
            if text and self.heard:
                self.heard(text)
        except Exception:  # noqa: BLE001
            pass

    def _save_capture(self, raw: bytes, text: str) -> None:
        """Save a raw mic capture that STT failed to understand, for diagnosis.

        Empty/junk transcripts are written as WAVs under database/captures/
        (newest 30 kept) so a 'voice not working' report can be traced to
        the actual audio - room noise, a dead mic, or a transient - instead
        of guessed at from text logs.
        """
        try:
            import wave
            d = DATA_DIR / "captures"
            d.mkdir(parents=True, exist_ok=True)
            for p in sorted(d.glob("cap_*.wav"))[:-30]:
                try:
                    p.unlink()
                except OSError:
                    pass
            label = "empty" if not text else "junk"
            path = d / f"cap_{int(time.time() * 1000)}_{label}_{len(raw)}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(raw)
            print(f"[core] saved failed capture -> {path.name} (stt={text!r})",
                  flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _say(self, text: str) -> None:
        self._set_status(STATUS_SPEAKING, "Speaking")
        self.tts.say(text)
        self.tts.wait_idle()

    def _emit_reply(self, payload: dict) -> None:
        """Push a reply-stream event (reply_begin/reply_seg/reply_play/
        reply_done) to the dashboard raw - not wrapped in the full state
        broadcast, so ordering and payload stay intact."""
        try:
            if self.stream_feed:
                self.stream_feed(payload)
        except Exception:  # noqa: BLE001
            pass

    def _emit_play(self, rid: int, seg: int, dur: int) -> None:
        self._emit_reply({"type": "reply_play", "id": rid, "seg": seg,
                          "dur": int(dur or 0)})

    def _speak_streamed(self, reply: str, speak: bool = True) -> None:
        """Progressive reply. The reply text is split into short segments;
        every segment's text is pushed to the dashboard instantly, then the
        segments are spoken one by one while each segment's real playback
        duration is streamed back. The UI reveals the text at exactly the
        pace the voice speaks - no word highlighting, just the text itself
        appearing progressively. With speak=False the text still appears
        progressively (estimated pace) but nothing is spoken."""
        reply = (reply or "").strip()
        if not reply:
            return
        segs = _split_segments(reply)
        self._reply_seq += 1
        rid = self._reply_seq
        self._emit_reply({"type": "reply_begin", "id": rid, "text": reply,
                          "segs": [{"seg": i, "text": s} for i, s in enumerate(segs)]})
        if not speak:
            return
        self._set_status(STATUS_SPEAKING, "Speaking")
        self.tts.on_segment = lambda i, d: self._emit_play(rid, i, d)
        for i, s in enumerate(segs):
            self.tts.say(s, i)
        self.tts.wait_idle()
        self._emit_reply({"type": "reply_done", "id": rid})

    def _stream_llm_reply(self, gen) -> str:
        """Consume a streaming LLM generator: the moment a sentence is
        generated it is pushed to the dashboard AND queued for speech, so
        text appears and the voice speaks at the same time, sentence by
        sentence, without waiting for the full reply."""
        self._reply_seq += 1
        rid = self._reply_seq
        full = ""
        buf = ""
        seg = 0
        spoke = False
        self._emit_reply({"type": "reply_begin", "id": rid, "text": ""})
        try:
            for chunk in gen:
                if self._interrupt.is_set():
                    break
                if chunk:
                    buf += chunk
                    full += chunk
                if len(buf) >= 8 and re.search(r"[.!?]\s*$", buf):
                    self._flush_llm_seg(rid, seg, buf)
                    seg += 1
                    spoke = True
                    buf = ""
                elif len(buf) >= 160:
                    self._flush_llm_seg(rid, seg, buf)
                    seg += 1
                    spoke = True
                    buf = ""
            if buf.strip():
                self._flush_llm_seg(rid, seg, buf)
                seg += 1
                spoke = True
            if spoke:
                self._spoken = True
                self._set_status(STATUS_SPEAKING, "Speaking")
                self.tts.on_segment = lambda i, d: self._emit_play(rid, i, d)
                self.tts.wait_idle()
            self._emit_reply({"type": "reply_done", "id": rid})
        except Exception as exc:  # noqa: BLE001
            print(f"[core] llm stream error: {exc}", flush=True)
            self._emit_reply({"type": "reply_done", "id": rid})
            if not spoke:
                return ""   # caller speaks a graceful fallback
            cleaned, actions = process_autonomous_actions(full)
            return cleaned.strip() or "(LLM unavailable.)"
        cleaned, actions = process_autonomous_actions(full)
        return cleaned.strip() or "(No reply.)"

    def _flush_llm_seg(self, rid: int, seg: int, text: str) -> None:
        clean_text = re.sub(r"\[ACTION:[^\]]+\]", "", text).strip()
        if not clean_text:
            return
        self._emit_reply({"type": "reply_seg", "id": rid, "seg": seg, "text": clean_text})
        self.tts.say(clean_text, seg)

    # ---- main loop ---------------------------------------------------
    def run_forever(self) -> None:
        self.mic.tts_busy_check = self.tts.is_busy   # never wake on our own voice
        self.mic.media_busy_check = tools.media_is_playing
        self.mic.start(on_wake=self.wake, on_wake_word=self.wake)
        while True:
            time.sleep(1)

    def wake(self) -> None:
        """Wake JARVIS - and if it's already awake and talking, BARGE IN:
        cut the speech off and go straight to listening."""
        barged = not self._wake_lock.acquire(blocking=False)
        if barged:
            # another wake session is live (greeting/reply playing): stop it
            print("[core] BARGE-IN - 'hey jarvis' while talking", flush=True)
            self.tts.stop()
            self._interrupt.set()
            self._wake_lock.acquire()          # wait for the old session to unwind
        self.busy = True
        self._wake_count += 1
        self._interrupt.clear()
        self.mic.resume()                      # detection stays live all session
        print(f"[core] WAKE SESSION start (barged={barged})", flush=True)
        try:
            if tools.media_is_playing():
                print("[core] wake ignored while media is playing", flush=True)
                return
            # a normal wake greets briefly; a barge-in goes STRAIGHT to
            # listening - the user already knows we're here, they cut in
            self.mic.clear_buffer()
            self.mic.reset_gate()
            first = True
            while True:
                if self._interrupt.is_set():
                    break
                self._set_status(STATUS_LISTENING, "Listening")
                self.mic.suppress_wake = True
                self.mic.reset_gate()
                text = self._listen(is_followup=not first)
                if not text:
                    if first:
                        self._say("I didn't catch that, boss. Try again.")
                        first = False
                        continue
                    break
                first = False
                self.mic.suppress_wake = False
                self._set_status(STATUS_THINKING, "Thinking")
                _t_think = time.time()
                reply = self.execute(text, source="voice")
                memory.add_turn("jarvis", reply)
                if self._spoken:
                    pass
                else:
                    self._speak_streamed(reply)
                total_ms = (time.time() - _t_think) * 1000
                print(f"[core] TIMING stop-talking->reply-spoken: "
                      f"STT={getattr(self, '_t_stt_ms', 0):.0f}ms  "
                      f"execute+TTS-start={total_ms:.0f}ms  "
                      f"TOTAL={getattr(self, '_t_stt_ms', 0) + total_ms:.0f}ms", flush=True)
                if tools.media_is_playing():
                    print("[core] ending voice session while media is playing",
                          flush=True)
                    break
                if self._interrupt.is_set():
                    break
                time.sleep(0.1)
        except Exception as exc:  # noqa: BLE001
            self._set_status(STATUS_ERROR, str(exc))
            self.tts.say("Something went wrong, but I'm still here, boss.")
        finally:
            self.mic.suppress_wake = False
            self.busy = False
            time.sleep(0.2)
            self.mic.resume()
            self._set_status(STATUS_STANDBY, "")
            self._wake_lock.release()

    def _listen(self, is_followup: bool = False) -> str:
        seed = self.mic.take_wake_audio()
        no_audio = 0
        timeout = 3.5 if is_followup else 5.0
        max_attempts = 1 if is_followup else 2
        for attempt in range(max_attempts):
            try:
                raw = self.mic.capture_phrase(timeout=timeout, phrase_limit=7.0,
                                              silence_after=0.35, seed=seed)
                _t_cap = time.time()
                seed = None
                if not raw:
                    no_audio += 1
                    continue
                from .speech import recognize
                text = recognize(raw)
                self._t_stt_ms = (time.time() - _t_cap) * 1000
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
                peak = float(np.abs(arr).max())
                print(f"[core] listen: {len(raw)} bytes (peak {peak:.3f}) -> {text!r} "
                      f"| STT={self._t_stt_ms:.0f}ms", flush=True)
                self._announce(text)
                if not text or _is_junk(text):
                    self._save_capture(raw, text)
                if text and not _is_junk(text):
                    return text
            except Exception as exc:  # noqa: BLE001
                print(f"[core] listen error: {exc}", flush=True)
        if no_audio >= max_attempts and not is_followup:
            self.mic.request_recheck()
        return ""

    # ---- command execution -------------------------------------------
    def execute(self, text: str, source: str = "text") -> str:
        """Run a command (voice, dashboard, or phone) and return the reply."""
        from .flow import clean_flow_speech
        text = clean_flow_speech(text or "").strip()
        if not text:
            return "Say again?"
        self._spoken = False
        memory.add_turn("user", text)
        memory.audit(source, f"command: {text[:200]}")
        t = text.lower()
        # strip leading wake phrases, polite greetings, and "can you" wrappers
        stripped = re.sub(
            r"^(?:(?:hey|ok|okay|hi|hello|yo)?\s*(?:spagasus\s+)?(?:jarvis|darlis|darvis|johnny)\s*[, .]*)+",
            "", t).strip()
        stripped = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+)+", "", stripped).strip()
        if stripped:
            t = stripped

        # confirmation flow for dangerous actions
        if self.pending_confirm:
            conf = self.pending_confirm
            self.pending_confirm = None
            if re.search(r"yes|confirm|do it|go ahead|yeah|sure", t):
                return self._run_confirmed(conf)
            return "Cancelled - nothing was done."

        # greeting / identity - answered with the right time-of-day greeting
        if not t or re.fullmatch(r"(hello|hi|hey|yo|good evening|good morning|good afternoon|good night|yes|jarvis|darlis|darvis|johnny)\s*(spagasus)?\s*(jarvis|darlis|darvis|johnny)?", t) or re.search(r"^(hey|ok|okay|hi|hello)\s*(spagasus\s+)?(?:jarvis|darlis|darvis|johnny)$", t):
            return time_greeting() + ", boss. Spagasus Jarvis at your service."
        if re.search(r"who are you|your name", t):
            return "I am Spagasus Jarvis, your personal AI assistant."
        if re.search(r"thank|thanks", t):
            return "You're welcome, boss."

        # memory
        m = re.search(r"remember\s+(?:that\s+)?(.+)", t)
        if m and "number" not in m.group(1):
            fact = m.group(1).strip()
            key = fact.split(" is ", 1)[0] if " is " in fact else fact[:40]
            value = fact.split(" is ", 1)[1] if " is " in fact else fact
            memory.remember(key, value)
            return f"Remembered: {fact}."
        if re.search(r"forget\s+(?:that|it)", t):
            memory.clear_memory()
            return "I've forgotten that."
        if re.search(r"clear my memory|erase.*memory", t):
            memory.clear_memory()
            return "Memory cleared, boss."
        if re.search(r"what do you (remember|know)|recall", t):
            facts = memory.recall()
            if not facts:
                return "I don't have anything saved yet, boss."
            return "I remember: " + "; ".join(f"{f['key']}: {f['value']}" for f in facts[:8]) + "."

        # routines
        m = re.search(r"every day at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(.+)", t)
        if m:
            hh, mm, ap, actions = int(m.group(1)), m.group(2) or "00", m.group(3), m.group(4).strip()
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            name = f"Routine {len(memory.get_routines()) + 1}"
            memory.add_routine(name, f"{hh:02d}:{mm}", [actions])
            return f"Done. Every day at {hh:02d}:{mm} I'll {actions}. Say 'my routines' to see it."
        if re.search(r"my routines|list.*routine", t):
            routines = memory.get_routines()
            if not routines:
                return "No routines set yet, boss."
            return "Routines: " + "; ".join(
                f"{r['name']} at {r['time']} ({'on' if r['enabled'] else 'off'})" for r in routines) + "."
        m = re.search(r"(enable|disable|delete)\s+routine\s+(?:called\s+)?(.+)", t)
        if m:
            action, name = m.group(1), m.group(2).strip().lower()
            for r in memory.get_routines():
                if name in r["name"].lower():
                    if action == "delete":
                        memory.delete_routine(r["id"])
                        return f"Deleted routine {r['name']}."
                    memory.set_routine_enabled(r["id"], action == "enable")
                    return f"{r['name']} is now {'on' if action == 'enable' else 'off'}."
            return f"No routine named {name}."

        # devices
        # ============ PHONE CONTROL (voice automations on the paired phone) ===
        # checked FIRST so phone-specific commands never hit the PC handlers
        # unlock the phone / type PIN: "unlock my mobile", "wakeup the mobile and type pin", "unlock phone with pin 1234"
        m_pin = re.search(r"(?:pin|code)\s+(?:of\s+|is\s+)?(\d{3,8})", t)
        custom_pin = m_pin.group(1) if m_pin else None

        if re.search(r"(?:unlock|wake(?:\s*up)?|type\s+pin)\s+(?:.*?\s+)?(?:mobile|phone|android)", t) or \
           re.search(r"(?:type\s+pin\s+\d+|enter\s+pin\s+\d+)", t):
            target = None if re.search(r"\ball\b|\bdevices\b", t) else (_phone_target(t) or "first mobile")
            reply = tools.unlock_phone(phone=target, pin=custom_pin)
            if self.action_broadcast:
                self.action_broadcast("phone", {"type": "action", "action": "unlock"})
            return reply

        # wake phone only: "wake up mobile", "wake phone"
        if re.search(r"(?:wake\s*up|wake)\s+(?:(?:the|my)\s+)?(?:mobile|phone|android)", t):
            target = None if re.search(r"\ball\b|\bdevices\b", t) else (_phone_target(t) or "first mobile")
            reply = tools.wake_phone(phone=target)
            if self.action_broadcast:
                self.action_broadcast("phone", {"type": "action", "action": "unlock"})
            return reply

        m = re.search(r"(?:open|launch|start)\s+(.+?)\s+(?:on|in)\s+" + _PH_PHR, t)
        if m:
            return tools.phone_open_app(m.group(1).strip(), phone=_phone_target(t))
        m = re.search(r"screenshot\s+(?:of\s+)?(?:my\s+)?(?:" + _PH_ORD
                      + r"\s+)?(?:phone|mobile)", t)
        if m:
            return tools.phone_screenshot(phone=_phone_target(t))
        m = re.search(r"phone\s+status|how\s+(?:is|are)\s+(?:my\s+)?(?:" + _PH_ORD
                      + r"\s+)?(?:phone|mobile)", t)
        if m:
            return tools.phone_status(phone=_phone_target(t))
        # lock the phone(s): "lock all mobiles" / "sleep all phones"
        # (screen off, at the same time) or one named phone "lock second mobile"
        if re.search(r"(?<!un)(?:lock|sleep)\s+all(?:" + r"\s+(?:the|my|three))?"
                     r"\s+(?:mobiles|phones)", t):
            return tools.lock_phone()
        if re.search(r"(?<!un)(?:lock|sleep)\s+(?:the\s+|my\s+)?(?:mobile|phone)", t):
            return tools.lock_phone(phone=_phone_target(t) or "first mobile")
        m = re.search(r"(?<!un)(?:lock|sleep)\s+(?:" + _PH_ORD + r")\s+(?:mobile|phone)", t)
        if m:
            return tools.lock_phone(phone=_phone_target(t))
        m = re.search(r"(?:call|dial)\s+([\d+][\d\s\-]{6,})\s+(?:on|in)\s+" + _PH_PHR, t)
        if m:
            return tools.phone_call(m.group(1).strip(),
                                    dial_only="dial" in t[:m.start()],
                                    phone=_phone_target(t))

        if re.search(r"what devices|connected devices|devices connected", t):
            devs = memory.get_devices()
            adb_names = {tools._phone_name_of(s).lower() for s in tools.adb_devices()}
            parts = [f"{d['name']} ({d['status']})" for d in devs
                     if d["name"].lower() not in adb_names]
            for ser in tools.adb_devices():
                parts.append(f"{tools._phone_name_of(ser)} (online, adb)")
            if not parts:
                return "No devices connected yet. The mobile app can pair via the phone token."
            return "Connected devices: " + "; ".join(parts) + "."

        # autonomous ideas & proactive automation (powered by Gemma 4 12B)
        if re.search(r"(?:own\s+idea|give\s+(?:me\s+)?(?:an?\s+)?idea|what\s+to\s+do|what\s+should\s+(?:i|we)\s+do|automate\s+(?:something|everything|all)|surprise\s+me|new\s+idea|autonomous\s+mode|make\s+all\s+automation|what\s+can\s+you\s+automate|do\s+all\s+automation|what\s+will\s+you\s+do|clean\s+(?:up\s+)?(?:temp|cache|system|my\s+pc))", t):
            stats = tools.system_stats()
            hour = datetime.datetime.now().hour
            time_ctx = "late night" if hour >= 22 or hour < 5 else ("morning" if hour < 12 else ("afternoon" if hour < 17 else "evening"))
            earbuds_info = "connected to wireless earbuds" if getattr(self, "audio_output_is_headset", True) else "on system speakers"
            prompt = (
                f"Context: Time is {time_ctx} ({datetime.datetime.now().strftime('%I:%M %p')}). "
                f"System: RAM usage {stats['ram']}%, CPU {stats['cpu']}%, Battery {stats['battery']}%. "
                f"Audio output is {earbuds_info}. "
                f"User request: '{text}'. "
                f"As Spagasus Jarvis with autonomous decision-making powered by Gemma 4 12B, formulate an intelligent "
                f"idea and plan of action for Gokul right now. Proactively decide what to automate (e.g. clean temp files, set volume, play music, open tools). "
                f"Speak what you are doing in 1-2 clear, confident sentences and append [ACTION:...] tags to execute the automations immediately."
            )
            reply = llm_chat(memory.recent_turns(), prompt)
            if reply and not reply.startswith("(LLM unavailable"):
                cleaned, executed = process_autonomous_actions(reply)
                return cleaned or reply

        # audio / microphone / wireless earbuds status check
        if re.search(r"(?:can\s+you\s+(?:hear|ear)\s+me|mic(?:rophone)?\s+status|check\s+mic|earbud|wireless\s+mic|(?:hear|ear)\s+(?:in|through)\s+(?:the\s+)?(?:wireless|mic|earbud)|wireless\s+earbud)", t):
            from .speech import find_best_headset_mic, find_best_output_device
            import sounddevice as sd
            in_dev = find_best_headset_mic()
            out_dev = find_best_output_device()
            in_name = sd.query_devices(in_dev)["name"] if in_dev is not None else None
            out_name = sd.query_devices(out_dev)["name"] if out_dev is not None else None
            if in_dev is not None and out_dev is not None:
                return f"Yes boss, I'm connected to your wireless earbuds ({in_name}). I am listening through your earbuds mic and speaking directly into your earbuds."
            elif in_dev is not None:
                return f"Yes boss, I can hear you clearly through your wireless earbuds microphone ({in_name})."
            elif out_dev is not None:
                return f"Yes boss, I am speaking directly to your wireless earbuds ({out_name})."
            return "Yes boss, I can hear you clearly through the system microphone."

        # system monitoring
        if re.search(r"system|status|usage|cpu|ram|memory|battery|storage|disk|network|temperature|apps running", t):
            return tools.system_question(t)

        # vision / multimodal screen intelligence
        m_vis = re.search(r"(?:look at (?:my )?screen|see (?:my )?screen|what(?:'s| is) on (?:my )?screen|explain (?:the |my )?screen|read (?:the |my )?screen|summarize (?:the |my )?screen|what error is (?:on|this)|check (?:my )?screen)(?:\s+(?:and\s+)?(?:tell me|explain|find)?\s*(.*))?", t)
        if m_vis:
            extra_q = (m_vis.group(1) or "").strip()
            prompt = f"Answer this question about the screen: {extra_q}" if extra_q else "Describe what is on this screen in 2 concise sentences."
            return vision.describe_screen(question=prompt)
        if re.search(r"screenshot|capture.*screen", t):
            path = vision.capture()
            return f"Screenshot saved to {path}."

        # media controls (play/pause, next, prev, mute, stop)
        if re.search(r"^(?:pause|resume|unpause)\s*(?:music|video|song|media|playback)?$|^(?:play|pause)\s*(?:the\s+)?(?:music|video|song|media)$", t):
            return tools.media_control("playpause")
        if re.search(r"^(?:next|skip)\s*(?:track|song|music|video)?$", t):
            return tools.media_control("next")
        if re.search(r"^(?:previous|prev|back)\s*(?:track|song|music|video)?$", t):
            return tools.media_control("prev")
        if re.search(r"^stop\s+(?:music|video|song|media|playback)$", t):
            return tools.media_control("stop")
        if re.search(r"^(?:mute|unmute)\s*(?:volume|sound|audio|pc)?$", t):
            return tools.media_control("mute")

        # display brightness
        m_b = re.search(r"(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})%?", t)
        if m_b:
            return tools.set_brightness(int(m_b.group(1)))
        if re.search(r"dim (?:the )?screen|decrease brightness|lower brightness", t):
            return tools.set_brightness(30)
        if re.search(r"brighten (?:the )?screen|increase brightness|max brightness", t):
            return tools.set_brightness(100)

        # window management
        if re.search(r"minimize all(?:\s+windows)?|show (?:the )?desktop|hide all windows", t):
            return tools.window_management("minimize_all")
        if re.search(r"(?:make\s+)?maximize(?:\s+(?:this|the|active)?\s*(?:window)?)?|fullscreen(?:\s+window)?", t):
            return tools.window_management("maximize")
        if re.search(r"(?:make\s+)?minimize(?:\s+(?:this|the|active)?\s*(?:window)?)?", t):
            return tools.window_management("minimize")
        if re.search(r"snap (?:this |the )?window (?:to the )?left", t):
            return tools.window_management("snap_left")
        if re.search(r"snap (?:this |the )?window (?:to the )?right", t):
            return tools.window_management("snap_right")
        if re.search(r"switch (?:window|app|application)|alt tab", t):
            return tools.window_management("switch_app")

        # clipboard
        if re.search(r"(?:what(?:'s| is) on (?:my )?clipboard|read (?:my )?clipboard|paste clipboard)", t):
            clip = tools.clipboard_read()
            return f"Clipboard contents: {clip}"
        m_clip = re.search(r"copy\s+(.+?)\s+to\s+clipboard", t)
        if m_clip:
            return tools.clipboard_write(m_clip.group(1).strip())

        # timers
        m_tim = re.search(r"set\s+(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*(min|minute|minutes|sec|second|seconds|hr|hour|hours)(?:\s+(?:called|for|labeled)\s+(.+))?", t)
        if m_tim:
            val = int(m_tim.group(1))
            unit = m_tim.group(2).lower()
            label = (m_tim.group(3) or "Timer").strip()
            mult = 60 if "min" in unit else (3600 if "hr" in unit or "hour" in unit else 1)
            return tools.set_timer(val * mult, label)
        if re.search(r"active timers|my timers|how much time left|check timers", t):
            t_list = tools.get_timers()
            if not t_list:
                return "No active timers running, boss."
            return "Active timers: " + "; ".join(f"{t['label']} ({t['remaining_s']}s remaining)" for t in t_list)

        # whatsapp messaging
        m_wmsg = re.search(r"(?:send\s+)?whatsapp\s+message\s+(?:to\s+)?([a-z0-9 ]+?)\s+(?:saying|that|with text)\s+(.+)", t)
        if m_wmsg:
            contact = m_wmsg.group(1).strip()
            orig_msg = re.search(r"(?:send\s+)?whatsapp\s+message\s+(?:to\s+)?(?:[a-z0-9 ]+?)\s+(?:saying|that|with text)\s+(.+)", text, re.I)
            msg_body = orig_msg.group(1).strip() if orig_msg else m_wmsg.group(2).strip()
            return tools.whatsapp_send_message(contact, msg_body)

        # time / date / weather / knowledge
        if re.search(r"what.*time|current time|the time|^time$", t):
            return "It is " + datetime.datetime.now().strftime("%I:%M %p") + "."
        if re.search(r"what.*date|today.*date|what day|^date$", t):
            return "Today is " + datetime.datetime.now().strftime("%A, %B %d, %Y") + "."
        m = re.search(r"weather\s*(?:in\s+)?(.+)?", t)
        if m and "weather" in t:
            city = (m.group(1) or "").strip()
            try:
                url = "https://wttr.in/" + urllib.parse.quote(city) + "?format=%l:+%t,+%w"
                req = urllib.request.Request(url, headers={"User-Agent": "spagasus-jarvis/1.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    out = resp.read().decode("utf-8", "replace").strip()
                return "Weather: " + re.sub(r"[^\x00-\x7F]+", "", out)
            except Exception:  # noqa: BLE001
                return "Couldn't reach the weather service."
        m = re.search(r"(?:who is|what is|tell me about)\s+(.+)", t)
        if m:
            topic = m.group(1).strip()
            wiki = "_".join(topic.title().split())
            try:
                req = urllib.request.Request(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(wiki)}",
                    headers={"User-Agent": "spagasus-jarvis/1.0 (personal assistant)"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                first = (data.get("extract") or "").split(". ")[0] + "."
                return "According to Wikipedia, " + re.sub(r"[^\x00-\x7F]+", "", first)
            except Exception:  # noqa: BLE001
                pass
            # Wikipedia missed - answer from live web results instead
            ans = tools.web_answer(topic)
            if ans:
                return ans
        # play the SAME song on ALL mobiles at once (YouTube app via ADB on
        # every attached phone): "play <song> on all mobiles" / "in all my
        # phones" - comes BEFORE the single-phone rule so plurals win
        m = re.search(r"(?:play|youtube)\s+(.+?)\s+(?:on|in)\s+all(?:\s+the)?"
                      r"(?:\s+my)?(?:\s+three)?\s+(?:mobiles|phones)"
                      r"(?:\s+at\s+the\s+same\s+time)?", t)
        if m:
            return tools.play_song_on_all_phones(m.group(1).strip())
        # play a song ON THE PHONE (YouTube app via ADB): "play <song> on my
        # phone/mobile" - handles phone, mobile, named phones and a trailing
        # "in youtube"
        m = re.search(r"(?:play|youtube)\s+(.+?)\s+(?:on|in)\s+" + _PH_PHR
                      + r"(?:\s+in\s+youtube)?", t)
        if m:
            return tools.play_song_on_phone(m.group(1).strip(), phone=_phone_target(t))
        # multi-device: play a song on ALL connected devices
        m = re.search(r"(?:play|youtube)\s+(.+?)\s+(?:on|in)\s+all(?:\s+the)?(?:\s+other)?\s+devices", t)
        if m:
            song = m.group(1).strip()
            url = tools.resolve_song(song)
            if not url:
                return f"I couldn't resolve {song}. Try again."
            local = tools.play_song(song)
            sent = 0
            if self.action_broadcast:
                sent = self.action_broadcast("phone", {"type": "action", "action": "play_song",
                                                       "song": song, "url": url})
            return f"{local} Also sent it to {sent} connected device(s)."

        # close app/window (e.g. "close cmd" / "close command prompt")
        m_close = re.search(r"(?:close|exit|quit)\s+(?:the\s+)?(.+)", t)
        if m_close:
            res = tools.close_app(m_close.group(1).strip())
            if res:
                return res

        # open app and type text (e.g. "open notepad and type you have been hacked" / "open notepad type hello")
        m_type = re.search(r"open\s+([a-z0-9 ]+?)\s+(?:and\s+)?(?:type|write)\s+(.+)", t)
        if m_type:
            app_name = m_type.group(1).strip()
            m_orig = re.search(r"open\s+[a-z0-9 ]+?\s+(?:and\s+)?(?:type|write)\s+(.+)", text, re.I)
            text_to_type = m_orig.group(1).strip() if m_orig else m_type.group(2).strip()
            return tools.open_and_type(app_name, text_to_type)

        # type in app (e.g. "type you have been hacked in notepad" / "write hello in notepad")
        m_in = re.search(r"(?:type|write)\s+(.+?)\s+(?:in|into|on)\s+([a-z0-9 ]+)", t)
        if m_in and m_in.group(2).strip() not in ("english", "tamil", "hindi", "spanish"):
            app_name = m_in.group(2).strip()
            m_orig = re.search(r"(?:type|write)\s+(.+?)\s+(?:in|into|on)\s+[a-z0-9 ]+", text, re.I)
            text_to_type = m_orig.group(1).strip() if m_orig else m_in.group(1).strip()
            return tools.open_and_type(app_name, text_to_type)

        # direct typing (e.g. "type you have been hacked")
        m_direct = re.search(r"^(?:type|write)\s+(.+)", t)
        if m_direct and not re.search(r"\b(email|mail|message|whatsapp)\b", t):
            m_orig = re.search(r"^(?:type|write)\s+(.+)", text, re.I)
            text_to_type = m_orig.group(1).strip() if m_orig else m_direct.group(1).strip()
            return tools.type_text(text_to_type)

        # apps - runs multi-step chains (e.g. "open chrome and open notepad")
        m = re.search(r"open\s+(.+)", t)
        if m:
            rest = m.group(1).strip()
            parts = re.split(r"\s+and\s+", rest)
            if len(parts) > 1:
                out = []
                for part in parts:
                    part = part.strip()
                    if part.startswith("play"):
                        out.append(tools.play_song(part[4:].strip()))
                    elif part.startswith("call"):
                        out.append(self._call_contact(part[4:].strip()))
                    elif part.startswith("type ") or part.startswith("write "):
                        out.append(tools.type_text(part.split(" ", 1)[1].strip()))
                    else:
                        out.append(tools.open_app(re.sub(r"^open\s+", "", part)))
                return " ".join(out)
            return tools.open_app(rest)
        # calls - place a REAL WhatsApp Desktop call (voice or video).
        # Handles the common phrasings, including "call in whatsapp X"
        # (note: the whatsapp-specific branch must come before plain "call"
        # so "call in whatsapp arvind" captures arvind, not "in whatsapp...")
        m = re.search(
            r"(?:call\s+(?:in|on)\s+(?:the\s+)?(?:desktop\s+|pc\s+)?whatsapp\s+(?:to\s+)?|"
            r"make\s+(?:a\s+)?(?:video\s+|voice\s+)?call\s+(?:to\s+)?|"
            r"(?:video|voice)\s+call\s+(?:to\s+)?|"
            r"call\s+(?:to\s+)?|ring\s+(?:up\s+)?)([a-z][a-z0-9 .'\-]{1,40})",
            t)
        if m:
            # "call aravind in desktop whatsapp" -> the contact is just "aravind"
            name = re.sub(
                r"\s+(?:in|on)\s+(?:the\s+)?(?:desktop\s+|pc\s+)?whatsapp\s*$",
                "", m.group(1).strip())
            if name and name.split()[0] not in _CALL_SKIP:
                return self._call_contact(name, video=bool(re.search(r"video", t)))
        # music
        m = re.search(r"(?:play|youtube)\s+(.+)", t)
        if m:
            # STT often appends punctuation - strip a trailing "in/on
            # youtube" (with optional period) so the search is the song only
            song = re.sub(r"\s+(?:in|on)\s+youtube[.,!?]*\s*$", "", m.group(1).strip())
            return tools.play_song(song)
        # websites
        site = tools.open_site(t)
        if site:
            return site
        # keys / type
        keys = tools.press_keys(t)
        if keys:
            return keys
        # volume / lock / power
        vol = tools.volume(t)
        if vol:
            return vol
        # pair a new phone: "pair phone code 266732 192.168.1.5:38707"
        m = re.search(r"pair\s+(?:my\s+)?phone\s+(?:with\s+)?(?:code\s+)?"
                      r"(\d{6})\s+(?:at\s+|on\s+)?"
                      r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", t)
        if m:
            return tools.adb_pair(m.group(2), m.group(1))
        # connect the phone to ADB:
        # "connect phone", "connect to my mobile", "connect my phone to adb",
        # "connect phone 192.168.1.11:42607", or "192.168.1.10:41565 connect this"
        m = re.search(r"(?:(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})\s+connect(?:\s+this)?|"
                      r"connect(?:\s+to)?\s+(?:my\s+|the\s+)?"
                      r"(?:phone|mobile|this)?(?:\s+to)?(?:\s+adb)?"
                      r"(?:\s+(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}))?)", t)
        if m:
            addr = m.group(1) or m.group(2)
            if addr:
                return tools.adb_connect(addr)
            if tools.adb_devices():
                return f"Phone already attached: {tools.adb_devices()[0]}."
            dev = tools._ensure_phone()  # remembered addr + mDNS auto-discovery
            if dev:
                return f"Connected to your phone automatically: {dev}."
            return ("I couldn't find your phone on ADB. Turn ON Wireless debugging "
                    "on the phone (Settings > Developer options), keep that screen "
                    "open, and say 'connect phone' again - I'll auto-detect it. "
                    "Or give me the address, e.g. connect phone 192.168.1.11:42607")
        # wake the phone screen (e.g. "wakeup the mobile")
        if re.search(r"(?:wake\s*up|wake)\s+(?:(?:all|my|the|three|" + _PH_ORD + r")\s+)?"
                     r"(?:(?:mobile|android|" + _PH_ORD + r")\s+)?"
                     r"(?:phone|devices|mobile)", t):
            target = None if re.search(r"\ball\b|\bdevices\b", t) else (_phone_target(t) or "first mobile")
            parts = [tools.wake_phone(phone=target)]
            if self.action_broadcast:
                sent = self.action_broadcast("phone", {"type": "action", "action": "unlock"})
                if sent:
                    parts.append(f"Also sent the wake alert to {sent} device(s).")
            return " ".join(parts)

        # phone unlock: a REAL unlock over ADB when the phone is attached
        # (USB/wireless debugging), plus the companion alert as the fallback
        if re.search(r"(?:unlock|wake(?:\s+up)?)\s+(?:(?:all|my|the|three|" + _PH_ORD + r")\s+)?"
                     r"(?:(?:mobile|android|" + _PH_ORD + r")\s+)?"
                     r"(?:phone|devices|mobile)", t):
            target = None if re.search(r"\ball\b|\bdevices\b", t) else (_phone_target(t) or "first mobile")
            parts = [tools.unlock_phone(phone=target)]
            if self.action_broadcast:
                sent = self.action_broadcast("phone", {"type": "action", "action": "unlock"})
                if sent:
                    parts.append(f"Also sent the unlock alert to {sent} device(s).")
                else:
                    parts.append("The unlock alert couldn't reach the phone - "
                                 "the companion page isn't open on it. Open "
                                 "http://<pc-ip>:8790/m on the phone so the alert "
                                 "can light the screen too.")
            return " ".join(parts)
        if re.search(r"unlock\s+my\s+screen|unlock\s+(the\s+)?(pc|computer|screen)", t):
            return ("Your screen is already unlocked, boss. For security, unlocking after a lock "
                    "needs your PIN or Windows Hello - I can lock it for you if you want.")
        if re.search(r"lock\s+(?:(?:the|my)\s+)?(pc|computer|screen|system)", t):
            return tools.lock()
        # cancel escape for a scheduled shutdown/restart: "cancel", "stop"
        if re.search(r"(?:cancel|abort|stop)\s+(?:the\s+)?(?:shutdown|restart|reboot|it|that)", t):
            if time.time() - self._shutdown_at < 60:
                tools._run("shutdown /a")
                self._shutdown_at = 0.0
                return "Cancelled - the shutdown was aborted."
        if re.search(r"shut\s*down|shutdown", t):
            if self.automation:
                # full automation: do it now, no confirmation asked. The OS
                # 10 s delay is the only grace (say 'cancel' to abort).
                self._shutdown_at = time.time()
                tools._run("shutdown /s /t 10")
                return "Shutting down in 10 seconds."
            self.pending_confirm = {"tool": "shutdown", "prompt": "Shut down the PC?",
                                    "run": lambda: tools._run("shutdown /s /t 20")}
            return "Shutting down in 20 seconds. Say yes to confirm, or cancel."
        if re.search(r"restart|reboot", t):
            if self.automation:
                self._shutdown_at = time.time()
                tools._run("shutdown /r /t 10")
                return "Restarting in 10 seconds."
            self.pending_confirm = {"tool": "restart", "prompt": "Restart the PC?",
                                    "run": lambda: tools._run("shutdown /r /t 20")}
            return "Restarting in 20 seconds. Say yes to confirm, or cancel."
        # filesystem
        fs = tools.fs_operation(t)
        if fs:
            if isinstance(fs, dict):
                m = re.search(r"delete\s+(?:the\s+)?file\s+([\w\-. ]+)", t)
                path = tools._home() / m.group(1).strip() if m else None

                def do_delete(p=path):
                    if p and p.exists():
                        p.unlink()
                        return f"Deleted {p}."
                    return "File not found - nothing deleted."

                if self.automation:
                    # full automation: delete immediately, no confirmation
                    return do_delete()
                self.pending_confirm = {"tool": "delete_file",
                                        "prompt": fs["prompt"],
                                        "run": do_delete}
                return fs["prompt"]
            return fs
        # raw command
        m = re.search(r"(?:run command|execute)\s+(.+)", t)
        if m:
            cmd = m.group(1).strip()
            if self.automation:
                # full automation: run immediately, no confirmation
                return tools._run(cmd)
            self.pending_confirm = {"tool": "run_command", "prompt": f"Run: {cmd}?",
                                    "run": lambda: tools._run(cmd)}
            return f"Run shell command '{cmd}'? Say yes to confirm."
        # search - answer from the web; only open the browser as a last resort
        m = re.search(r"(?:search|google|look up)\s+(.+)", t)
        if m:
            ans = tools.web_answer(m.group(1).strip())
            return ans or tools.web_search(m.group(1).strip())
        # joke
        if re.search(r"joke", t):
            return random.choice(JOKES)

        # LLM or web fallback - never "I can't": answer from the web,
        # and only open a browser tab if even the web lookup fails. For
        # voice, the LLM streams: each sentence is pushed to the dashboard
        # and spoken the moment it's generated (low-latency, text + voice
        # together) instead of waiting for the whole reply.
        if source == "voice":
            try:
                gen = llm_chat_stream(memory.recent_turns(), text)
                if gen is not None:
                    reply = self._stream_llm_reply(gen)
                    if reply and not reply.startswith("(LLM unavailable"):
                        cleaned, _ = process_autonomous_actions(reply)
                        return cleaned or reply
            except Exception:  # noqa: BLE001
                pass
        try:
            reply = llm_chat(memory.recent_turns(), text)
            if reply and not reply.startswith("(LLM unavailable"):
                cleaned, _ = process_autonomous_actions(reply)
                return cleaned or reply
        except Exception:  # noqa: BLE001
            pass
        ans = tools.web_answer(text)
        if ans:
            return ans
        return "I'm listening, boss. What would you like me to do?"

    def _run_confirmed(self, conf: dict) -> str:
        memory.audit("confirmed", f"ran {conf['tool']}")
        try:
            result = conf["run"]()
            return f"Done. {result or ''}".strip()
        except Exception as exc:  # noqa: BLE001
            return f"Failed: {exc}"

    def _call_contact(self, name: str, video: bool = False) -> str:
        """Place a WhatsApp Desktop call. Uses a stored number if available,
        otherwise finds the contact straight in the WhatsApp chat list."""
        clean = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
        contacts = {f["key"]: f["value"] for f in memory.recall()
                    if "number" in f["key"].lower() or re.fullmatch(r"[\d+ ]+", f["value"].strip())}
        number = contacts.get(clean) or contacts.get(clean.replace(" ", ""))
        number_digits = re.sub(r"[^\d]", "", number) if number else None
        return tools.whatsapp_call(clean, number_digits, video)


def _is_junk(text: str) -> bool:
    """Only drop truly empty audio or pure filler grunts (um, uh)."""
    if not text or not text.strip():
        return True
    t = re.sub(r"[^a-z0-9 ]", "", text.strip().lower()).strip()
    if not t or t in {"um", "uh", "hmm", "ah", "er"}:
        return True
    return False
