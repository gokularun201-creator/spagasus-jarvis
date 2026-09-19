"""SPAGASUS JARVIS - app launcher.

Tapping the desktop icon runs this: it starts the server (if it is not
already running) and opens the dashboard in a standalone app window
(Edge/Chrome --app mode - no tabs, no address bar), so it feels like a
real application. Closing that window shuts the whole app down.

Run:  pythonw launch_app.pyw
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = int(__import__("os").environ.get("SPAGASUS_PORT", "8790"))
URL = f"http://127.0.0.1:{PORT}"


def alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1.0):
            return True
    except OSError:
        return False


def open_app_window() -> None:
    exe = None
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(candidate).exists():
            exe = candidate
            break
    if exe:
        # a dedicated profile makes every icon tap open a fresh standalone
        # app window (without it, Edge routes the launch into the running
        # browser and no new window appears)
        profile = BASE / ".edge-app-profile"
        subprocess.Popen([exe, f"--user-data-dir={profile}", "--app=" + URL,
                          "--window-size=1440,900"],
                         creationflags=0x08000000)
    else:
        import webbrowser
        webbrowser.open(URL)


def main() -> int:
    # start the supervised server if it is not already serving
    if not alive():
        pyw = BASE / "python" / "pythonw.exe"
        if not pyw.exists():
            pyw = BASE / ".venv" / "Scripts" / "pythonw.exe"
        if not pyw.exists():
            pyw = Path(sys.executable)
        subprocess.Popen([str(pyw), "run.py"], cwd=BASE, creationflags=0x08000000)
        for _ in range(60):           # wait up to ~30s for the UI
            if alive():
                break
            time.sleep(0.5)
    open_app_window()
    return 0


if __name__ == "__main__":
    sys.exit(main())
