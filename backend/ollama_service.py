"""SPAGASUS JARVIS - Ollama Local AI Lifecycle Manager.

Manages automatic startup and shutdown of the Ollama server and Gemma 4 12B model:
- Starts automatically when Spagasus Jarvis starts.
- Terminates automatically when Spagasus Jarvis closes.
- Preloads Gemma 4 12B with 24h keep-alive for instant responses.
"""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
_ollama_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def get_ollama_path() -> str | None:
    """Locate the ollama executable on Windows."""
    found = shutil.which("ollama")
    if found and Path(found).exists():
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Ollama" / "ollama.exe",
        Path(r"C:\Users\gokularun\AppData\Local\Programs\Ollama\ollama.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def is_ollama_alive(timeout: float = 0.5) -> bool:
    """Fast check if Ollama port 11434 is accepting connections."""
    try:
        with socket.create_connection((OLLAMA_HOST, OLLAMA_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def preload_model(model_name: str = "gemma4:12b") -> None:
    """Preload model into memory with 24h keep-alive in background."""
    def _preload():
        try:
            import json
            req = urllib.request.Request(
                f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
                data=json.dumps({"model": model_name, "keep_alive": "24h"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
        except Exception:
            pass

    threading.Thread(target=_preload, daemon=True).start()


def start_ollama(wait: bool = True, timeout: float = 12.0) -> bool:
    """Start Ollama server automatically in the background."""
    global _ollama_proc
    with _lock:
        if is_ollama_alive():
            print("[ollama] Ollama server is already running.", flush=True)
            return True

        exe = get_ollama_path()
        if not exe:
            print("[ollama] ollama.exe not found on system.", flush=True)
            return False

        print(f"[ollama] Launching Ollama server ({exe})...", flush=True)
        env = os.environ.copy()
        env["OLLAMA_KEEP_ALIVE"] = "24h"
        env["OLLAMA_ORIGINS"] = "*"
        env["OLLAMA_HOST"] = f"{OLLAMA_HOST}:{OLLAMA_PORT}"

        try:
            # 0x08000000 = CREATE_NO_WINDOW
            _ollama_proc = subprocess.Popen(
                [exe, "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
            )
        except Exception as exc:
            print(f"[ollama] Failed to start Ollama process: {exc}", flush=True)
            return False

    if wait:
        start_t = time.monotonic()
        while time.monotonic() - start_t < timeout:
            if is_ollama_alive():
                print(f"[ollama] Ollama server active on port {OLLAMA_PORT} (pid {_ollama_proc.pid if _ollama_proc else 'unknown'}).", flush=True)
                preload_model()
                return True
            time.sleep(0.3)
        print("[ollama] Ollama server started but did not respond within timeout.", flush=True)
        return is_ollama_alive()

    return True


def stop_ollama() -> None:
    """Terminate Ollama server and all child runner processes when app closes."""
    global _ollama_proc
    print("[ollama] Stopping Ollama server...", flush=True)
    with _lock:
        if _ollama_proc is not None:
            try:
                _ollama_proc.terminate()
                _ollama_proc.wait(timeout=2.0)
            except Exception:
                try:
                    _ollama_proc.kill()
                except Exception:
                    pass
            _ollama_proc = None

        # Force terminate any remaining ollama processes and runners on Windows
        for proc_name in ("ollama.exe", "ollama_llama_server.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", proc_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                    check=False,
                )
            except Exception:
                pass

    time.sleep(0.5)
    if not is_ollama_alive():
        print("[ollama] Ollama server stopped successfully.", flush=True)
    else:
        print("[ollama] Warning: Ollama port still answering.", flush=True)


# Register automatic shutdown on process exit
atexit.register(stop_ollama)
