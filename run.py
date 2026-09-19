"""SPAGASUS JARVIS - launcher + supervisor.

Starts the backend and keeps it alive: if the process dies or the UI
port stops answering, it restarts within seconds.

Run:  python run.py            (foreground)
      pythonw run.py           (background, no console)
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env() -> None:
    env_path = BASE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
PORT = int(os.environ.get("SPAGASUS_PORT", "8790"))


def port_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1.0):
            return True
    except OSError:
        return False


_mutex_handle = None


def _acquire_single_instance() -> bool:
    global _mutex_handle
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _mutex_handle = kernel32.CreateMutexW(None, False,
                                              "Local\\SpagasusJarvisSupervisor")
        return kernel32.GetLastError() != 183
    except Exception:  # noqa: BLE001
        return True


def main() -> int:
    proc: subprocess.Popen | None = None
    started_at = 0.0
    dead_checks = 0
    if not _acquire_single_instance():
        print("[spagasus] another supervisor is already running - exiting",
              flush=True)
        return 0
    try:
        (BASE / "stop.flag").unlink(missing_ok=True)
    except OSError:
        pass
    if port_alive():
        print("[spagasus] already running - leaving it alone", flush=True)
        return 0
    while True:
        if (BASE / "stop.flag").exists():
            print("[spagasus] stop requested - shutting down", flush=True)
            try:
                if proc is not None:
                    proc.kill()
            except OSError:
                pass
            return 0
        if proc is not None and proc.poll() is not None:
            if port_alive():
                print("[spagasus] server exited but another stack is serving - leaving it alone",
                      flush=True)
                return 0
            print("[spagasus] server exited - restarting", flush=True)
            proc = None
            dead_checks = 0
        # only check port responsiveness after 20s startup grace
        if proc is not None and time.time() - started_at > 20.0:
            if not port_alive():
                dead_checks += 1
                if dead_checks >= 8:
                    print("[spagasus] UI unresponsive - restarting", flush=True)
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    proc = None
                    dead_checks = 0
            else:
                dead_checks = 0
        if proc is None:
            if port_alive():
                print("[spagasus] another stack is serving - leaving it alone", flush=True)
                return 0
            try:
                (BASE / "stop.flag").unlink(missing_ok=True)
            except OSError:
                pass
            with open(BASE / "spagasus.log", "a", encoding="utf-8", errors="replace") as log:
                py_exe = str(Path(sys.executable).parent / "python.exe")
                proc = subprocess.Popen(
                    [py_exe, "-m", "backend.main"],
                    cwd=BASE, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=0x08000000,
                )
            started_at = time.time()
            dead_checks = 0
            print(f"[spagasus] started server (pid {proc.pid})", flush=True)
            time.sleep(6)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
