"""SPAGASUS JARVIS - speech engine.

One continuous mic stream powers everything: clap wake, "Hey JARVIS" wake
word, voice-activity-gated phrase capture, barge-in, and the live level
feed for the UI. STT sends raw PCM straight to Google (no flac binary -
that was the crash bug in the earlier build). TTS uses the neural
edge-tts voices played through Windows MCI, with pyttsx3 as fallback.
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from .config import NOISE_CANCEL, SAMPLE_RATE, STT_ENGINE, STT_LANGUAGE, TTS_VOICE, WHISPER_MODEL

# Google speech endpoint - accepts RAW int16 PCM, so no flac needed.
GOOGLE_SPEECH_URL_TEMPLATE = (
    "https://www.google.com/speech-api/v2/recognize"
    "?client=chromium&lang={lang}&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"
)


def google_recognize_raw(raw_pcm: bytes, rate: int = SAMPLE_RATE, lang: str | None = None) -> str:
    if not raw_pcm:
        return ""
    target_lang = lang or STT_LANGUAGE or "en-IN"
    url = GOOGLE_SPEECH_URL_TEMPLATE.format(lang=target_lang)
    req = urllib.request.Request(
        url,
        data=raw_pcm,
        headers={"Content-Type": f"audio/l16; rate={rate}",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            for result in data.get("result", []):
                alts = result.get("alternative", [])
                if alts and alts[0].get("transcript"):
                    return alts[0]["transcript"]
    except Exception:
        pass

    # Multi-accent fallback: if en-IN produced nothing, try en-US (or vice-versa)
    fallback_lang = "en-US" if target_lang != "en-US" else "en-IN"
    try:
        url_fb = GOOGLE_SPEECH_URL_TEMPLATE.format(lang=fallback_lang)
        req_fb = urllib.request.Request(
            url_fb,
            data=raw_pcm,
            headers={"Content-Type": f"audio/l16; rate={rate}",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req_fb, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            for result in data.get("result", []):
                alts = result.get("alternative", [])
                if alts and alts[0].get("transcript"):
                    return alts[0]["transcript"]
    except Exception:
        pass
    return ""


# faster-whisper (CTranslate2) local model - loaded once, reused for every
# request. Far better accuracy than the free Google speech endpoint and fully
# offline; the model file is downloaded from HuggingFace on first use.
_fast_model = None
_fast_lock = threading.Lock()


def recognize_fast(raw_pcm: bytes, language: str = "en") -> str:
    """Transcribe raw int16 PCM with local faster-whisper."""
    global _fast_model
    with _fast_lock:
        if _fast_model is None:
            from faster_whisper import WhisperModel  # type: ignore
            _fast_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        model = _fast_model
    import io
    import wave as wave_mod
    buf = io.BytesIO()
    with wave_mod.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(raw_pcm)
    buf.seek(0)
    segments, _info = model.transcribe(buf, language=language, vad_filter=True)
    parts = [s.text.strip() for s in segments]
    return " ".join(p for p in parts if p).strip()


def _normalize_gain(raw_pcm: bytes) -> bytes:
    """Normalize speech volume dynamically with AGC so quiet, soft, or distant voices reach full clarity."""
    if not raw_pcm:
        return raw_pcm
    arr = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if len(arr) == 0:
        return raw_pcm
    peak = float(np.max(np.abs(arr)))
    if 0.0005 < peak < 0.88:
        scale = min(0.90 / max(peak, 0.001), 35.0)
        arr = np.clip(arr * scale, -1.0, 1.0)
        return (arr * 32767).astype(np.int16).tobytes()
    return raw_pcm


def _transcribe(raw_pcm: bytes) -> str:
    """Intelligent multi-tier STT pipeline: Parakeet v2 / Wispr Flow / Google en-IN+en-US / Whisper."""
    # 1. Local neural Parakeet v2 (offline, high fidelity, 0 latency, no rate limits)
    if STT_ENGINE in ("auto", "parakeet"):
        try:
            if parakeet_status()["installed"]:
                res = recognize_parakeet(raw_pcm)
                if res and res.strip():
                    return res.strip()
        except Exception as exc:
            print(f"[speech] Parakeet ASR fallback ({exc})", flush=True)

    # 2. Local Whisper (offline, multi-lingual)
    if STT_ENGINE == "whisper":
        try:
            res = recognize_fast(raw_pcm)
            if res and res.strip():
                return res.strip()
        except Exception as exc:
            print(f"[speech] Whisper ASR fallback ({exc})", flush=True)

    # 3. Wispr Flow / Whisper API if configured
    try:
        from .flow import recognize_wispr_flow
        w_text = recognize_wispr_flow(raw_pcm)
        if w_text and w_text.strip():
            return w_text.strip()
    except Exception:
        pass

    # 4. Google Speech API (multi-accent: en-IN with en-US fallback)
    try:
        res = google_recognize_raw(raw_pcm)
        if res and res.strip():
            return res.strip()
    except Exception:
        pass

    # 5. Secondary local offline fallback
    if STT_ENGINE not in ("auto", "parakeet"):
        try:
            if parakeet_status()["installed"]:
                res = recognize_parakeet(raw_pcm)
                if res and res.strip():
                    return res.strip()
        except Exception:
            pass
    if STT_ENGINE != "whisper":
        try:
            res = recognize_fast(raw_pcm)
            if res and res.strip():
                return res.strip()
        except Exception:
            pass
    return ""


def _gated(raw_pcm: bytes) -> bytes:
    """Spectral-gate a clip with the app's own NoiseGate, for a STT retry."""
    arr = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if len(arr) < SAMPLE_RATE // 4:
        return raw_pcm
    gate = NoiseGate()
    quiet = np.argsort(np.abs(arr))[: max(1, len(arr) // 4)]
    gate.process(arr[quiet], silence=True)
    out = gate.process(arr, silence=False)
    return (np.clip(out, -1, 1) * 32767).astype(np.int16).tobytes()


def recognize(raw_pcm: bytes) -> str:
    """STT adapter: Wispr Flow -> Parakeet v2 -> faster-whisper -> google with flow cleaner."""
    if not raw_pcm or len(raw_pcm) < int(SAMPLE_RATE * 0.2 * 2):  # < 200ms
        return ""
    raw_pcm = _normalize_gain(raw_pcm)
    text = _transcribe(raw_pcm)
    if not text and NOISE_CANCEL and len(raw_pcm) >= int(SAMPLE_RATE * 0.4 * 2):
        try:
            text = _transcribe(_gated(raw_pcm))
            if text:
                print("[speech] STT empty on raw audio - rescued by noise-gated retry", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[speech] gated STT retry failed ({exc})", flush=True)

    if text:
        from .flow import clean_flow_speech
        cleaned = clean_flow_speech(text)
        if cleaned:
            return cleaned
    return text


# ---------------------------------------------------------------------
# Parakeet v2 (sherpa-onnx) - downloaded on demand, never bundled
# ---------------------------------------------------------------------

from .config import MODELS_DIR, PARAKEET_MODEL_NAME  # noqa: E402

PARAKEET_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2"
)
PARAKEET_FILES = ["encoder.int8.onnx", "decoder.int8.onnx",
                  "joiner.int8.onnx", "tokens.txt"]


def parakeet_model_dir() -> Path:
    return MODELS_DIR / PARAKEET_MODEL_NAME


def parakeet_status() -> dict:
    """Is the Parakeet v2 model installed and usable?"""
    d = parakeet_model_dir()
    missing = [f for f in PARAKEET_FILES if not (d / f).exists()]
    size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) if d.exists() else 0
    return {
        "name": PARAKEET_MODEL_NAME,
        "installed": not missing and size > 100_000_000,
        "missing": missing,
        "size": size,
        "dir": str(d),
    }


_parakeet_install = {"running": False, "phase": "idle", "progress": 0.0,
                     "bytes": 0, "total": 0, "error": None}
_parakeet_install_lock = threading.Lock()


def parakeet_install_state() -> dict:
    with _parakeet_install_lock:
        return dict(_parakeet_install)


def _parakeet_report(**kw: object) -> None:
    with _parakeet_install_lock:
        _parakeet_install.update(kw)


def install_parakeet() -> None:
    """Download the Parakeet v2 int8 model (tar.bz2) and extract it.

    Runs on a background thread; progress is polled by the app via
    /api/stt/model. The ~482 MB model comes from the official k2-fsa
    sherpa-onnx release assets - the same artifact the sherpa docs use.
    """
    with _parakeet_install_lock:
        if _parakeet_install["running"]:
            return
        _parakeet_install.update(running=True, phase="downloading",
                                 progress=0.0, bytes=0, total=0, error=None)
    dst = MODELS_DIR / (PARAKEET_MODEL_NAME + ".tar.bz2")
    tmp = dst.with_suffix(".tmp")
    try:
        req = urllib.request.Request(PARAKEET_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            _parakeet_report(total=total, bytes=0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                _parakeet_report(bytes=done, progress=done / total if total else 0.0)
        _parakeet_report(phase="extracting", progress=1.0)
        import tarfile
        target = MODELS_DIR
        with tarfile.open(tmp, "r:bz2") as tar:
            for member in tar:
                tar.extract(member, target)
        tmp.unlink(missing_ok=True)
        _parakeet_report(phase="done", progress=1.0)
        print(f"[speech] parakeet v2 model installed: {parakeet_model_dir()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _parakeet_report(phase="error", error=str(exc)[:200])
        print(f"[speech] parakeet install failed: {exc}", flush=True)
    finally:
        with _parakeet_install_lock:
            _parakeet_install["running"] = False


_parakeet_rec = None
_parakeet_rec_lock = threading.Lock()


def recognize_parakeet(raw_pcm: bytes) -> str:
    """Transcribe raw int16 PCM with NVIDIA Parakeet v2 via sherpa-onnx."""
    global _parakeet_rec
    with _parakeet_rec_lock:
        if _parakeet_rec is None:
            import sherpa_onnx  # type: ignore
            d = parakeet_model_dir()
            # 4 threads matches the 4 high-performance P-cores on Intel 13th Gen (avoids E-core thrashing)
            _parakeet_rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(d / "encoder.int8.onnx"),
                decoder=str(d / "decoder.int8.onnx"),
                joiner=str(d / "joiner.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=4, sample_rate=SAMPLE_RATE, feature_dim=80,
                decoding_method="greedy_search", provider="cpu",
                model_type="nemo_transducer",
            )
            print("[speech] parakeet v2 recognizer loaded (4 threads)", flush=True)
        rec = _parakeet_rec
    samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    stream = rec.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    rec.decode_stream(stream)
    return stream.result.text.strip()


def warmup_parakeet() -> None:
    """Preload the Parakeet recognizer so the FIRST voice turn is fast.

    The model takes ~8-10 s to load on this PC. If the app's first voice
    request hits that cold load inside its 20 s STT timeout it can feel
    like voice mode is broken, so the backend warms it up in a background
    thread at startup (a tiny silence clip - cheap and harmless).
    """
    try:
        if not parakeet_status()["installed"]:
            return
        t0 = time.time()
        silent = np.zeros(SAMPLE_RATE, dtype=np.int16).tobytes()  # 1 s of silence
        recognize_parakeet(silent)
        print(f"[speech] parakeet v2 warmed in {time.time() - t0:.1f}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[speech] parakeet warmup failed: {exc}", flush=True)


# ---------------------------------------------------------------------
# noise cancellation
# ---------------------------------------------------------------------

class NoiseGate:
    """Real-time spectral-gate noise suppressor (pure numpy, no new deps).

    Learns the background-noise spectrum from quiet moments, then subtracts
    it from every 25 ms frame (spectral gating, the same idea Audacity's
    noise reduction uses). A 50% overlap-add with sqrt-Hann windows keeps it
    click-free. Only the voice passes - fans, AC, keyboard and street noise
    are gated out before the audio ever reaches the VAD or speech-to-text.
    """

    def __init__(self, frame: int = 800, hop: int = 400,
                 alpha: float = 2.0, smooth: float = 0.50,
                 kill_bins: int = 6) -> None:
        self.frame = frame          # 50 ms @16 kHz
        self.hop = hop              # 25 ms @16 kHz
        self.alpha = alpha          # how aggressively noise is removed
        self.smooth = smooth        # gain smoothing (kills musical noise)
        self.kill_bins = kill_bins  # hard-zero DC + sub-100 Hz rumble
        self.floor_gain = 0.04      # whisper of the spectral floor (no zeros)
        # sqrt-Hann windows satisfy the COLA condition for 50% overlap-add
        self.win = np.sqrt(np.hanning(frame)).astype(np.float32)
        self._buf = np.zeros(frame, dtype=np.float32)   # rolling raw frame
        self._tail = np.zeros(hop, dtype=np.float32)    # OLA carry-over
        self._noise: np.ndarray | None = None           # learned noise spectrum
        self._gain: np.ndarray | None = None            # smoothed per-bin gain

    def process(self, x: np.ndarray, silence: bool) -> np.ndarray:
        """Gate one input block. `silence` says the raw block is pure noise."""
        n = len(x)
        out = np.zeros(n, dtype=np.float32)
        pos = 0
        while pos < n:
            chunk = x[pos:pos + self.hop]
            k = len(chunk)
            if k < self.hop:
                chunk = np.pad(chunk, (0, self.hop - k))
            self._buf[:self.frame - self.hop] = self._buf[self.hop:]
            self._buf[self.frame - self.hop:] = chunk
            spec = np.fft.rfft(self._buf * self.win)
            mag = np.abs(spec)
            if self._noise is None:
                self._noise = mag.copy()
            else:
                # per-bin MINIMUM tracker: the spectrum converges to the
                # STEADY background (TV, music, fan) even when it's loud,
                # because speech/vocals come and go but the background is
                # constant. This lets the gate subtract a loud room from
                # under the user's voice - the old average-from-silence-
                # only model never learned loud rooms at all (it only
                # updated on quiet frames, and a loud TV is never quiet).
                # The tiny multiplicative drift lets the model recover if
                # the room genuinely gets louder over time.
                self._noise = np.minimum(self._noise, mag) * (1.0005 if silence else 1.0001)
            # spectral gain: hard-suppress bins at/below the noise floor so
            # only the VOICE's frequencies survive (alpha=2.0 is aggressive)
            gain = np.clip(1.0 - self.alpha * self._noise / (mag + 1e-6),
                           self.floor_gain, 1.0)
            gain[:self.kill_bins] = 0.0
            if self._gain is None:
                self._gain = gain
            else:
                self._gain = self._gain * self.smooth + gain * (1 - self.smooth)
            clean = np.fft.irfft(spec * self._gain) * self.win
            out[pos:pos + k] = (clean[:self.hop] + self._tail)[:k]
            self._tail = clean[self.hop:].copy()
            pos += self.hop
        return out


# ---------------------------------------------------------------------
# microphone stream
# ---------------------------------------------------------------------

def _device_valid(dev: int | None) -> bool:
    """True if the device index still exists and can capture audio.

    Windows re-enumerates audio devices (a monitor/speaker unplugs, the
    Realtek driver restarts) and indices go stale: a stream armed on a
    vanished index opens fine but returns pure silence. That silent dead
    stream is exactly 'the mic stopped working'.
    """
    if dev is None:
        return True
    try:
        import sounddevice as sd
        d = sd.query_devices(dev)
        return bool(d.get("max_input_channels", 0) > 0)
    except Exception:  # noqa: BLE001
        return False


def _device_hears(dev: int | None, seconds: float = 0.5) -> float:
    """Record briefly on `dev` and return the RMS (0.0 on failure/silence)."""
    import sounddevice as sd
    try:
        rec = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                     channels=1, dtype="int16", device=dev)
        sd.wait()
        a = np.abs(rec.astype(np.float64)) / 32768.0
        return float(np.sqrt(np.mean(a * a)))
    except Exception:  # noqa: BLE001
        return 0.0


# --- durable mic selection -----------------------------------------
# The Realtek/SST driver on this machine CHURNS: device indices go stale
# and rapid open/close probing destabilizes it further (a probe storm makes
# entries invalid and streams silent - exactly the recurring deafness). So
# device picking is now CONSERVATIVE:
#   * the system default (None) is the anchor - only probed away from when
#     a live direct entry is actually HEARD,
#   * probe results are cached 120 s, switches rate-limited to 1 per 2 min,
#   * devices that errored or proved silent are remembered and skipped,
#   * the last known-good device is persisted across restarts.
_probe_cache: dict = {"at": 0.0, "result": None}
_bad_devices: dict[int, float] = {}
_last_switch_at = 0.0
SAVED_MIC_PATH = Path(__file__).resolve().parent.parent / "database" / "mic_device.json"


def _load_saved_mic() -> int | None:
    """The last device that produced real audio, or None for the default."""
    try:
        data = json.loads(SAVED_MIC_PATH.read_text(encoding="utf-8"))
        dev = int(data.get("device", -1))
        return dev if dev >= 0 else None
    except Exception:  # noqa: BLE001
        return None


def _save_mic(dev: int | None) -> None:
    try:
        SAVED_MIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAVED_MIC_PATH.write_text(json.dumps({"device": dev}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _mark_bad(dev: int | None) -> None:
    """Remember a device that proved dead - skip it for 5 minutes."""
    global _probe_cache
    if dev is None:
        return
    _bad_devices[dev] = time.time()
    _probe_cache = {"at": 0.0, "result": None}   # force a fresh probe next time


WIRELESS_PATTERNS = re.compile(
    r"bluetooth|bth|wireless|airpod|airdopes|rainbow|earbud|buds|hands-free|headset|headphone",
    re.IGNORECASE
)
HEADSET_PATTERNS = re.compile(
    r"headset|headphone|earbud|earphone|airpod|airdopes|rainbow|bluetooth|hands-free|wireless|buds",
    re.IGNORECASE
)


def find_best_headset_mic() -> int | None:
    """Find the best connected wireless earbuds or headset microphone."""
    import sounddevice as sd
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0 and i not in _bad_devices:
                name = d.get("name", "")
                if name.startswith("@") or "bthhfenum" in name.lower() or "realtek" in name.lower():
                    continue
                if WIRELESS_PATTERNS.search(name) or HEADSET_PATTERNS.search(name):
                    try:
                        s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=i)
                        s.start()
                        s.stop()
                        s.close()
                        return i
                    except Exception:
                        continue
    except Exception:
        pass
    return None


def find_best_output_device() -> int | None:
    """Find the best output device (Wireless Earbuds > Headset > None for default speakers)."""
    import sounddevice as sd
    try:
        devices = sd.query_devices()
        best_headset = None
        for i, d in enumerate(devices):
            if d.get("max_output_channels", 0) > 0:
                name = d.get("name", "")
                if name.startswith("@") or "bthhfenum" in name.lower() or "realtek" in name.lower():
                    continue
                if WIRELESS_PATTERNS.search(name) or HEADSET_PATTERNS.search(name):
                    try:
                        ch = min(2, d.get("max_output_channels", 2))
                        s = sd.OutputStream(samplerate=44100, channels=ch, dtype="int16", device=i)
                        s.start()
                        s.stop()
                        s.close()
                        # Prioritize stereo headphones/earbuds profile for high quality audio
                        if any(k in name.lower() for k in ["headphone", "buds", "airdopes", "airpod"]):
                            return i
                        if best_headset is None:
                            best_headset = i
                    except Exception:
                        continue
        if best_headset is not None:
            return best_headset
    except Exception:
        pass
    return None


def _decode_audio(path_or_bytes) -> tuple[np.ndarray, int]:
    """Decode any audio file/bytes (MP3, WAV, etc.) to (int16 mono samples, sample_rate)."""
    import av
    container = av.open(str(path_or_bytes) if isinstance(path_or_bytes, Path) else path_or_bytes)
    stream = container.streams.audio[0]
    rate = stream.rate or 24000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    chunks = []
    for frame in container.decode(audio=0):
        for rf in resampler.resample(frame):
            chunks.append(rf.to_ndarray())
    if not chunks:
        return np.zeros(0, dtype=np.int16), rate
    arr = np.concatenate(chunks, axis=1).squeeze()
    return arr, rate


def _find_working_mic(seconds: float = 0.8, passes: int = 1) -> int | None:
    """Pick the input device that actually hears sound - CONSERVATIVELY.
    Prioritizes wireless earbuds / Bluetooth headsets whenever connected.
    """
    global _probe_cache
    now = time.time()

    # Priority 1: Check for wireless earbuds / headset mic immediately
    headset_mic = find_best_headset_mic()
    if headset_mic is not None:
        _probe_cache = {"at": now, "result": headset_mic}
        return headset_mic

    if now - _probe_cache["at"] < 120:
        return _probe_cache["result"]
    # prune expired bad-device entries
    for dev in [d for d, t in _bad_devices.items() if now - t > 300]:
        _bad_devices.pop(dev, None)
    import sounddevice as sd
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001
        _probe_cache = {"at": now, "result": None}
        return None
    junk = re.compile(r"speaker|output|stereo mix|what u hear|mapper", re.I)
    is_mic = re.compile(r"mic(rophone)?|array", re.I)
    best_dev, best_rms = None, 0.0
    for _ in range(max(1, passes)):
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) < 1:
                continue
            if i in _bad_devices:
                continue
            name = d.get("name", "")
            if junk.search(name):
                continue
            try:
                rec = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                             channels=1, dtype="int16", device=i)
                sd.wait()
                a = np.abs(rec.astype(np.float64)) / 32768.0
                a = np.clip(a, 0.0, 1.0)      # format garbage -> ignore
                rms = float(np.sqrt(np.mean(a * a)))
                # a real microphone entry with any live signal beats a louder
                # non-mic entry, so give mic-named devices a head start (but
                # only after the live-signal check - dead mics stay excluded)
                if 0.0015 < rms:
                    if is_mic.search(name):
                        rms += 0.05
                    if rms > best_rms:
                        best_dev, best_rms = i, rms
            except Exception:  # noqa: BLE001
                _mark_bad(i)   # errored on probe - remember it, don't repeat
        if best_dev is not None:
            break
        time.sleep(1.0)   # one pause before the final pass - noise comes and goes
    if best_dev is not None:
        print(f"[speech] mic device {best_dev}: {devices[best_dev]['name']} "
              f"(rms {best_rms:.4f})", flush=True)
    else:
        print("[speech] no live mic found - keeping the default", flush=True)
    _probe_cache = {"at": now, "result": best_dev}
    return best_dev


class MicStream:
    """One sounddevice InputStream doing claps + VAD + capture + level.

    The stream is opened once and never re-opened (re-opening was the
    source of the WinError 50 degradation), so speech capture uses the
    already-running stream instead of a second device handle.
    """

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = threshold          # clap threshold
        self.on_wake = None                 # callable for two-clap wake
        self.on_wake_word = None            # callable for "hey jarvis"
        self._active = True                 # master detection switch
        self.suppress_wake = False          # pause wake-word/clap checks (listening)
        self.tts_busy_check = None          # callable -> is the TTS speaking now
        self.media_busy_check = None        # callable -> speaker media is playing
        self._clap_cooldown = 0.0           # last clap-wake time (double-fire guard)
        self._amp_gate = 1.0                # smoothed amplitude gate (no clicks)
        self._claps: list[float] = []
        self._stream = None
        self.device: int | None = None      # chosen live mic (auto-detected)
        self._silent = 0                    # consecutive silent windows
        # live level + adaptive noise floor
        self.level = 0.0
        self.floor = 0.004
        self.speech_threshold = 0.01
        # phrase capture state
        self.vad_event = threading.Event()
        self.capture_on = False
        self.cap: list = []
        self.raw_cap: list = []         # raw (pre-gate) audio - fed to STT for accuracy
        self.cap_lock = threading.Lock()
        self.buf: collections.deque = collections.deque(maxlen=64000)  # 4s @16k
        self.raw_buf: collections.deque = collections.deque(maxlen=64000)  # raw (pre-gate) 4s
        self._wakeword_lock = threading.Lock()
        # spectral voice-activity detector: in a LOUD room (TV/fan/music)
        # amplitude alone can't tell your voice from the noise - the gate's
        # per-bin spectral gain can. vad_on = a real voice is here NOW.
        self.vad_on = False
        self._vad_noise = 0.12       # spectral-score baseline (minimum tracker)
        self._vad_hits = 0           # consecutive speech frames (anti-spike)
        self._vad_miss = 0           # consecutive non-speech frames (release)
        self._recheck_now = threading.Event()   # wake the watchdog immediately
        # real-time noise gate - only your voice reaches VAD/STT
        self.gate = NoiseGate() if NOISE_CANCEL else None

    @property
    def ambient(self) -> float:
        return max(0.002, self.floor)

    def calibrate(self, seconds: float = 1.2) -> None:
        import sounddevice as sd
        try:
            samples = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                             channels=1, dtype="int16", device=self.device)
            sd.wait()
            amps = np.abs(samples.astype(np.float64) / 32768.0)
            seed = float(np.percentile(amps, 20))
            # CAP the calibrated floor: in a loud room (TV/fan/speakers)
            # the 20th percentile is the NOISE level, not a quiet baseline.
            # An uncapped floor made SG deaf - it gated the user's (quieter)
            # voice out right after startup in a noisy room.
            self.floor = max(0.002, min(seed * 1.6, 0.012))
            self.threshold = max(0.045, min(self.floor * 6 + 0.025, 0.10))
            print(f"[speech] noise floor={self.floor:.4f} -> clap threshold={self.threshold:.3f}", flush=True)
        except Exception as exc:  # noqa: BLE001
            # Mic busy / WDM-KS glitch (PortAudio -9999): keep the safe
            # defaults so startup NEVER dies here - the watchdog re-probes
            # the mic later and will switch to a live device if one appears.
            print(f"[speech] calibrate failed ({exc}) - using default thresholds",
                  flush=True)
            self._update(0.0)

    def _update(self, rms: float) -> None:
        # TRUE minimum-tracker: the floor drops instantly into quiet gaps
        # but rises only GLACIALLY (0.04%/frame). The old formula (1.02x +
        # 0.0003 per frame) let the floor chase any sustained sound - TV,
        # a fan, or JARVIS's own reply - until the user's voice fell below
        # the amp gate and VAD thresholds and SG went deaf (the 'captured
        # 58726 bytes but STT -> empty' pattern).
        self.floor = max(0.001, min(rms, self.floor * 1.0004))
        self.threshold = max(0.045, min(self.floor * 6 + 0.025, 0.10))
        self.speech_threshold = max(0.006, min(self.floor * 2.5, 0.03))

    def _vad_update(self, score: float) -> None:
        """Drive the spectral voice-activity detector from one frame's score.

        `score` is the gate's mean spectral gain: ~0.04-0.15 on noise frames,
        0.4+ when a real voice lifts bins above the noise spectrum. The
        baseline is a minimum tracker (falls into quiet, rises glacially) so
        a loud TV can never push it up. Speech needs 2 consecutive frames
        (spike rejection) and releases after 8 misses (~200 ms) so short
        gaps between words keep the capture alive.
        """
        # capped baseline: even a TV full of voices can never push the
        # speech threshold above ~0.30 - otherwise the user's (quieter)
        # voice stops firing the VAD and SG goes deaf in a vocal room
        self._vad_noise = max(0.05, min(score, self._vad_noise * 1.0008, 0.14))
        # Lowered minimum from 0.30 → 0.22: lets softer speech trigger VAD
        # while still above the noise gate's false-positive floor (~0.04).
        thr = max(self._vad_noise * 2.0, 0.18)
        if score > thr:
            self._vad_hits += 1
            self._vad_miss = 0
        else:
            self._vad_hits = 0
            self._vad_miss += 1
        if self._vad_hits >= 2:
            self.vad_on = True
        elif self.vad_on and self._vad_miss >= 4:
            self.vad_on = False
        if self.vad_on:
            self.vad_event.set()

    def _open(self, retries: int = 3) -> bool:
        """Open the input stream on self.device (int16 - the native format
        this Realtek/SST driver delivers audio in). Returns True on success.

        Retries a few times: the Windows WDM-KS driver throws a transient
        PortAudio -9999 when the device is momentarily busy (another app,
        or right after a device switch) - it almost always succeeds on the
        retry, which keeps SG off the "dead fallback" mic.
        """
        import sounddevice as sd
        for attempt in range(retries):
            try:
                kw = {"device": self.device} if self.device is not None else {}
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                    blocksize=400, latency="low", callback=self._callback, **kw)
                self._stream.start()
                print(f"[speech] mic stream armed on device {self.device} "
                      f"(claps + 'hey jarvis')", flush=True)
                return True
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                print(f"[speech] could not open mic stream (try {attempt + 1}): {exc}",
                      flush=True)
                if attempt + 1 < retries:
                    time.sleep(0.4)
        return False

    def request_recheck(self) -> None:
        """Wake the watchdog NOW (e.g. after a wake session captured nothing)."""
        self._recheck_now.set()

    def _swap_to(self, dev: int | None) -> None:
        """Stop the current stream and (re)open on `dev` (None = default)."""
        global _last_switch_at
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
        self.device = dev
        self.level = 0.0
        self._peak = 0.0
        self._callback_errs = 0
        ok = self._open()
        if not ok:
            _mark_bad(dev)
            if dev is not None:
                self._swap_to(None)
            return
        _bad_devices.pop(dev, None)
        _last_switch_at = time.time()
        _save_mic(dev)
        # verify internal mics only (external bluetooth devices cannot capture concurrently)
        if dev is not None:
            is_headset = False
            try:
                import sounddevice as sd
                dname = sd.query_devices(dev).get("name", "")
                if WIRELESS_PATTERNS.search(dname) or HEADSET_PATTERNS.search(dname):
                    is_headset = True
            except Exception:
                pass
            if not is_headset:
                time.sleep(0.4)
                rms = _device_hears(dev, 0.4)
                default_rms = _device_hears(None, 0.4)
                if rms < 0.0008 and default_rms > 0.0015:
                    print("[speech] new device silent while the room has sound - "
                          "falling back to the default", flush=True)
                    _mark_bad(dev)
                    self._swap_to(None)

    def _rearm(self, reason: str) -> None:
        """Re-probe for a live mic and switch to it.

        A quiet room reads EXACTLY like a dead mic on this Realtek/SST
        driver (both ~0.0000), so this NEVER downgrades: if the probe finds
        nothing live, the current device (or the default) is kept instead of
        re-opening in a churn loop - that loop is what made SG go deaf.
        The one exception: if the ARMED device has vanished (Windows
        re-enumerates audio devices and indices go stale - a stream opens
        on an index that later becomes invalid and returns pure silence),
        fall back to the system default instead of keeping a dead mic.
        """
        print(f"[speech] re-checking mic ({reason})", flush=True)
        if self.capture_on:
            # a phrase capture is live (we're listening) - swapping the stream
            # now would kill the audio mid-capture; defer to the next check
            print("[speech] capture in progress - deferring mic re-check", flush=True)
            return

        # 1. Prioritize wireless earbuds / Bluetooth headset mic immediately
        headset = find_best_headset_mic()
        if headset is not None and self.device != headset:
            print(f"[speech] wireless earbuds mic detected (device {headset}) - switching to it", flush=True)
            self._swap_to(headset)
            return

        if self.device is not None and not _device_valid(self.device):
            print("[speech] armed device vanished - falling back to the default mic",
                  flush=True)
            self._swap_to(None)
            return
        # RATE-LIMIT device switching: a healthy mic must never be swapped
        # just because a re-probe caught a different loud moment - that churn
        # is what destabilized the driver. Keep current mic if valid.
        if (self._stream is not None and (self.device is None or _device_valid(self.device))):
            print("[speech] mic healthy - keeping active stream", flush=True)
            self._peak = 0.0
            self._callback_errs = 0
            return
        found = _find_working_mic()
        if found is None:
            if self.device is not None and not _device_valid(self.device):
                print("[speech] current device invalid - falling back to the default mic",
                      flush=True)
                self._swap_to(None)
                return
            print("[speech] room is quiet - keeping the current mic", flush=True)
            self._peak = 0.0
            self._callback_errs = 0
            return
        if found == self.device and self._stream is not None:
            print("[speech] current mic is still the best - keeping it", flush=True)
            self._peak = 0.0
            self._callback_errs = 0
            return
        # a genuinely better device was heard: switch to it
        print(f"[speech] switching to live mic device {found}", flush=True)
        self._swap_to(found)

    def _watchdog(self) -> None:
        """Background health check. Dynamically monitors wireless earbuds
        connect/disconnect, streams, and callback errors."""
        last_silence_check = 0.0
        while True:
            try:
                # Dynamic wireless earbuds monitor: check if earbuds connected or disconnected
                headset = find_best_headset_mic()
                if headset is not None and self.device != headset and not self.capture_on:
                    print(f"[speech] wireless earbuds connected (device {headset}) - switching to it", flush=True)
                    self._swap_to(headset)
                    time.sleep(1.5)
                    continue
                elif self.device is not None and not _device_valid(self.device) and not self.capture_on:
                    print("[speech] current device disconnected - reverting to default mic", flush=True)
                    self._swap_to(None)
                    time.sleep(1.5)
                    continue

                # quick pass every ~2.5 s (or instantly on a recheck request)
                if self._recheck_now.wait(timeout=2.5):
                    self._recheck_now.clear()
                    self._rearm("user requested re-check")
                    continue
                self._peak = 0.0
                time.sleep(2.5)
                if self._stream is None:
                    self._rearm("no stream")
                    continue
                if self._callback_errs >= 5:
                    self._rearm(f"{self._callback_errs} callback errors")
                    continue
                if self.device is not None and not _device_valid(self.device):
                    self._rearm("device no longer exists")
                    continue
                # long-silence check on a ~90 s cadence (never on the quick pass)
                if time.time() - last_silence_check >= 90:
                    last_silence_check = time.time()
                    self._peak = 0.0
                    time.sleep(20)
                    heard = self._peak
                    if self.device is None:
                        # on the fallback default: keep hunting for a live mic
                        if heard < 0.0015:
                            self._rearm("still on the fallback mic")
                    elif heard < 0.0015:
                        self._silent += 1
                        if self._silent >= 3:      # ~4.5 min on a real device
                            self._silent = 0
                            self._rearm("long silence")
                    else:
                        self._silent = 0
            except Exception:  # noqa: BLE001
                pass

    def start(self, on_wake, on_wake_word=None) -> None:
        import sounddevice as sd  # noqa: F401  (import check)
        self.on_wake = on_wake
        self.on_wake_word = on_wake_word
        headset = find_best_headset_mic()
        if headset is not None:
            self.device = headset
            _save_mic(headset)
            try:
                hname = sd.query_devices(headset).get("name", f"device {headset}")
                print(f"[speech] wireless earbuds mic detected on startup: {hname}", flush=True)
            except Exception:
                print(f"[speech] wireless earbuds mic detected on startup (device {headset})", flush=True)
        else:
            saved = _load_saved_mic()
            if (saved is not None and _device_valid(saved) and saved not in _bad_devices):
                saved_rms = _device_hears(saved, 0.4)
                default_rms = _device_hears(None, 0.4)
                if saved_rms > 0.003 and saved_rms >= default_rms * 0.7:
                    self.device = saved
                    print(f"[speech] using live saved mic device {saved} (rms={saved_rms:.4f})", flush=True)
                else:
                    self.device = None
                    print(f"[speech] saved device {saved} is quiet (rms={saved_rms:.4f}) - using system default mic (rms={default_rms:.4f})", flush=True)
            else:
                self.device = None
                print("[speech] using system default microphone", flush=True)
        if self.threshold is None:
            try:
                self.calibrate()
            except Exception:  # noqa: BLE001
                # belt-and-braces: a calibration hiccup must never take down
                # the voice thread - the watchdog heals the mic afterwards
                print("[speech] calibrate raised - continuing with defaults",
                      flush=True)
        self._peak = 0.0
        self._callback_errs = 0
        self._open()
        threading.Thread(target=self._watchdog, daemon=True,
                         name="mic-watchdog").start()

    def pause(self) -> None:
        self._active = False

    def resume(self) -> None:
        self._active = True
        self._claps.clear()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()

    def clear_buffer(self) -> None:
        self.buf.clear()
        self.raw_buf.clear()
        with self.cap_lock:
            self.raw_cap.clear()
        self.vad_event.clear()

    def reset_gate(self) -> None:
        """Reset noise gate and amp gate instantly to 100% sensitivity."""
        self._amp_gate = 1.0
        self.vad_on = False
        self._vad_hits = 0
        self._vad_miss = 0
        self.clear_buffer()

    def recent_audio(self, seconds: float = 3.5) -> bytes:
        n = int(seconds * SAMPLE_RATE)
        data = list(self.buf)[-n:]
        if not data:
            return b""
        arr = np.asarray(data, dtype=np.float32)
        return (np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes()

    def recent_raw_audio(self, seconds: float = 3.5) -> bytes:
        """Last `seconds` of RAW (pre-gate) audio as int16 PCM.

        The barge-in path transcribes the wake clip + command on RAW audio:
        the noise-gated version distorts phonemes enough that Parakeet
        returns '' even on clear speech (the same reason _finish_phrase
        captures raw for STT).
        """
        n = int(seconds * SAMPLE_RATE)
        data = list(self.raw_buf)[-n:]
        if not data:
            return b""
        arr = np.asarray(data, dtype=np.float32)
        return (np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes()

    def recent_frames(self, seconds: float = 1.2) -> np.ndarray:
        """Last `seconds` of gated audio from the rolling buffer (float32)."""
        n = int(seconds * SAMPLE_RATE)
        data = list(self.buf)[-n:]
        if not data:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(data, dtype=np.float32)

    def recent_raw_frames(self, seconds: float = 1.2) -> np.ndarray:
        """Last `seconds` of RAW (pre-gate) audio for STT pre-roll (float32)."""
        n = int(seconds * SAMPLE_RATE)
        data = list(self.raw_buf)[-n:]
        if not data:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(data, dtype=np.float32)

    def take_wake_audio(self):
        """Grab (and clear) the audio captured with the wake word, if any."""
        with self._wakeword_lock:
            data = getattr(self, "_wake_audio", None)
            self._wake_audio = None
            return data

    def capture_phrase(self, timeout: float = 4.5, phrase_limit: float = 6.0,
                       silence_after: float = 0.35, seed=None) -> bytes:
        """Wait for speech, then capture a phrase. Returns int16 PCM or b''.

        `seed` (optional float32 audio, e.g. the wake-word clip) is prepended
        so a command spoken in the same breath as the wake word is not lost.
        """
        self.vad_event.clear()
        with self.cap_lock:
            self.cap = []
            self.raw_cap = []
            if seed is not None:
                self.cap.extend(seed)
                self.raw_cap.extend(seed)
        self.capture_on = True

        deadline = time.time() + timeout
        speech_started = seed is not None and len(seed) > int(SAMPLE_RATE * 0.4)
        while time.time() < deadline and not speech_started:
            if self.vad_event.wait(timeout=0.06):
                speech_started = True
                break
            recent = self.recent_raw_frames(0.20)
            if len(recent) and float(np.abs(recent).max()) > max(self.floor * 1.5, 0.003):
                speech_started = True
                break

        if not speech_started and seed is None:
            self.capture_on = False
            return b""

        phrase_deadline = time.time() + phrase_limit
        last_voice = time.time()
        while time.time() < phrase_deadline:
            if self.vad_on:
                last_voice = time.time()
            else:
                recent = self.recent_raw_frames(0.20)
                if len(recent) and float(np.abs(recent).max()) > max(self.floor * 1.5, 0.003):
                    last_voice = time.time()
                elif time.time() - last_voice > silence_after and len(self.cap) > int(SAMPLE_RATE * 0.3):
                    break
            time.sleep(0.02)
        self.capture_on = False
        return self._finish_phrase()

    def _finish_phrase(self) -> bytes:
        """Trim silence around the captured phrase and return int16 PCM."""
        with self.cap_lock:
            data = list(self.raw_cap) if self.raw_cap else list(self.cap)
            self.raw_cap = []
            self.cap = []
        if not data:
            return b""
        arr = np.asarray(data, dtype=np.float32)
        voice = np.abs(arr) > max(self.floor * 1.05, 0.0015)
        if voice.any():
            idxs = np.where(voice)[0]
            pad = int(SAMPLE_RATE * 0.20)
            a = max(0, idxs[0] - pad)
            b = min(len(arr), idxs[-1] + pad)
            arr = arr[a:b]
        if len(arr) < SAMPLE_RATE * 0.15:
            return b""
        return (np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self._callback_errs = getattr(self, "_callback_errs", 0) + 1
        if indata.dtype != np.float32:
            raw = indata[:, 0].astype(np.float32) / 32768.0
        else:
            raw = indata[:, 0].copy()
        r = float(np.sqrt(np.mean(raw ** 2)))
        if r > getattr(self, "_peak", 0.0):
            self._peak = r
        # Speaker output must never feed back into recognition. While JARVIS
        # speaks or browser music/video is playing, do not learn, buffer,
        # capture, wake, or clap-detect from the microphone stream.
        tts_busy = bool(self.tts_busy_check and self.tts_busy_check())
        media_busy = bool(self.media_busy_check and self.media_busy_check())
        audio_blocked = tts_busy or media_busy
        was_tts_busy = getattr(self, "_was_tts_busy", False)
        was_audio_blocked = getattr(self, "_was_audio_blocked", False)
        if (not was_audio_blocked) and audio_blocked:
            self.clear_buffer()
            self.vad_on = False
            self.vad_event.clear()
            self.capture_on = False
        if was_tts_busy and not tts_busy:
            self.reset_gate()
        self._was_tts_busy = tts_busy
        if was_audio_blocked and not audio_blocked:
            self.reset_gate()
            self._claps.clear()
            self._was_audio_blocked = False
            return
        self._was_audio_blocked = audio_blocked
        if audio_blocked:
            self.level = self.level * 0.85
            return
        raw_rms = float(np.sqrt(np.mean(raw ** 2)))
        if self.gate is not None:
            # the noise floor is learned from the RAW audio; the gate
            # decides when a frame is pure noise so it can learn the
            # noise spectrum
            self._update(raw_rms)
            clean = self.gate.process(raw, raw_rms < self.floor * 1.6)
            # spectral speech score: the gate's per-bin gain is ~0.04 on
            # noise bins and near 1 on speech bins, so mean(gain) tells
            # 'a real voice is here' apart from 'just loud TV' - pure
            # amplitude can't (both are loud). Drives the VAD AND the
            # amp gate, so ONLY the voice reaches the capture/STT.
            score = (float(np.mean(self.gate._gain))
                     if self.gate._gain is not None else 0.0)
            self._vad_update(score)
            # amp gate: opens only while a voice is present (fast
            # attack, slow release) - loud ambient never passes through
            gate_target = 1.0 if self.vad_on else 0.0
            if gate_target > self._amp_gate:
                self._amp_gate = self._amp_gate * 0.7 + gate_target * 0.3
            else:
                self._amp_gate = self._amp_gate * 0.97 + gate_target * 0.03
            clean = clean * (0.04 + 0.96 * self._amp_gate)
        else:
            clean = raw
            self._update(raw_rms)
            score = 1.0 if raw_rms > self.speech_threshold else 0.05
            self._vad_update(score)
            gate_target = 1.0 if self.vad_on else 0.0
            if gate_target > self._amp_gate:
                self._amp_gate = self._amp_gate * 0.7 + gate_target * 0.3
            else:
                self._amp_gate = self._amp_gate * 0.97 + gate_target * 0.03
            clean = clean * (0.04 + 0.96 * self._amp_gate)
        rms = float(np.sqrt(np.mean(clean ** 2)))
        self.level = self.level * 0.8 + rms * 0.2
        self.buf.extend(clean)
        self.raw_buf.extend(raw)   # always keep raw for STT pre-roll
        if self.capture_on:
            with self.cap_lock:
                self.cap.extend(clean)
                self.raw_cap.extend(raw)  # parallel raw capture for STT
        if not self._active:
            return
        now = time.time()
        # wake-word + clap checks: while JARVIS is SPEAKING these stay live so
        # "hey jarvis" (or a clap) interrupts the reply; during LISTENING they
        # are suppressed so JARVIS never wakes itself on its own voice.
        if not self.suppress_wake:
            # wake-word check: when voice is heard while idle, grab a short clip.
            # Skipped while the TTS is playing - without echo cancellation,
            # JARVIS's own voice wakes itself (that self-wake loop is what
            # made it 'not listen'). Claps still barge in at any time.
            if (self.on_wake_word is not None and not tts_busy
                    and now - getattr(self, "_last_wakeword_check", 0) > 0.60
                    and not getattr(self, "_wake_check_running", False)):
                if (self.vad_on or rms > max(0.005, self.floor * 2.2)):
                    self._last_wakeword_check = now
                    self._wake_check_running = True
                    threading.Thread(target=self._try_wake_word, daemon=True).start()
            # two-clap wake
            if rms > self.threshold:
                if now - self._clap_cooldown < 2.0:
                    self._claps.clear()   # same burst re-clapping - ignore
                elif not self._claps or now - self._claps[-1] > 0.15:
                    self._claps.append(now)
                    self._claps = [t for t in self._claps if now - t < 2.0]
                    if len(self._claps) >= 2:
                        gap = self._claps[-1] - self._claps[-2]
                        if 0.12 < gap < 1.2 and self.on_wake:
                            self._claps.clear()
                            self._clap_cooldown = now
                            print("[speech] TWO CLAPS - waking", flush=True)
                            threading.Thread(target=self.on_wake, daemon=True).start()

    def _try_wake_word(self) -> None:
        try:
            if self.capture_on:
                return   # a phrase capture (listening) is live - don't clobber it
            self._check_wake_word()
        finally:
            self._wake_check_running = False

    def _check_wake_word(self) -> None:
        with self._wakeword_lock:
            # pre-roll: capture 1.6s prior + 0.40s post for instant, responsive wake check
            pre = list(self.recent_raw_frames(1.6))
            self.capture_on = True
            time.sleep(0.40)                       # ultra-fast phrase completion
            self.capture_on = False
            with self.cap_lock:
                data = list(self.cap)
                raw_data = list(self.raw_cap)
            self.cap = []
            self.raw_cap = []
            raw_data = list(pre) + list(raw_data)
        if not data and not raw_data:
            return
        arr = np.asarray(raw_data if raw_data else data, dtype=np.float32)
        if len(arr) == 0 or float(np.max(np.abs(arr))) < max(0.015, self.floor * 2.2):
            return
        raw = (np.clip(arr, -1, 1) * 32767).astype(np.int16).tobytes()
        try:
            text = recognize(raw)
        except Exception:  # noqa: BLE001
            return
        if not text or not text.strip():
            return
        norm = re.sub(r"[^a-z0-9 ]", "", text.lower())
        print(f"[speech] wake-check clip: {norm!r}", flush=True)
        # Broad wake-word pattern covering accent variants, phonetics, and combined phrases
        if re.search(
            r"(?:^|\s)(?:k\s*|a\s*|hey\s*|ay\s*|hai\s*|hi\s*|hello\s*|ok\s*|okay\s*)?(?:jarvis|jar vis|ja rvis|jervis|jarv is|jarvs|"
            r"darvis|darv is|darvus|javis|jav is|jarbis|jar vee|jarvee|jarvies|service|travis|harvest|starfish|"
            r"spagasus|pegasus|spagas|bagas|chavis|jarve|javi)",
            norm):
            with self._wakeword_lock:
                self._wake_audio = np.asarray(raw_data if raw_data else data,
                                              dtype=np.float32)
            print("[speech] WAKE WORD - 'hey jarvis' heard", flush=True)
            threading.Thread(target=self.on_wake_word, daemon=True).start()


# ---------------------------------------------------------------------
# text-to-speech
# ---------------------------------------------------------------------

# pyttsx3 engines are COM/SAPI objects bound to the thread that created
# them: calling runAndWait() from a DIFFERENT thread hangs forever (the
# TTS worker never finishes, wait_idle spins its 30s timeout, the app
# shows 'Speaking' with no sound, and the stuck is_busy() even mutes the
# mic's VAD). So each thread gets its own engine instead of one shared
# global - the worker creates its own on first use (~1s), the warmup
# thread's engine is discarded with its thread.
_pyttsx3_local = threading.local()


def _get_pyttsx3():
    """Thread-local pyttsx3 engine.

    Re-creating it per thread (pyttsx3.init()) is a slow COM/SAPI re-init
    (~1s), but that is far cheaper than the cross-thread hang it prevents:
    a shared engine created in the warmup thread and spoken from the TTS
    worker never finishes its first runAndWait.
    """
    eng = getattr(_pyttsx3_local, "engine", None)
    if eng is None:
        import pyttsx3  # type: ignore
        eng = pyttsx3.init()
        eng.setProperty("rate", 185)
        _pyttsx3_local.engine = eng
    return eng


def warmup_pyttsx3() -> None:
    """Preload Windows SAPI voice engine so the first reply is instant."""
    try:
        t0 = time.time()
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
        pythoncom.CoInitialize()
        win32com.client.Dispatch("SAPI.SpVoice")
        print(f"[speech] SAPI voice warmed in {time.time() - t0:.1f}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[speech] SAPI warmup failed: {exc}", flush=True)


class Tts:
    """Neural TTS via edge-tts played through Windows MCI; Windows SAPI fallback."""

    def __init__(self, voice: str = TTS_VOICE) -> None:
        self.voice = voice
        self.q: queue.Queue = queue.Queue()
        self._busy = threading.Event()
        self._lock = threading.Lock()
        self._cancel = threading.Event()         # set to drop pending/next speech
        self._current = None                     # current playback mci alias
        self.on_segment = None                   # (seg_index, dur_ms) as each segment starts playing
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _play_audio(self, path: str) -> None:
        if self._cancel.is_set():
            return
        out_dev = find_best_output_device()
        if out_dev is not None:
            try:
                import sounddevice as sd
                arr, rate = _decode_audio(path)
                if len(arr) > 0:
                    sd.play(arr, rate, device=out_dev)
                    while sd.get_stream() and sd.get_stream().active and not self._cancel.is_set():
                        time.sleep(0.03)
                    if self._cancel.is_set():
                        sd.stop()
                    return
            except Exception as exc:  # noqa: BLE001
                print(f"[speech] sounddevice direct headset playback failed ({exc}) - falling back to MCI", flush=True)

        self._play_mci(path)

    def _play_mci(self, path: str) -> None:
        if self._cancel.is_set():
            return
        mci = ctypes.windll.winmm.mciSendStringW
        mci('close s', None, 0, None)
        mci(f'open "{path}" type mpegvideo alias s', None, 0, None)
        mci('play s', None, 0, None)
        buf = ctypes.create_unicode_buffer(64)
        while not self._cancel.is_set():
            mci('status s mode', buf, 64, None)
            if buf.value != 'playing':
                break
            time.sleep(0.04)
        mci('stop s', None, 0, None)
        mci('close s', None, 0, None)

    def _play_fallback(self, text: str) -> None:
        if self._cancel.is_set():
            return
        out_dev = find_best_output_device()
        if out_dev is not None:
            tmp_wav = Path(os.environ.get("TEMP", ".")) / f"spagasus_fallback_{os.getpid()}.wav"
            try:
                engine = _get_pyttsx3()
                engine.save_to_file(text, str(tmp_wav))
                engine.runAndWait()
                if tmp_wav.exists() and tmp_wav.stat().st_size > 100:
                    self._play_audio(str(tmp_wav))
                    try:
                        tmp_wav.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return
            except Exception:
                pass

        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Rate = 1
            speaker.Speak(text, 0)
        except Exception:
            engine = _get_pyttsx3()
            engine.say(text)
            engine.runAndWait()

    def _notify_segment(self, text: str, idx: int | None, dur: int | None) -> None:
        """Tell the dashboard a segment started playing, with its real
        playback duration (ms) so the text reveal stays in sync with the
        voice. Falls back to an estimate when no timing is available."""
        if self.on_segment is None or idx is None:
            return
        try:
            if not dur or dur <= 0:
                dur = int(len(text) * 68)   # ~15 chars/sec, matching the UI
            self.on_segment(idx, dur)
        except Exception:  # noqa: BLE001
            pass

    def _speak(self, text: str, idx: int | None = None) -> None:
        if self._cancel.is_set():
            return
        dur = None
        # Try high-quality neural voice first with cached audio & fast fallback
        try:
            cache_id = abs(hash(text + (self.voice or ""))) % 10000000
            tmp = Path(os.environ.get("TEMP", ".")) / f"spagasus_tts_{cache_id}.mp3"
            if tmp.exists() and tmp.stat().st_size > 500:
                self._notify_segment(text, idx, dur)
                self._play_audio(str(tmp))
                return
            mp3, words = asyncio.run(asyncio.wait_for(
                edge_tts_stream(text, self.voice), timeout=4.5))
            if self._cancel.is_set():
                return
            if mp3 and len(mp3) > 500:
                tmp.write_bytes(mp3)
                if words:
                    dur = int(words[-1]["t"] + words[-1]["d"])
                self._notify_segment(text, idx, dur)
                self._play_audio(str(tmp))
                return
        except Exception:  # noqa: BLE001
            pass
        if self._cancel.is_set():
            return
        self._notify_segment(text, idx, dur)
        self._play_fallback(text)

    def _run(self) -> None:
        while True:
            item = self.q.get()
            text, idx = item if isinstance(item, tuple) else (item, None)
            self._busy.set()
            try:
                self._cancel.clear()
                self._speak(text, idx)
            finally:
                self._busy.clear()

    def say(self, text: str, idx: int | None = None) -> None:
        """Queue one speech segment; `idx` lets the UI time its text reveal
        to this segment's real playback duration."""
        self.q.put((text, idx))

    def stop(self) -> None:
        """Interrupt current speech immediately: cut any MP3 playing now AND
        drop replies that are still being synthesized/queued, so a barge-in
        never gets a late reply spoken over it."""
        self._cancel.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        try:
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
        except Exception:  # noqa: BLE001
            pass
        try:
            mci = ctypes.windll.winmm.mciSendStringW
            mci('stop s', None, 0, None)
            mci('close s', None, 0, None)
        except Exception:  # noqa: BLE001
            pass

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancel.is_set():
                # a barge-in interrupted this reply - don't keep the session
                # stuck waiting while the interrupt wants to take over
                return
            if not self.is_busy() and self.q.empty():
                return
            time.sleep(0.05)


async def edge_tts_say(text: str, voice: str, out: str) -> None:
    import edge_tts  # type: ignore
    comm = edge_tts.Communicate(text, voice)
    await comm.save(out)


async def edge_tts_stream(text: str, voice: str):
    """Synthesize with edge-tts and return (mp3_bytes, words).

    `words` is a list of {t, d} - each word's start offset and duration in
    milliseconds, captured from the WordBoundary events. The app uses these
    to reveal the reply text in the chat in sync with the spoken audio.
    """
    import edge_tts  # type: ignore
    try:
        comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, voice)
    chunks: list[bytes] = []
    words: list[dict] = []
    async for chunk in comm.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            chunks.append(chunk["data"])
        elif ctype == "WordBoundary":
            try:
                # offset/duration are in 100-nanosecond units -> milliseconds
                t = round(int(chunk["offset"]) / 10000)
                d = round(int(chunk["duration"]) / 10000)
            except (KeyError, ValueError, TypeError):
                continue
            words.append({"t": t, "d": d})
    return b"".join(chunks), words
