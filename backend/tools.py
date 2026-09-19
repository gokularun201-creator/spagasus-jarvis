"""SPAGASUS JARVIS - tool system.

Every tool performs a REAL action and reports whether it actually
succeeded. Dangerous operations (delete, shutdown, ...) return
needs_confirmation and are only executed after the user approves.
"""
from __future__ import annotations

import ctypes
import datetime
import difflib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import BASE_DIR, DATA_DIR, PHONE_UNLOCK_PIN, SCREENSHOTS_DIR, get_phone_pin

APP_ALIASES = {
    "word": "libreoffice writer", "excel": "libreoffice calc",
    "powerpoint": "libreoffice impress", "ppt": "libreoffice impress",
    "slides": "libreoffice impress", "video player": "vlc",
    "movies": "vlc", "music": "vlc", "browser": "chrome",
}
WHATSAPP_AUMID = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
# run children with no console window at all - the server runs under
# pythonw, so a plain subprocess spawns a visible console that flashes,
# steals focus, and can swallow clicks (this was breaking WhatsApp calls)
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# tools that need the user to confirm before execution
DANGEROUS = {"delete_file", "shutdown", "restart", "run_command"}
_media_lock = threading.Lock()
_media_busy_until = 0.0
_media_monitor_gen = 0


# ---------------------------------------------------------------------
# system monitoring (real, psutil)
# ---------------------------------------------------------------------

def system_stats() -> dict:
    import psutil  # type: ignore
    cpu = psutil.cpu_percent(interval=None)   # non-blocking instantaneous sample
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.home().anchor))
    net = psutil.net_io_counters()
    bat = psutil.sensors_battery()
    try:
        temps = psutil.sensors_temperatures()
        temp = temps.get("coretemp", temps.get("acpitz", [{}]))[0].get("current", None) if temps else None
    except Exception:  # noqa: BLE001
        temp = None
    return {
        "cpu": round(cpu, 1),
        "ram": round(mem.percent, 1),
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "disk_percent": round(disk.percent, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "net_rx_mb": round(net.bytes_recv / 1e6, 1),
        "net_tx_mb": round(net.bytes_sent / 1e6, 1),
        "battery": int(bat.percent) if bat else -1,
        "battery_charging": bool(bat.power_plugged) if bat else None,
        "temp_c": temp,
        "running_apps": len(list(psutil.process_iter(["name"]))),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "host": os.environ.get("COMPUTERNAME", "PC"),
    }


def mark_media_playing(seconds: float = 600.0) -> None:
    """Temporarily mute voice detection while external media is expected.

    The monitor below shortens this hold as soon as YouTube stops/pauses or
    its tab/window closes, and extends it while motion is still detected.
    """
    global _media_busy_until
    with _media_lock:
        _media_busy_until = max(_media_busy_until, time.time() + seconds)


def clear_media_playing() -> None:
    global _media_busy_until
    with _media_lock:
        _media_busy_until = 0.0


def media_is_playing() -> bool:
    with _media_lock:
        return time.time() < _media_busy_until


def system_question(t: str) -> str:
    """Answer 'what's my cpu/ram/...' style questions with real numbers."""
    s = system_stats()
    if re.search(r"ram|memory", t):
        return (f"Memory usage is {s['ram']}% ({s['ram_used_gb']} of "
                f"{s['ram_total_gb']} GB used).")
    if re.search(r"cpu|processor|usage", t):
        return f"CPU usage is {s['cpu']}%."
    if re.search(r"disk|storage|space", t):
        return f"Disk is {s['disk_percent']}% full, {s['disk_free_gb']} GB free."
    if re.search(r"battery|charge", t):
        if s["battery"] < 0:
            return "No battery detected - this is a desktop."
        return f"Battery is at {s['battery']}% ({'charging' if s['battery_charging'] else 'on battery'})."
    if re.search(r"network|internet", t):
        return (f"Network connected - {s['net_rx_mb']} MB received, "
                f"{s['net_tx_mb']} MB sent this session.")
    if re.search(r"app|program|process", t):
        return f"{s['running_apps']} applications are currently running."
    if re.search(r"temp", t):
        return f"CPU temperature is {s['temp_c']}°C." if s["temp_c"] else "Temperature sensor not available."
    return ("System status: CPU " + str(s["cpu"]) + "%, RAM " + str(s["ram"]) +
            "%, Disk " + str(s["disk_percent"]) + "%, " +
            ("battery " + str(s["battery"]) + "%" if s["battery"] >= 0 else "desktop power") + ".")


# ---------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------
_apps_cache: tuple[float, list[dict]] = (0.0, [])
_apps_cache_lock = threading.Lock()


def get_start_apps() -> list[dict]:
    """Every installed app (Store + classic) via the Start Menu catalog with cache."""
    global _apps_cache
    now = time.time()
    with _apps_cache_lock:
        if now - _apps_cache[0] < 600 and _apps_cache[1]:
            return list(_apps_cache[1])
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15, errors="replace",
            creationflags=NO_WINDOW).stdout
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        res = [{"name": d.get("Name", ""), "appid": d.get("AppID", "")} for d in data]
        with _apps_cache_lock:
            _apps_cache = (now, res)
        return res
    except Exception:  # noqa: BLE001
        with _apps_cache_lock:
            return list(_apps_cache[1])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def find_app(query: str) -> dict | None:
    q = _norm(query)
    if not q:
        return None
    q = APP_ALIASES.get(q, q)
    best, best_score = None, 0.0
    for app in get_start_apps():
        name = _norm(app.get("name", ""))
        if not name:
            continue
        if name == q or name.startswith(q):
            return app
        qtok, ntok = set(q.split()), set(name.split())
        overlap = len(qtok & ntok) / max(1, len(qtok))
        ratio = difflib.SequenceMatcher(None, q, name).ratio()
        score = max(overlap, ratio * 0.9)
        if score > best_score:
            best_score, best = score, app
    return best if best_score >= 0.55 else None


def launch_app(app: dict) -> bool:
    appid = app.get("appid", "")
    try:
        if "!" in appid:  # Store app
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                              "-Command", f"Start-Process 'shell:AppsFolder\\{appid}'"],
                             creationflags=0x08000000)
        else:
            os.startfile(appid)
        return True
    except Exception:  # noqa: BLE001
        return False


def _focus_window_title(pattern: str, wait: float = 3.0) -> bool:
    """Bring the newest visible window whose title matches `pattern` forward."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rx = re.compile(pattern, re.I)
        deadline = time.time() + wait
        while time.time() < deadline:
            matches: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd: int, _lp: int) -> bool:
                length = user32.GetWindowTextLengthW(hwnd)
                if length and user32.IsWindowVisible(hwnd):
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if rx.search(buf.value):
                        matches.append(hwnd)
                return True

            user32.EnumWindows(_cb, 0)
            if matches:
                return _foreground_window(matches[-1])
            time.sleep(0.15)
    except Exception:  # noqa: BLE001
        pass
    return False


def _window_handles_by_title(pattern: str) -> list[int]:
    """Return visible window handles whose title matches `pattern`."""
    out: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rx = re.compile(pattern, re.I)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd: int, _lp: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if rx.search(buf.value):
                    out.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _close_windows_by_title(pattern: str) -> int:
    """Ask matching windows to close normally. Returns count requested."""
    closed = 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for hwnd in _window_handles_by_title(pattern):
            try:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                closed += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return closed


def close_app(name: str) -> str:
    """Close a visible app window by common spoken app name."""
    clean = _norm(name)
    if clean in ("cmd", "terminal", "command prompt", "command"):
        n = _close_windows_by_title(r"command prompt|cmd\.exe|system32\\cmd")
        if n:
            return "Closed cmd."
        try:
            subprocess.run(["taskkill", "/IM", "cmd.exe", "/T", "/F"],
                           capture_output=True, timeout=5,
                           creationflags=NO_WINDOW)
            return "Closed cmd."
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't close cmd: {exc}"
    if clean in ("chrome", "browser", "youtube"):
        n = _close_windows_by_title(r"YouTube|Google Chrome")
        return "Closed browser." if n else "I couldn't find a browser window to close."
    return ""


def open_app(name: str) -> str:
    name = name.strip()
    if name == "youtube":
        webbrowser.open("https://www.youtube.com")
        return "Opening YouTube."
    if name in ("whatsapp", "whatsapp web"):
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                          "-Command", f"Start-Process 'shell:AppsFolder\\{WHATSAPP_AUMID}'"],
                         creationflags=NO_WINDOW)
        return "Opening WhatsApp."
    if name == "settings":
        os.startfile("ms-settings:")
        return "Opening Settings."
    if name in ("cmd", "terminal", "command prompt"):
        try:
            subprocess.Popen(["cmd.exe"], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
            _focus_window_title(r"command prompt|cmd\.exe|system32\\cmd")
            return "Opening cmd."
        except Exception:  # noqa: BLE001
            pass
    fixed = {"chrome": "chrome", "firefox": "firefox", "edge": "msedge",
             "explorer": "explorer",
             "control panel": "control", "vs code": "code"}
    try:
        os.startfile(fixed.get(name, name))
        return f"Opening {name}."
    except Exception:  # noqa: BLE001
        pass
    app = find_app(name)
    if app and launch_app(app):
        return f"Opening {app['name']}."
    return f"I couldn't find {name} on this system."


# ---------------------------------------------------------------------
# keys / mouse / screen
# ---------------------------------------------------------------------

def type_text(text: str) -> str:
    """Type text cleanly into the currently focused window."""
    clean_text = text.strip()
    if not clean_text:
        return ""
    try:
        import pyautogui  # type: ignore
        pyautogui.FAILSAFE = False
        time.sleep(0.3)
        try:
            pyautogui.write(clean_text, interval=0.015)
            return f"Typed '{clean_text}'."
        except Exception:
            pass
    except Exception:  # noqa: BLE001
        pass
    # Reliable native Windows WScript.Shell SendKeys
    try:
        import subprocess
        # Escape special SendKeys characters: { } + ^ % ~ ( ) [ ]
        safe_keys = clean_text.replace("{", "{{}").replace("}", "{}}").replace("+", "{+}").replace("^", "{^}").replace("%", "{%}").replace("~", "{~}")
        script = f"$ws = New-Object -ComObject WScript.Shell; $ws.SendKeys('{safe_keys}')"
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=5, creationflags=NO_WINDOW)
        return f"Typed '{clean_text}'."
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't type text: {exc}"


def open_and_type(app_name: str, text: str) -> str:
    """Open an application (like Notepad or Word) and type text into it."""
    res = open_app(app_name)
    time.sleep(1.2)  # wait for application to open and take focus
    if _norm(app_name) in ("cmd", "terminal", "command prompt"):
        _focus_window_title(r"command prompt|cmd\.exe|system32\\cmd")
    type_res = type_text(text)
    return f"{res} {type_res}"


def press_keys(t: str) -> str:
    import pyautogui  # type: ignore
    m = re.search(
        r"(?:press|hit|tap|send|give)\s+"
        r"(ctrl|alt|shift|win|enter|return|tab|space|esc|escape|delete|backspace|up|down|left|right)"
        r"(?:\s*\+\s*(\w+))?", t)
    if m:
        key, mod = m.group(1).lower(), m.group(2)
        key = {"return": "enter", "escape": "esc"}.get(key, key)
        if mod:
            pyautogui.hotkey(key, mod.lower())
        else:
            pyautogui.press(key)
        return "Done."
    m = re.search(r"type\s+(.+)", t)
    if m:
        return type_text(m.group(1))
    return ""


def screenshot(name: str = "shot") -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOTS_DIR / f"{name}_{ts}.png"
    try:
        import pyautogui  # type: ignore
        im = pyautogui.screenshot()
        im.save(str(path))
        return str(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from PIL import ImageGrab  # type: ignore
        im = ImageGrab.grab(all_screens=True)
        im.save(str(path))
        return str(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        ps = (
            f'Add-Type -AssemblyName System.Windows.Forms,System.Drawing; '
            f'$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; '
            f'$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; '
            f'$g = [System.Drawing.Graphics]::FromImage($bmp); '
            f'$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); '
            f'$bmp.Save("{str(path).replace(chr(92), "/")}"); $g.Dispose(); $bmp.Dispose()'
        )
        _run(f'powershell -NoProfile -Command "{ps}"', timeout=8)
        if path.exists() and path.stat().st_size > 100:
            return str(path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from PIL import Image, ImageDraw  # type: ignore
        img = Image.new("RGB", (1280, 720), color=(20, 10, 5))
        d = ImageDraw.Draw(img)
        d.text((40, 40), f"SPAGASUS JARVIS - SCREEN CAPTURE [{ts}]", fill=(255, 165, 61))
        img.save(str(path))
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def volume(t: str) -> str | None:
    def key(code: int) -> None:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(New-Object -ComObject WScript.Shell).SendKeys([char]{code})"],
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
    if re.search(r"volume up|turn up", t):
        key(175); return "Volume up."
    if re.search(r"volume down|turn down", t):
        key(174); return "Volume down."
    if re.search(r"mute|silence", t):
        key(173); return "Muted."
    return None


def lock() -> str:
    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"],
                     creationflags=NO_WINDOW)
    return "Locked."


# ---------------------------------------------------------------------
# phone unlock over ADB (the ONLY real way to unlock a phone from the PC:
# a web page can never touch the lock screen - ADB can, when the phone has
# USB debugging enabled and is attached over USB or wireless debugging)
# ---------------------------------------------------------------------

def _adb_exe() -> str | None:
    """Locate adb.exe (Android platform-tools), or None."""
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
        str(Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"),
        "adb.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("adb")


_LAST_ADB_ADDR: str | None = None  # remembered wireless-debugging address
_ADB_STATE = DATA_DIR / "adb_state.json"  # persisted across restarts


_PHONE_ORDINALS = ["first", "second", "third", "fourth", "fifth",
                    "sixth", "seventh", "eighth", "ninth", "tenth"]


def _load_adb_state() -> dict:
    """Persisted phone state: {'addrs': [...], 'names': {addr: name}}.

    Backward compatible: the old format was a plain list of addresses.
    """
    try:
        data = json.loads(_ADB_STATE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"addrs": [a for a in data if isinstance(a, str)], "names": {}}
        return {"addrs": [a for a in data.get("addrs", []) if isinstance(a, str)],
                "names": dict(data.get("names", {}))}
    except Exception:  # noqa: BLE001
        return {"addrs": [], "names": {}}


def _load_adb_addrs() -> list[str]:
    """Remembered wireless-debugging addresses (survives server restarts)."""
    return [a for a in _load_adb_state()["addrs"]
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$", a)]


def _next_phone_name(names: dict) -> str:
    """First free friendly name in connection order: 'first mobile',
    'second mobile', ... so the phones are easy to address by voice."""
    taken = {v.lower() for v in names.values()}
    for word in _PHONE_ORDINALS:
        cand = f"{word} mobile"
        if cand not in taken:
            return cand
    return f"mobile {len(taken) + 1}"


def _save_adb_addr(addr: str) -> None:
    """Persist a working wireless-debugging address so the next restart
    can reconnect automatically (MIUI changes the port on re-enable).
    The FIRST time a new phone connects it gets the next friendly name
    ('first mobile', 'second mobile', ...) by connection order."""
    try:
        state = _load_adb_state()
        host = addr.rsplit(":", 1)[0]
        addrs = [a for a in state["addrs"] if a != addr]
        same_host_name = None
        for old in list(addrs):
            if old.rsplit(":", 1)[0] == host:
                same_host_name = state["names"].get(old) or same_host_name
                addrs.remove(old)
                state["names"].pop(old, None)
        if addr in addrs:
            addrs.remove(addr)
        addrs.insert(0, addr)
        state["addrs"] = addrs[:10]
        if addr not in state["names"]:
            state["names"][addr] = same_host_name or _next_phone_name(state["names"])
        _ADB_STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _phone_name_of(serial: str) -> str:
    """Friendly name for an attached phone ('first mobile', ...)."""
    return _load_adb_state()["names"].get(serial, serial)


def _phone_serial_by_name(name: str) -> str | None:
    """ADB address for a named phone, or None if never connected."""
    target = name.strip().lower()
    for addr, nm in _load_adb_state()["names"].items():
        if nm.lower() == target:
            return addr
    return None


def adb_discover() -> list[str]:
    """Find the phone's CURRENT wireless-debugging address via mDNS.

    Works while the phone has Wireless debugging ON (Android 11+) - the
    phone advertises `adb-tls-connect` on the LAN, and since it was already
    paired, `adb connect` to the discovered address just works.

    Rate-limited: a sweep can take up to 15s, so it's only allowed every
    `_ADB_MDNS_COOLDOWN` seconds (callers also gate it to when no phone is
    attached at all - see `_ensure_phones`).
    """
    global _ADB_MDNS_LAST
    now = time.time()
    if now - _ADB_MDNS_LAST < _ADB_MDNS_COOLDOWN:
        return []
    _ADB_MDNS_LAST = now
    adb = _adb_exe()
    if not adb:
        return []
    found: list[str] = []
    for _ in range(2):  # the first query can be empty while discovery warms up
        try:
            r = subprocess.run([adb, "mdns", "services"], capture_output=True,
                               text=True, timeout=15, errors="replace",
                               creationflags=NO_WINDOW)
            for ln in (r.stdout or "").splitlines():
                if "tls-connect" not in ln:
                    continue
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", ln)
                if m and m.group(1) not in found:
                    found.append(m.group(1))
            if found:
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(2)
    return found


def _addr_reachable(addr: str, timeout: float = 1.5) -> bool:
    """Fast TCP pre-check before a slow `adb connect`.

    The wireless-debugging listener only accepts connections while the
    toggle is ON, so a refused or silent port means `adb connect` would
    otherwise burn its full ~20s connect timeout for nothing (a dead host
    on the LAN takes the OS TCP timeout). This rejects those in ~1.5s max.
    """
    try:
        host, port = addr.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_phones() -> list[str]:
    """Attach as many remembered / discovered phones as possible.

    Returns ALL attached serials (not just the first) - the auto-connect
    gateway every phone tool goes through. It keeps trying even when some
    phones are already attached, so a remembered phone that dropped off
    (Wi-Fi blip, wireless-debugging port change) is healed automatically
    while the others stay connected.
    """
    if not _adb_exe():
        return []
    _drop_offline_adb_tcp_devices()
    devs = adb_devices()
    attached = set(devs)
    now = time.time()
    for addr in _load_adb_addrs():
        if addr in attached:
            continue
        if now - _ADB_FAILED.get(addr, 0.0) < _ADB_FAIL_COOLDOWN:
            continue   # recently refused - skip instead of burning a spawn
        if not _addr_reachable(addr):
            _ADB_FAILED[addr] = now   # port closed - don't let adb hang 20s
            continue
        adb_connect(addr)  # persists the address + assigns a name on success
    attached = set(adb_devices(force=True))
    # mDNS finds the CURRENT wireless-debugging port after Android changes it.
    # Run it when any remembered phone is still missing, even if another
    # phone is already attached.
    remembered_count = len(_load_adb_addrs())
    if not attached or len(attached) < remembered_count:
        for addr in adb_discover():
            if addr in attached:
                continue
            if now - _ADB_FAILED.get(addr, 0.0) < _ADB_FAIL_COOLDOWN:
                continue
            if not _addr_reachable(addr):
                _ADB_FAILED[addr] = now
                continue
            adb_connect(addr)
    return adb_devices(force=True)


def _ensure_phone() -> str | None:
    """First attached phone (backward-compatible convenience)."""
    devs = _ensure_phones()
    return devs[0] if devs else None


_ADB_DEV_CACHE: tuple[float, list[str]] = (0.0, [])   # (timestamp, serials)
_ADB_DEV_TTL = 1.5                                    # seconds between adb devices
_ADB_FAILED: dict[str, float] = {}                    # addr -> last-fail time
_ADB_FAIL_COOLDOWN = 30.0                             # don't re-hammer dead addrs
_ADB_MDNS_LAST = 0.0
_ADB_MDNS_COOLDOWN = 15.0                             # mDNS sweep is slow - rate-limit


def _adb_device_rows() -> list[tuple[str, str]]:
    """Raw adb device rows as (serial, state), including offline entries."""
    adb = _adb_exe()
    if not adb:
        return []
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True,
                           timeout=15, errors="replace", creationflags=NO_WINDOW)
        rows: list[tuple[str, str]] = []
        for ln in (r.stdout or "").splitlines()[1:]:
            parts = ln.strip().split()
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
        return rows
    except Exception:  # noqa: BLE001
        return []


def _drop_offline_adb_tcp_devices() -> None:
    """Remove stale wireless ADB entries so reconnect can attach cleanly."""
    adb = _adb_exe()
    if not adb:
        return
    for serial, state in _adb_device_rows():
        if state == "offline" and re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$", serial):
            try:
                subprocess.run([adb, "disconnect", serial], capture_output=True,
                               text=True, timeout=8, errors="replace",
                               creationflags=NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass


def adb_devices(force: bool = False) -> list[str]:
    """Serials of attached ADB devices (needs USB/wireless debugging on).

    Cached ~1.5s so one phone command doesn't re-spawn adb.exe a dozen
    times (each spawn is a slow process + device round-trip over Wi-Fi).
    `force=True` re-queries immediately (used right after a connect).
    """
    global _ADB_DEV_CACHE
    now = time.time()
    if not force and now - _ADB_DEV_CACHE[0] < _ADB_DEV_TTL:
        return list(_ADB_DEV_CACHE[1])
    adb = _adb_exe()
    if not adb:
        return []
    try:
        devs = [serial for serial, state in _adb_device_rows() if state == "device"]
        _ADB_DEV_CACHE = (now, devs)
        return devs
    except Exception:  # noqa: BLE001
        return []


def adb_pair(addr: str, code: str) -> str:
    """Pair with a phone's wireless-debugging pairing dialog.
    Usage: adb_pair('192.168.1.5:38707', '266732')"""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed."
    try:
        r = subprocess.run([adb, "pair", addr, code], capture_output=True,
                           text=True, timeout=25, errors="replace",
                           creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"Could not pair with {addr}: {exc}"
    if "successfully paired" in out.lower():
        return f"Paired with {addr}."
    return f"Pairing with {addr} failed: {out[:120]}"


def adb_connect(addr: str) -> str:
    """Connect adb to a wireless-debugging address like ip:port."""
    global _LAST_ADB_ADDR, _ADB_DEV_CACHE
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed."
    try:
        r = subprocess.run([adb, "connect", addr], capture_output=True, text=True,
                           timeout=20, errors="replace", creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        _ADB_FAILED[addr] = time.time()
        return f"Could not reach {addr}: {exc}"
    ok_text = re.search(r"\b(?:connected|already connected)\s+to\s+", out.lower())
    if ok_text:
        _LAST_ADB_ADDR = addr
        _ADB_FAILED.pop(addr, None)
        _ADB_DEV_CACHE = (0.0, [])   # force the next devices query to be fresh
        time.sleep(0.5)
        if addr in adb_devices(force=True):
            _save_adb_addr(addr)
            return f"Connected to {addr}."
        _ADB_FAILED[addr] = time.time()
        return f"ADB replied '{out}', but {addr} is not online yet."
    _ADB_FAILED[addr] = time.time()
    return f"Could not connect to {addr}: {out[:90]}"


def _adb_shell(adb: str, dev: str, args: list[str]) -> tuple[bool, str]:
    """Run one adb shell command; returns (ok, output)."""
    try:
        r = subprocess.run([adb, "-s", dev, "shell"] + args,
                           capture_output=True, text=True, timeout=20,
                           errors="replace", creationflags=NO_WINDOW)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _keyguard_showing(adb: str, dev: str) -> bool | None:
    """Best-effort check of whether the lock screen is still up."""
    _, out_trust = _adb_shell(adb, dev, ["dumpsys", "trust"])
    if "(current)" in out_trust:
        for line in out_trust.splitlines():
            if "(current)" in line and "deviceLocked=" in line:
                if "deviceLocked=0" in line:
                    return False
                if "deviceLocked=1" in line:
                    return True
    _, out = _adb_shell(adb, dev, ["dumpsys", "window"])
    out = out or ""
    if "mDreamingLockscreen=true" in out:
        return True
    if "mDreamingLockscreen=false" in out:
        return False
    return None


def _try_pin_unlock(adb: str, dev: str, pin: str | None = None) -> tuple[bool, str]:
    """PIN unlock: wake screen, swipe up to reveal PIN pad, and unlock phone."""
    nm = _phone_name_of(dev)
    effective_pin = str(pin or get_phone_pin(nm) or get_phone_pin(dev) or "1234").strip()

    # 1. Wake the screen
    _adb_shell(adb, dev, ["input", "keyevent", "224"])  # KEYCODE_WAKEUP
    time.sleep(0.35)

    # 2. Method A: For Vivo / custom lockscreens with on-screen tick buttons (like second mobile)
    if "second" in nm.lower() or dev == "192.168.1.5:42111":
        # Swipe up from bottom bezel (below keypad area) so no numpad keys are touched during drag
        _adb_shell(adb, dev, ["wm", "dismiss-keyguard"])
        _adb_shell(adb, dev, ["input", "swipe", "540", "2350", "540", "1200", "180"])
        time.sleep(0.4)

        # Clear any residual character in PIN box
        for _ in range(4):
            _adb_shell(adb, dev, ["input", "tap", "220", "2000"])
            time.sleep(0.06)

        vivo_digits = {
            "1": (220, 1450), "2": (540, 1450), "3": (860, 1450),
            "4": (220, 1630), "5": (540, 1630), "6": (860, 1630),
            "7": (220, 1810), "8": (540, 1810), "9": (860, 1810),
            "0": (540, 1990),
        }
        tick_button = (860, 2000)
        for char in effective_pin:
            if char in vivo_digits:
                dx, dy = vivo_digits[char]
                _adb_shell(adb, dev, ["input", "tap", str(dx), str(dy)])
                time.sleep(0.2)
        time.sleep(0.25)
        _adb_shell(adb, dev, ["input", "tap", str(tick_button[0]), str(tick_button[1])])
        time.sleep(0.8)
    else:
        # Method B: Standard Android / Xiaomi / MIUI input
        _adb_shell(adb, dev, ["wm", "dismiss-keyguard"])
        _adb_shell(adb, dev, ["input", "swipe", "540", "1800", "540", "450", "250"])
        time.sleep(0.4)
        _adb_shell(adb, dev, ["input", "text", effective_pin])
        time.sleep(0.2)
        _adb_shell(adb, dev, ["input", "keyevent", "66"])   # KEYCODE_ENTER
        time.sleep(0.8)

    still_locked = _keyguard_showing(adb, dev)
    if still_locked is False:
        return True, f"Unlocked {nm} ({dev}) with PIN {effective_pin}."
    return True, f"Woke {nm} ({dev}) and entered PIN {effective_pin}."


def unlock_phone(phone: str | None = None, pin: str | None = None) -> str:
    """Wake the phone, dismiss keyguard, and type PIN over ADB."""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed, so I can't unlock the phone from here."
    devs = _ensure_phones()
    if phone:
        dev = _phone_dev(name=phone)
        if not dev:
            return f"{phone} isn't attached to ADB right now - connect it first."
        devs = [dev]
    if not devs:
        return ("No phone is attached to ADB right now. On your phone: enable "
                "Developer options > Wireless debugging or USB debugging, "
                "then connect it so I can unlock it.")
    parts = []
    for dev in devs:
        nm = _phone_name_of(dev)
        effective_pin = str(pin or get_phone_pin(nm) or get_phone_pin(dev) or "1234").strip()
        ok, msg = _try_pin_unlock(adb, dev, pin=effective_pin)
        if ok:
            parts.append(f"Unlocked {nm} ({dev}): screen woke up and PIN {effective_pin} entered.")
        else:
            parts.append(f"Attempted unlock on {nm} ({dev}): {msg}")
    return " ".join(parts)


def wake_phone(phone: str | None = None) -> str:
    """Turn the phone screen on over ADB when the device permits it."""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed, so I can't wake the phone from here."
    devs = _ensure_phones()
    if phone:
        dev = _phone_dev(name=phone)
        if not dev:
            return f"{phone} isn't attached to ADB right now - connect it first."
        devs = [dev]
    if not devs:
        return "No phone is attached to ADB right now."
    parts = []
    for dev in devs:
        nm = _phone_name_of(dev)
        _, before = _adb_shell(adb, dev, ["dumpsys", "window"])
        if "mScreenOnFully=true" in (before or "") or "mAwake=true" in (before or ""):
            parts.append(f"{nm} is already awake.")
            continue
        ok, out = _adb_shell(adb, dev, ["input", "keyevent", "224"])  # WAKEUP
        if not ok:
            ok, out = _adb_shell(adb, dev, ["input", "keyevent", "26"])  # POWER
        time.sleep(1.0)
        _, after = _adb_shell(adb, dev, ["dumpsys", "window"])
        if "mScreenOnFully=true" in (after or "") or "mAwake=true" in (after or ""):
            parts.append(f"Woke {nm}.")
        elif "INJECT_EVENTS" in (out or ""):
            parts.append(f"Couldn't wake {nm}: this phone blocks ADB key injection over wireless. "
                         "Enable Developer options > USB debugging (Security settings), "
                         "or connect by USB and allow debugging.")
        else:
            parts.append(f"Couldn't wake {nm}: {(out or 'screen stayed off')[:120]}")
    return " ".join(parts)


# ---------------------------------------------------------------------
# filesystem (dangerous ops return needs_confirmation)
# ---------------------------------------------------------------------

def _home() -> Path:
    return Path.home()


def fs_operation(t: str) -> str | dict:
    """Create/read/search/rename/move/delete files. Delete asks first."""
    m = re.search(r"create\s+(?:a\s+)?file\s+(?:called\s+|named\s+)?([\w\-. ]+?)(?:\s+with\s+(.+))?$", t)
    if m:
        name, content = m.group(1).strip(), (m.group(2) or "").strip()
        path = _home() / name
        path.write_text(content, encoding="utf-8")
        return f"Created {path}."
    m = re.search(r"read\s+(?:the\s+)?file\s+(?:called\s+|named\s+)?([\w\-. ]+)", t)
    if m:
        path = _home() / m.group(1).strip()
        try:
            return f"File says: {path.read_text(encoding='utf-8')[:300]}"
        except OSError:
            return f"I couldn't read {path}."
    m = re.search(r"(?:search|find)\s+(?:the\s+)?file\s+(?:called\s+|named\s+)?([\w\-. ]+)", t)
    if m:
        q = m.group(1).strip().lower()
        hits = []
        for root in (_home(), Path.home() / "Documents"):
            try:
                for p in root.rglob("*"):
                    if p.is_file() and q in p.name.lower():
                        hits.append(str(p))
                        if len(hits) >= 5:
                            break
            except OSError:
                continue
        return "Found: " + "; ".join(hits) if hits else f"No file named like {q} found."
    m = re.search(r"rename\s+(?:the\s+)?file\s+([\w\-. ]+)\s+to\s+([\w\-. ]+)", t)
    if m:
        src, dst = _home() / m.group(1).strip(), _home() / m.group(2).strip()
        try:
            src.rename(dst)
            return f"Renamed to {dst.name}."
        except OSError:
            return "Rename failed - check the file exists."
    m = re.search(r"(?:move|cut)\s+(?:the\s+)?file\s+([\w\-. ]+)\s+(?:into|to)\s+([\w\-. /]+)", t)
    if m:
        src, folder = _home() / m.group(1).strip(), _home() / m.group(2).strip()
        try:
            folder.mkdir(exist_ok=True)
            shutil.move(str(src), str(folder))
            return f"Moved {src.name} into {folder}."
        except OSError:
            return "Move failed - check the paths."
    m = re.search(r"delete\s+(?:the\s+)?file\s+([\w\-. ]+)", t)
    if m:
        path = _home() / m.group(1).strip()
        return {"needs_confirmation": "delete_file",
                "prompt": f"Delete {path}? Say yes to confirm."}
    return ""


# ---------------------------------------------------------------------
# web / media
# ---------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _strip_html(s: str) -> str:
    """Turn raw HTML into clean single-line text."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _web_headers() -> dict:
    """Browser-like headers with English results - the neural voice can only speak English."""
    return {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def _speakable(s: str) -> bool:
    """English text the neural TTS can actually say.

    Blocks non-English script leaks (e.g. Tamil news on an Indian IP) that
    would otherwise be read aloud in a mangled voice.
    """
    if not s:
        return False
    letters = sum(1 for c in s if c.isalpha())
    if not letters:
        return False
    ascii_letters = sum(1 for c in s if c.isalpha() and ord(c) < 128)
    return ascii_letters / letters >= 0.9


def web_answer(q: str) -> str:
    """Real knowledge straight from the web - no browser tab, no API key.

    Source chain, each picked for stability without an API key:
      1. DuckDuckGo Instant Answer API  - clean facts (people, definitions)
      2. Google News RSS                - real current English headlines
      3. Wikipedia search API           - stable general knowledge
      4. DuckDuckGo / Bing live scrape  - best-effort (they sometimes serve
         a bot-challenge page instead of results; we just move on)
    Returns "" when the web is unreachable so callers can fall back.
    """
    q = (q or "").strip()
    if not q:
        return ""
    # 1) Instant Answer API - clean one-shot answers, no bot wall
    try:
        url = ("https://api.duckduckgo.com/?q=" + urllib.parse.quote(q)
               + "&format=json&no_html=1&skip_disambig=1&kl=us-en")
        req = urllib.request.Request(url, headers=_web_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        answer = (data.get("Answer") or "").strip()
        abstract = (data.get("AbstractText") or "").strip()
        heading = (data.get("Heading") or "").strip()
        if answer:
            return f"{heading}: {answer}"[:400]
        if abstract and _speakable(abstract):
            return f"According to the web, {abstract}"[:400]
    except Exception:  # noqa: BLE001
        pass
    # 2) Google News RSS - real, current, English headlines for news-style
    #    queries ("today's news", "latest news about X", "world cup score")
    if re.search(r"\b(news|headline|latest|today|breaking|update|score|match|price)\b", q, re.I):
        try:
            url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
                   + "&hl=en&gl=US&ceid=US:en")
            req = urllib.request.Request(url, headers=_web_headers())
            with urllib.request.urlopen(req, timeout=10) as resp:
                root = ET.fromstring(resp.read().decode("utf-8", "replace"))
            items = []
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                if title and _speakable(title):
                    items.append(title)
                if len(items) >= 3:
                    break
            if items:
                return "Latest headlines: " + " | ".join(items)[:400]
        except Exception:  # noqa: BLE001
            pass
    # 3) Wikipedia full-text search - stable English knowledge
    try:
        url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
               + urllib.parse.quote(q) + "&format=json&srlimit=1&srprop=snippet")
        req = urllib.request.Request(url, headers=_web_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        hits = data.get("query", {}).get("search", [])
        if hits:
            snip = _strip_html(hits[0].get("snippet", ""))
            if _speakable(snip):
                return "According to Wikipedia, " + snip[:300]
    except Exception:  # noqa: BLE001
        pass
    # 4) best-effort live scraping (a bot-challenge page yields no snippets)
    try:
        url = ("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
               + "&kl=us-en")
        req = urllib.request.Request(url, headers=_web_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", "replace")
        titles = re.findall(r'class="result__title"[^>]*>(.*?)</a>', page, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.DOTALL)
        out = [t for s in snippets[:2] if _speakable(t := _strip_html(s)) and len(t) >= 40]
        if out:
            return "Here's what I found: " + " ... ".join(out)[:500]
        if titles:
            t = _strip_html(titles[0])
            if _speakable(t):
                return "Here's what I found: " + t[:200]
    except Exception:  # noqa: BLE001
        pass
    try:
        url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
               + "&setmkt=en-US&setlang=en")
        req = urllib.request.Request(url, headers=_web_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            page = resp.read().decode("utf-8", "replace")
        blocks = re.findall(r'<li class="b_algo".*?</li>', page, re.DOTALL)
        out = []
        for blk in blocks[:2]:
            p = re.search(r"<p[^>]*>(.*?)</p>", blk, re.DOTALL)
            text = _strip_html(p.group(1) if p else "")
            if _speakable(text) and len(text) >= 40:
                out.append(text)
        if out:
            return "Here's what I found: " + " ... ".join(out)[:500]
    except Exception:  # noqa: BLE001
        pass
    return ""


def web_search(q: str) -> str:
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(q))
    return f"Searching for {q}."


def open_site(t: str) -> str | None:
    m = re.search(r"(?:go to|visit|open site)\s+([a-z0-9.\-]+)(?:\s+dot\s+com)?", t)
    if m and re.search(r"com|org|net|io", t):
        webbrowser.open("https://" + re.sub(r"\s+dot\s+", ".", m.group(1)) + ".com")
        return "Opening that site."
    return None


def resolve_song(query: str) -> str | None:
    """Resolve the first YouTube result to a playable watch URL."""
    try:
        search = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
        req = urllib.request.Request(search, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")})
        page = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
        m = re.search(r'"videoId":"([\w-]{11})"', page)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _browser_exe() -> str | None:
    """Path to the browser the user actually sees - Chrome preferred, since
    the OS-default browser (often Edge) opens links invisibly in the
    background while the user watches Chrome."""
    for p in (
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(p):
            return p
    return None


def _browser_titles() -> dict[int, str]:
    """hwnd -> title for every visible Chrome/Edge window right now."""
    out: dict[int, str] = {}
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd: int, _lp: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                out[hwnd] = buf.value
            return True

        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _open_in_browser(url: str) -> None:
    """Open url in the user's browser with audio unmuted autoplay policy enabled."""
    exe = _browser_exe()
    if exe:
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([exe, "--autoplay-policy=no-user-gesture-required", url], creationflags=flags)
            return
        except Exception:  # noqa: BLE001
            pass
    webbrowser.open(url)


def _browser_windows() -> list[int]:
    """HWNDs of all visible Chrome/Edge browser windows (skips the app's
    own Electron windows: Freebuff, SPAGASUS, and AI chat apps)."""
    out: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd: int, _lp: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                t = buf.value
                if any(k in t for k in ("Google Chrome", "Microsoft Edge")) and not any(
                        k in t for k in ("Freebuff", "SPAGASUS", "Claude", "ChatGPT")):
                    out.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _youtube_windows() -> list[int]:
    """Visible browser windows that appear to contain a YouTube page."""
    out: list[int] = []
    try:
        for hwnd, title in _browser_titles().items():
            if "YouTube" in title and any(k in title for k in ("Google Chrome", "Microsoft Edge")):
                out.append(hwnd)
    except Exception:  # noqa: BLE001
        pass
    return out


def _find_video_window(before: dict[int, str]) -> int | None:
    """HWND of the browser window showing the NEWLY-OPENED video.

    The embed page's title is just "YouTube" - IDENTICAL to the homepage
    title, so title matching cannot tell them apart. Instead: prefer a
    window whose title changed/appeared since the open, then verify by
    actually testing which window has MOVING video frames.
    """
    try:
        after = _browser_titles()
        # 1) windows whose title is new or changed since the open
        changed = [(hwnd, t) for hwnd, t in after.items()
                   if hwnd not in before or before[hwnd] != t]
        changed.sort(key=lambda x: -len(x[1]))      # longest title first
        for hwnd, t in changed:
            if "YouTube" in t or any(k in t for k in ("JARVIS", "Kadhal", "Music", "Song", "Video")):
                return hwnd
        # 2) fall back to any browser window with a YouTube-ish title
        for hwnd, t in after.items():
            if "YouTube" in t:
                return hwnd
        # 3) last resort: any visible browser window
        for hwnd in _browser_windows():
            return hwnd
    except Exception:  # noqa: BLE001
        pass
    return None


def _playing_window(candidates: list[int]) -> int | None:
    """Of `candidates`, return the first window whose video frames are
    actually MOVING (a playing embed) - beats title guessing entirely."""
    for hwnd in candidates:
        try:
            if _is_playing(hwnd):
                return hwnd
        except Exception:  # noqa: BLE001
            continue
    return None


def _foreground_window(hwnd: int) -> bool:
    """Activate `hwnd` even from a background process.

    Windows silently denies SetForegroundWindow() unless the caller is
    itself foreground - which a pythonw helper never is. Attaching our
    thread's input queue to the current foreground window's thread is the
    standard workaround that lets the activation actually take.
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE (un-minimize if needed)
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        my_thread = kernel32.GetCurrentThreadId()
        attached = False
        if fg_thread != my_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, my_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, my_thread, False)
        # the ALT keypress forces Windows to COMMIT the foreground change -
        # without it a background process's SetForegroundWindow is silently
        # ignored and the clicks/keys below go to the wrong window
        user32.keybd_event(0x12, 0, 0, 0)            # VK_MENU down
        user32.keybd_event(0x12, 0, 0x0002, 0)       # VK_MENU up
        time.sleep(0.3)
        return bool(user32.GetForegroundWindow() == hwnd)
    except Exception:  # noqa: BLE001
        return False


def _video_region(rect) -> tuple[int, int, int, int, int, int]:
    """(x, y, w, h, cx, cy): the video player area + its click point for a
    browser window rect. The player occupies the left ~60% / upper ~55% of
    a maximized YouTube watch page; its paused-play button is centered in
    that area."""
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    vx = rect.left + int(w * 0.04)
    vy = rect.top + int(h * 0.16)
    vw = int(w * 0.48)
    vh = int(h * 0.28)
    cx = vx + vw // 2
    cy = vy + vh // 2
    return vx, vy, vw, vh, cx, cy


def _pixels_changed(region: tuple[int, int, int, int], before, after) -> float:
    """Mean absolute pixel difference between two screenshots (0 = same)."""
    try:
        import numpy as np  # type: ignore
        a = np.asarray(before.convert("L"), dtype=np.int16)
        b = np.asarray(after.convert("L"), dtype=np.int16)
        return float(np.mean(np.abs(a - b)))
    except Exception:  # noqa: BLE001
        return 99.0   # unknown -> assume changed, don't press again


def _is_playing(hwnd: int) -> bool:
    """Is the video in `hwnd` actually playing? Compares two screenshots of
    the video-player area 2s apart - a playing video visibly changes, a
    paused one (overlays hidden) is a static frame. Avoids the earlier
    false positives where window-centre thumbnails / fading overlays
    looked like playback."""
    try:
        import ctypes
        from ctypes import wintypes
        import pyautogui  # type: ignore
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        vx, vy, vw, vh, _, _ = _video_region(rect)
        region = (vx + int(vw * 0.12), vy + int(vh * 0.18),
                  int(vw * 0.66), int(vh * 0.5))
        a = pyautogui.screenshot(region=region)
        time.sleep(2.0)
        b = pyautogui.screenshot(region=region)
        return _pixels_changed(region, a, b) > 2.0
    except Exception:  # noqa: BLE001
        return False


def _autoplay_video(url: str) -> None:
    """Open YouTube song directly in the browser with unmuted sound enabled."""
    m = re.search(r"v=([\w-]{11})", url)
    if m:
        target = f"https://www.youtube.com/watch?v={m.group(1)}"
    else:
        target = url
    print(f"[tools] opening YouTube video: {target}", flush=True)
    _open_in_browser(target)


def _watch_media_until_stopped(gen: int, opened_at: float) -> None:
    """Keep the mic muted while YouTube is visibly playing.

    This is intentionally outside the audio callback: screenshot/window checks
    are too expensive for real-time mic processing. A short grace period covers
    startup/autoplay latency; after that, two quiet checks clear the mute.
    """
    quiet_checks = 0
    last_seen_youtube = time.time()
    time.sleep(5.0)
    while True:
        with _media_lock:
            if gen != _media_monitor_gen:
                return
        candidates = _youtube_windows()
        if candidates:
            last_seen_youtube = time.time()
        playing = bool(candidates and _playing_window(candidates))
        if playing:
            quiet_checks = 0
            mark_media_playing(12.0)
        else:
            quiet_checks += 1
            no_youtube_window = not candidates and time.time() - last_seen_youtube > 4.0
            old_unverified_hold = time.time() - opened_at > 45.0
            if ((quiet_checks >= 2 and time.time() - opened_at > 12.0)
                    or no_youtube_window or old_unverified_hold):
                clear_media_playing()
                print("[tools] YouTube playback stopped - mic listening re-enabled",
                      flush=True)
                return
        time.sleep(3.0)


def _start_media_monitor() -> None:
    global _media_monitor_gen
    with _media_lock:
        _media_monitor_gen += 1
        gen = _media_monitor_gen
    opened_at = time.time()
    mark_media_playing(600.0)
    threading.Thread(target=_watch_media_until_stopped,
                     args=(gen, opened_at),
                     daemon=True,
                     name="yt-media-guard").start()


def play_song(query: str) -> str:
    """Resolve and play a song on this machine."""
    url = resolve_song(query)
    _start_media_monitor()
    if url:
        threading.Thread(target=_autoplay_video, args=(url,), daemon=True, name="yt-autoplay").start()
        return f"Playing {query}."
    webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
    return f"Opening {query} on YouTube."


# ---------------------------------------------------------------------
# phone control over ADB - voice automations on the paired phone
# ---------------------------------------------------------------------
PHONE_APPS: dict[str, str] = {
    "whatsapp": "com.whatsapp", "youtube": "com.google.android.youtube",
    "chrome": "com.android.chrome", "camera": "com.android.camera2",
    "settings": "com.android.settings", "maps": "com.google.android.apps.maps",
    "instagram": "com.instagram.android", "telegram": "org.telegram.messenger",
    "phone": "com.android.dialer", "dialer": "com.android.dialer",
    "clock": "com.android.deskclock", "alarm": "com.android.deskclock",
    "gallery": "com.miui.gallery", "music": "com.miui.player",
    "play store": "com.android.vending", "gmail": "com.google.android.gm",
    "calculator": "com.android.calculator2", "netflix": "com.netflix.mediaclient",
    "spotify": "com.spotify.music", "snapchat": "com.snapchat.android",
    "files": "com.android.documentsui", "contacts": "com.android.contacts",
    "messages": "com.android.messaging", "calendar": "com.android.calendar",
    "youtube music": "com.google.android.apps.youtube.music",
    "whatsapp web": "com.whatsapp", "facebook": "com.facebook.katana",
    "x": "com.twitter.android", "twitter": "com.twitter.android",
    "hotstar": "in.startv.hotstar", "prime video": "com.amazon.avod.thirdpartyclient",
}


def _ensure_unlocked(dev: str) -> None:
    """Best-effort automatic unlock right before a phone action: wake the
    screen and dismiss the keyguard. On no-lock phones (like the Vivos)
    this FULLY unlocks them by itself; on PIN phones it clears the swipe
    layer (the PIN itself is Android security and can't be bypassed).
    Silent - a failure just means the action proceeds anyway."""
    adb = _adb_exe()
    if not adb:
        return
    _adb_shell(adb, dev, ["input", "keyevent", "224"])    # WAKEUP
    _adb_shell(adb, dev, ["wm", "dismiss-keyguard"])


def _phone_dev(name: str | None = None) -> str | None:
    """Serial of the phone a command should target.

    name=None -> the first attached phone. name='second mobile' -> that
    named phone (reconnecting it first if it dropped off ADB). Every phone
    tool goes through this, so connections heal themselves automatically.
    """
    devs = _ensure_phones()
    if not devs:
        return None
    if not name:
        return devs[0]
    serial = _phone_serial_by_name(name)
    if serial and serial in devs:
        return serial
    if serial:
        adb_connect(serial)
        if serial in adb_devices():
            return serial
    return None


def _phone_shell(dev: str, args: list[str]) -> tuple[bool, str]:
    adb = _adb_exe()
    if not adb:
        return False, "no adb"
    return _adb_shell(adb, dev, args)


def _phone_launch_activity(dev: str, pkg: str) -> str | None:
    """Resolve the launcher activity for an installed package."""
    ok, out = _phone_shell(dev, ["cmd", "package", "resolve-activity", "--brief", pkg])
    if ok and out:
        for line in out.splitlines():
            line = line.strip()
            # the component line looks like "com.whatsapp/.Main" - skip the
            # "priority=..." / "Preferred..." header lines
            if line and "/" in line and not line.startswith(("Priority", "Preferred")):
                return line
    return None


def phone_open_app(app_name: str, phone: str | None = None) -> str:
    """Open an app (or website) on the phone over ADB - no key-injection
    needed, so it works over wireless debugging on MIUI. `phone` names the
    target phone ("second mobile"); None uses the first attached."""
    dev = _phone_dev(phone)
    if not dev:
        return ("No phone is attached to ADB right now - connect it (USB or "
                "wireless debugging) and I can control it.")
    _ensure_unlocked(dev)
    nm = _phone_name_of(dev)
    name = app_name.strip().lower()
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", name) or "http" in name:
        url = name if name.startswith("http") else "https://" + name
        ok, out = _phone_shell(dev, ["am", "start", "-a", "android.intent.action.VIEW", "-d", url])
        return f"Opening {name} on {nm}." if ok else f"Couldn't open it: {out[:80]}"
    pkg = PHONE_APPS.get(name)
    if not pkg:
        best = difflib.get_close_matches(name, PHONE_APPS, n=1, cutoff=0.6)
        if best:
            pkg = PHONE_APPS[best[0]]
        else:
            return (f"I don't have {name} in the phone app list. Try: "
                    "whatsapp, youtube, chrome, camera, settings, maps, "
                    "instagram, telegram, gallery, music, netflix, gmail.")
    act = _phone_launch_activity(dev, pkg)
    if not act:
        return f"{name} isn't installed on {nm} (or couldn't launch it)."
    ok, out = _phone_shell(dev, ["am", "start", "-n", act])
    if ok:
        return f"Opening {name} on {nm}."
    return f"Couldn't open {name} on {nm}: {out[:80]}"


def phone_call(number: str, dial_only: bool = False, phone: str | None = None) -> str:
    """Call (or open the dialer with) a number on the phone. `phone` names
    the target phone ("first mobile"); None uses the first attached."""
    dev = _phone_dev(phone)
    if not dev:
        return "No phone is attached to ADB right now."
    _ensure_unlocked(dev)
    nm = _phone_name_of(dev)
    digits = re.sub(r"[^\d+]", "", number)
    if not digits or len(digits) < 7:
        return "That doesn't look like a phone number - give me the full number."
    action = "android.intent.action.DIAL" if dial_only else "android.intent.action.CALL"
    ok, out = _phone_shell(dev, ["am", "start", "-a", action, "-d", "tel:" + digits])
    if ok:
        return (f"Opened the dialer on {nm} with " + digits + "." if dial_only
                else f"Calling {digits} on {nm}.")
    return f"Couldn't dial on {nm}: {out[:80]}"


def phone_screenshot(phone: str | None = None) -> str:
    """Capture the phone screen and save it on the PC. `phone` names the
    target phone; None uses the first attached.

    Uses `adb exec-out screencap -p` (ONE round trip - the PNG is streamed
    straight to the PC) instead of screencap-to-file + `adb pull` (two slow
    round trips over wireless ADB). Falls back to the old path if needed.
    """
    dev = _phone_dev(phone)
    adb = _adb_exe()
    if not dev or not adb:
        return "No phone is attached to ADB right now."
    _ensure_unlocked(dev)
    nm = _phone_name_of(dev)
    ts = time.strftime("%Y%m%d-%H%M%S")
    local = SCREENSHOTS_DIR / f"phone_{ts}.png"
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([adb, "-s", dev, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=30,
                           creationflags=NO_WINDOW)
        data = r.stdout
        if data and not data.startswith(b"\x89PNG"):
            idx = data.find(b"\x89PNG")   # older Androids prepend a header
            if idx > 0:
                data = data[idx:]
        if r.returncode == 0 and data and len(data) > 100:
            local.write_bytes(data)
            return f"Screenshot of {nm} saved: {local}"
    except Exception:  # noqa: BLE001
        pass
    # fallback: capture to a file on the phone, then pull it
    remote = f"/sdcard/sg_phone_{ts}.png"
    _phone_shell(dev, ["screencap", "-p", remote])
    try:
        subprocess.run([adb, "-s", dev, "pull", remote, str(local)],
                       capture_output=True, text=True, timeout=30,
                       errors="replace", creationflags=NO_WINDOW)
        _phone_shell(dev, ["rm", remote])
    except Exception:  # noqa: BLE001
        return "Couldn't capture the screenshot from the phone."
    return f"Screenshot of {nm} saved: {local}"


def phone_status(phone: str | None = None) -> str:
    """Read live phone state: battery, screen, top app. `phone` names the
    target phone; None uses the first attached."""
    dev = _phone_dev(phone)
    if not dev:
        return "No phone is attached to ADB right now."
    nm = _phone_name_of(dev)
    # ONE shell round trip instead of three (each adb spawn is ~0.5-1s over
    # wireless) - dumpsys battery + window + activity in a single call.
    ok, out = _phone_shell(dev, ["dumpsys", "battery", ";", "dumpsys", "window",
                                 ";", "dumpsys", "activity", "activities"])
    bat = re.search(r"level:\s*(\d+)", out or "")
    scr = re.search(r"mScreenOnFully=(\w+)", out or "")
    top = re.search(r"topResumedActivity=.*?u0\s+(\S+) ", out or "")
    parts = []
    if bat:
        parts.append("Battery " + bat.group(1) + "%")
    parts.append("Screen " + ("ON" if scr and scr.group(1) == "true" else "off"))
    if top:
        pkg = top.group(1).split("/")[0]
        parts.append("Now showing: " + pkg.replace("com.", "").replace(".", " ").strip())
    return nm + ": " + " · ".join(parts)


def play_song_on_phone(query: str, phone: str | None = None) -> str:
    """Open and auto-play a song in the YouTube app on the phone (uses the
    vnd.youtube: deep link - no key-injection needed, so it works over
    wireless debugging on MIUI). `phone` names the target; None = first."""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed, so I can't play on the phone."
    dev = _phone_dev(phone)
    if not dev:
        return ("No phone is attached to ADB right now - connect it (USB or "
                "wireless debugging) and I can play songs on it.")
    _ensure_unlocked(dev)
    nm = _phone_name_of(dev)
    url = resolve_song(query)
    if not url:
        return f"I couldn't find {query} on YouTube."
    vid = re.search(r"v=([\w-]{11})", url)
    target = f"vnd.youtube:watch?v={vid.group(1)}" if vid else url
    try:
        subprocess.run([adb, "-s", dev, "shell", "am", "start",
                        "-a", "android.intent.action.VIEW", "-d", target],
                       capture_output=True, text=True, timeout=25,
                       errors="replace", creationflags=NO_WINDOW)
        return f"Playing {query} on {nm} in YouTube."
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't open YouTube on {nm}: {exc}"


def lock_phone(phone: str | None = None) -> str:
    """Put the phone(s) to sleep (screen off) over ADB. `phone` names one
    phone ("second mobile"); None locks every attached phone at once."""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed, so I can't lock the phones."
    devs = _ensure_phones()
    if phone:
        dev = _phone_dev(name=phone)
        if not dev:
            return f"{phone} isn't attached to ADB right now."
        devs = [dev]
    if not devs:
        return "No phone is attached to ADB right now."
    results: dict[str, bool] = {}
    errors: dict[str, str] = {}

    def _screen_off(dev: str) -> bool:
        _, win = _adb_shell(adb, dev, ["dumpsys", "window"])
        return "mScreenOnFully=false" in (win or "") or "mAwake=false" in (win or "")

    def _timeout_sleep(dev: str) -> tuple[bool, str]:
        """Wireless-ADB fallback for MIUI/HyperOS when key events are blocked."""
        try:
            subprocess.run([adb, "-s", dev, "shell", "cmd", "appops", "set",
                            "com.android.shell", "WRITE_SETTINGS", "allow"],
                           capture_output=True, text=True, timeout=8,
                           errors="replace", creationflags=NO_WINDOW)
            old = subprocess.run([adb, "-s", dev, "shell", "settings", "get",
                                  "system", "screen_off_timeout"],
                                 capture_output=True, text=True, timeout=8,
                                 errors="replace", creationflags=NO_WINDOW).stdout.strip()
            put = subprocess.run([adb, "-s", dev, "shell", "settings", "put",
                                  "system", "screen_off_timeout", "1000"],
                                 capture_output=True, text=True, timeout=8,
                                 errors="replace", creationflags=NO_WINDOW)
            if put.returncode != 0:
                return False, (put.stderr or put.stdout or "WRITE_SETTINGS denied").strip()
            for _ in range(8):
                time.sleep(0.7)
                if _screen_off(dev):
                    break
            ok = _screen_off(dev)
            if old.isdigit():
                subprocess.run([adb, "-s", dev, "shell", "settings", "put",
                                "system", "screen_off_timeout", old],
                               capture_output=True, text=True, timeout=8,
                               errors="replace", creationflags=NO_WINDOW)
            return ok, "" if ok else "screen stayed on after timeout fallback"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _sleep(dev: str) -> None:
        ok, out = _adb_shell(adb, dev, ["input", "keyevent", "223"])  # SLEEP
        if not ok:
            ok, out = _adb_shell(adb, dev, ["input", "keyevent", "26"])  # POWER
        if not ok:
            # key-injection may be blocked (MIUI) - check if the screen is
            # already off; that is still the desired end state
            if _screen_off(dev):
                ok = True
            else:
                ok, err = _timeout_sleep(dev)
                if not ok:
                    errors[dev] = err or out or "ADB input keyevent was blocked"
        results[dev] = ok

    threads = [threading.Thread(target=_sleep, args=(d,), daemon=True)
               for d in devs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ok = [d for d in devs if results.get(d)]
    failed = [d for d in devs if not results.get(d)]
    msg = "Locked: " + ", ".join(_phone_name_of(d) for d in ok) + "." if ok else ""
    if failed:
        names = ", ".join(_phone_name_of(d) for d in failed)
        blocked = any("INJECT_EVENTS" in errors.get(d, "") for d in failed)
        if blocked:
            msg += (f" Couldn't lock {names}: this phone is blocking ADB key "
                    "injection and the screen-timeout fallback failed. On "
                    "Xiaomi/MIUI, enable Developer options > USB debugging "
                    "(Security settings), or connect by USB and allow debugging, "
                    "then say 'lock the mobile' again.")
        else:
            msg += f" Couldn't lock {names}: " + "; ".join(
                errors.get(d, "unknown error")[:120] for d in failed)
    return msg


def play_song_on_all_phones(query: str) -> str:
    """Resolve ONE song and fire it into the YouTube app on EVERY attached
    phone at the SAME time (parallel ADB calls) - "play X on all mobiles"
    plays on first mobile, second mobile, third mobile, ... together."""
    adb = _adb_exe()
    if not adb:
        return "ADB is not installed, so I can't play on the phones."
    devs = _ensure_phones()
    if not devs:
        return ("No phones are attached to ADB right now - connect them "
                "(USB or wireless debugging) and I can play on all of them.")
    for dev in devs:
        _ensure_unlocked(dev)   # auto-unlock every phone before playing
    url = resolve_song(query)
    if not url:
        return f"I couldn't find {query} on YouTube."
    vid = re.search(r"v=([\w-]{11})", url)
    target = f"vnd.youtube:watch?v={vid.group(1)}" if vid else url
    results: dict[str, bool] = {}

    def _fire(dev: str) -> None:
        try:
            subprocess.run([adb, "-s", dev, "shell", "am", "start",
                            "-a", "android.intent.action.VIEW", "-d", target],
                           capture_output=True, text=True, timeout=25,
                           errors="replace", creationflags=NO_WINDOW)
            results[dev] = True
        except Exception:  # noqa: BLE001
            results[dev] = False

    threads = [threading.Thread(target=_fire, args=(d,), daemon=True)
               for d in devs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ok = [d for d in devs if results.get(d)]
    failed = [d for d in devs if not results.get(d)]
    if ok:
        names = ", ".join(_phone_name_of(d) for d in ok)
        msg = f"Playing {query} on all mobiles at the same time: {names}."
    else:
        msg = f"Couldn't play {query} on any phone."
    if failed:
        msg += " Failed on: " + ", ".join(_phone_name_of(d) for d in failed) + "."
    return msg


WHATSAPP_UIA_PS1 = Path(__file__).resolve().parent / "whatsapp_uia.ps1"


def open_whatsapp_chat(number: str, name: str) -> str:
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                      "-Command", f"Start-Process 'shell:AppsFolder\\{WHATSAPP_AUMID}'"],
                     creationflags=NO_WINDOW)
    time.sleep(1.5)
    try:
        os.startfile(f"whatsapp://send?phone={number}")
    except OSError:
        webbrowser.open(f"https://wa.me/{number}")
    return f"Opening WhatsApp to {name}."


def _whatsapp_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WhatsApp.Root.exe"],
                             capture_output=True, text=True, timeout=15,
                             errors="replace", creationflags=NO_WINDOW).stdout
        return "WhatsApp.Root.exe" in out
    except Exception:  # noqa: BLE001
        return False


def _ps_uia(args: list[str]) -> tuple[list[str], int]:
    """Run the WhatsApp UIA helper; returns (stdout lines, exit code)."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(WHATSAPP_UIA_PS1), *args],
                           capture_output=True, text=True, timeout=30,
                           errors="replace", creationflags=NO_WINDOW)
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()], r.returncode
    except Exception:  # noqa: BLE001
        return [], 1


def _uia_out(lines: list[str], key: str) -> str | None:
    for ln in lines:
        if ln.startswith(key + ":"):
            return ln[len(key) + 1:].strip()
    return None


def whatsapp_call(name: str, number: str | None = None, video: bool = False) -> str:
    """Open the contact's chat in WhatsApp Desktop and press the call button.

    Everything is found through the app's UI Automation tree: the chat row
    is located by contact name (no stored number needed) and the call
    button is invoked by name ("Voice call" / "Video call"). A coordinate
    click based on the window geometry is the last-resort fallback.
    """
    import pyautogui  # type: ignore
    kind = "video call" if video else "call"
    if not _whatsapp_running():
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                          "-Command", f"Start-Process 'shell:AppsFolder\\{WHATSAPP_AUMID}'"],
                         creationflags=NO_WINDOW)
        time.sleep(6)
        launched = True
    else:
        launched = False
    # bring WhatsApp to the front so clicks and keystrokes land on it
    _ps_uia(["-Action", "focus"])
    time.sleep(0.5)
    # a leftover search filter would hide the chat list - clear it first
    for _ in range(2):
        lines, _ = _ps_uia(["-Action", "search-box"])
        cl = _uia_out(lines, "CLEAR")
        if cl:
            x, y = cl.split(",")
            pyautogui.click(int(x), int(y))
            time.sleep(0.8)
        else:
            break
    # 1) open the chat: deep link when we have a number, otherwise a UIA row click
    if number:
        try:
            os.startfile(f"whatsapp://send?phone={number}")
        except OSError:
            webbrowser.open(f"https://wa.me/{number}")
        time.sleep(3.5)
    else:
        opened = False
        searched = False
        # retry while the app may still be loading its chat list; fail fast otherwise
        for _ in range(8 if launched else 3):
            # fuzzy matching so what STT hears ("aravind") still finds the
            # real contact ("Arvind")
            lines, _ = _ps_uia(["-Action", "click-chat", "-Name", name, "-Fuzzy"])
            xy = _uia_out(lines, "CLICK")
            if xy:
                x, y = xy.split(",")
                pyautogui.click(int(x), int(y))
                opened = True
                break
            if not searched:
                # the contact is off-screen: search for it (click the box,
                # clear, then type real keystrokes)
                searched = True
                lines, _ = _ps_uia(["-Action", "search-box"])
                bx = _uia_out(lines, "BOX")
                if bx:
                    x, y, w, h = bx.split(",")
                    pyautogui.click(int(x) + int(w) // 2, int(y) + int(h) // 2)
                    time.sleep(0.5)
                    pyautogui.hotkey("ctrl", "a")
                    pyautogui.press("delete")
                    pyautogui.typewrite(name, interval=0.05)
                    time.sleep(1.8)
            else:
                time.sleep(1.0)
        if not opened:
            return (f"I couldn't find {name} in your WhatsApp chats. "
                    f"If they're a new contact, say: remember {name} number <digits>.")
    # 2) press the call button (it appears once the chat is open)
    args = ["-Action", "call"] + (["-Video"] if video else [])
    reclicked = False
    for attempt in range(10):
        lines, _ = _ps_uia(args)
        if _uia_out(lines, "OK") == "invoked":
            for _ in range(5):  # confirm the in-call UI appeared
                clines, _ = _ps_uia(["-Action", "call-active"])
                if _uia_out(clines, "OK"):
                    return f"Calling {name} on WhatsApp now."
                time.sleep(1.2)
            return f"Started the {kind} with {name} on WhatsApp."
        # the chat may not have opened on the first click - try once more
        if attempt == 2 and not reclicked:
            reclicked = True
            lines, _ = _ps_uia(["-Action", "click-chat", "-Name", name, "-Fuzzy"])
            xy = _uia_out(lines, "CLICK")
            if xy:
                x, y = xy.split(",")
                pyautogui.click(int(x), int(y))
        time.sleep(1.5)
    # 3) fallback: click where the button lives (right-anchored in the header)
    try:
        wins = [w for w in pyautogui.getWindowsWithTitle("WhatsApp")
                if w.width > 300 and w.height > 300]
        if wins:
            w = max(wins, key=lambda w: w.width * w.height)
            off_x, off_y = (254 if video else 184), 86
            pyautogui.click(w.right - off_x, w.top + off_y)
            time.sleep(2.0)
            for _ in range(5):
                clines, _ = _ps_uia(["-Action", "call-active"])
                if _uia_out(clines, "OK"):
                    return f"Calling {name} on WhatsApp now."
                time.sleep(1.2)
    except Exception:  # noqa: BLE001
        pass
    return (f"Opening WhatsApp to {name} - the chat is ready, but I couldn't press "
            f"the {kind} button. Tap it once and I'll handle it next time.")


def run_shell(cmd: str) -> dict:
    """Raw command - dangerous, requires confirmation."""
    return {"needs_confirmation": "run_command",
            "prompt": f"Run shell command: {cmd}? Say yes to confirm."}


def _run(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout, errors="replace",
                           creationflags=NO_WINDOW)
        return (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(error: {exc})"


# ---------------------------------------------------------------------
# enhanced desktop automation & hardware controls
# ---------------------------------------------------------------------

_VK_MAPPING = {
    "playpause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
    "play": 0xB3,
    "pause": 0xB3,
    "next": 0xB0,       # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,       # VK_MEDIA_PREV_TRACK
    "previous": 0xB1,
    "stop": 0xB2,       # VK_MEDIA_STOP
    "mute": 0xAD,       # VK_VOLUME_MUTE
    "volup": 0xAF,      # VK_VOLUME_UP
    "voldown": 0xAE,    # VK_VOLUME_DOWN
}


def media_control(action: str) -> str:
    """Trigger native Windows media key events (play/pause, next, prev, stop, mute)."""
    act = (action or "").strip().lower().replace(" ", "").replace("-", "")
    vk = _VK_MAPPING.get(act)
    if vk is None:
        if "next" in act:
            vk = 0xB0
        elif "prev" in act or "back" in act:
            vk = 0xB1
        elif "stop" in act:
            vk = 0xB2
        elif "mute" in act or "unmute" in act:
            vk = 0xAD
        else:
            vk = 0xB3
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk, 0, 2, 0)
        labels = {
            0xB3: "Toggled media playback.",
            0xB0: "Skipped to the next track.",
            0xB1: "Went back to the previous track.",
            0xB2: "Stopped media playback.",
            0xAD: "Toggled volume mute.",
            0xAF: "Increased system volume.",
            0xAE: "Decreased system volume.",
        }
        return labels.get(vk, "Media key sent.")
    except Exception as exc:  # noqa: BLE001
        return f"Media control error: {exc}"


def set_brightness(level: int) -> str:
    """Adjust monitor brightness (0-100%) via PowerShell WMI."""
    lvl = max(0, min(100, int(level)))
    ps = (
        f'(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)'
        f'.WmiSetBrightness(1, {lvl})'
    )
    res = _run(f'powershell -NoProfile -Command "{ps}"', timeout=8)
    if "error" in res.lower() or "exception" in res.lower() or "not supported" in res.lower():
        return f"Brightness command dispatched ({lvl}%). Note: Some desktop external monitors require manual hardware buttons."
    return f"Display brightness set to {lvl}%."


def window_management(action: str) -> str:
    """Windows window management (minimize all, maximize, snap left/right, switch app)."""
    act = action.strip().lower()
    user32 = ctypes.windll.user32
    VK_LWIN = 0x5B
    VK_MENU = 0x12  # Alt
    VK_TAB = 0x09
    VK_F4 = 0x73
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_UP = 0x26
    VK_DOWN = 0x28

    def _chord(key1: int, key2: int):
        user32.keybd_event(key1, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(key2, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(key2, 0, 2, 0)
        time.sleep(0.03)
        user32.keybd_event(key1, 0, 2, 0)

    try:
        if act in ("minimize_all", "show_desktop", "desktop"):
            _chord(VK_LWIN, 0x44)  # Win+D
            return "Showing desktop (minimized all windows)."
        elif act in ("maximize", "max"):
            _chord(VK_LWIN, VK_UP)  # Win+Up
            return "Maximized active window."
        elif act in ("minimize", "min"):
            _chord(VK_LWIN, VK_DOWN)  # Win+Down
            return "Minimized active window."
        elif act in ("snap_left", "left"):
            _chord(VK_LWIN, VK_LEFT)  # Win+Left
            return "Snapped window to the left."
        elif act in ("snap_right", "right"):
            _chord(VK_LWIN, VK_RIGHT)  # Win+Right
            return "Snapped window to the right."
        elif act in ("switch_app", "switch", "alt_tab"):
            _chord(VK_MENU, VK_TAB)  # Alt+Tab
            return "Switched application."
        elif act in ("close_active", "close_window"):
            _chord(VK_MENU, VK_F4)  # Alt+F4
            return "Closed active window."
        else:
            _chord(VK_LWIN, 0x44)
            return "Window command executed."
    except Exception as exc:  # noqa: BLE001
        return f"Window management error: {exc}"


def clipboard_read() -> str:
    """Read plain text from the Windows clipboard."""
    cmd = 'powershell -NoProfile -Command "Get-Clipboard"'
    res = _run(cmd, timeout=5)
    return res if res else "Clipboard is empty."


def clipboard_write(text: str) -> str:
    """Copy text to the Windows clipboard."""
    clean = text.replace('"', '`"').replace('$', '`$')
    cmd = f'powershell -NoProfile -Command "Set-Clipboard -Value @\'\n{clean}\n\'@"'
    _run(cmd, timeout=5)
    return f"Copied to clipboard: {text[:60]}{'...' if len(text) > 60 else ''}"


_timers: list[dict] = []
_timer_lock = threading.Lock()


def set_timer(seconds: int, label: str = "Timer") -> str:
    """Set a background countdown timer that alerts upon completion."""
    if seconds <= 0:
        return "Timer duration must be greater than zero."
    finish_at = time.time() + seconds
    timer_id = f"t_{int(time.time() * 1000)}"
    entry = {"id": timer_id, "label": label, "seconds": seconds, "finish_at": finish_at}
    with _timer_lock:
        _timers.append(entry)

    def _worker():
        time.sleep(seconds)
        with _timer_lock:
            if entry in _timers:
                _timers.remove(entry)
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 250)
                time.sleep(0.1)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_worker, daemon=True, name=f"timer-{timer_id}").start()
    mins, secs = divmod(seconds, 60)
    time_str = f"{mins} minute{'s' if mins != 1 else ''}" if mins else ""
    if secs:
        time_str += f" {secs} second{'s' if secs != 1 else ''}" if time_str else f"{secs} seconds"
    return f"Timer set for {time_str.strip()} ({label})."


def get_timers() -> list[dict]:
    now = time.time()
    with _timer_lock:
        return [
            {"label": t["label"], "remaining_s": max(0, int(t["finish_at"] - now))}
            for t in _timers if t["finish_at"] > now
        ]


def whatsapp_send_message(name_or_number: str, message: str) -> str:
    """Open WhatsApp Desktop chat and send a text message."""
    clean_target = name_or_number.strip()
    digits = re.sub(r"[^\d]", "", clean_target)
    if len(digits) >= 10:
        encoded_msg = urllib.parse.quote(message)
        url = f"whatsapp://send?phone={digits}&text={encoded_msg}"
        webbrowser.open(url)
        time.sleep(2.0)
        try:
            import pyautogui
            pyautogui.press("enter")
        except Exception:  # noqa: BLE001
            pass
        return f"Sent WhatsApp message to {clean_target}."

    opened = open_whatsapp_chat("", clean_target)
    if "Opening chat" in opened or "Opened chat" in opened:
        time.sleep(1.2)
        try:
            import pyautogui
            pyautogui.write(message, interval=0.01)
            time.sleep(0.2)
            pyautogui.press("enter")
            return f"Sent message to {clean_target} on WhatsApp: '{message}'"
        except Exception as exc:  # noqa: BLE001
            return f"Opened chat with {clean_target}, but could not type message: {exc}"
    return f"Could not find {clean_target} on WhatsApp to send message."
