"""SPAGASUS JARVIS - persistent memory (SQLite).

Three memory tiers plus audit/routines/devices:
  short-term : recent conversation turns
  long-term  : user-approved facts and preferences ("remember ...")
  task       : previous actions and their results
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime

from .config import DB_PATH

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, role TEXT, text TEXT
            );
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE, value TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, action TEXT, result TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, source TEXT, event TEXT
            );
            CREATE TABLE IF NOT EXISTS routines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, time TEXT, actions TEXT, enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE, kind TEXT, status TEXT DEFAULT 'online',
                battery INTEGER DEFAULT -1, cpu REAL DEFAULT -1, mem REAL DEFAULT -1,
                last_seen REAL
            );
            """
        )


def audit(source: str, event: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO audit (ts, source, event) VALUES (?,?,?)",
                     (time.time(), source, event[:500]))
        conn.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 500)")


# ---------- short-term ----------
def add_turn(role: str, text: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO conversations (ts, role, text) VALUES (?,?,?)",
                     (time.time(), role, text))
        conn.execute("DELETE FROM conversations WHERE id NOT IN (SELECT id FROM conversations ORDER BY id DESC LIMIT 100)")


def recent_turns(n: int = 20) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT role, text FROM conversations WHERE id IN "
            "(SELECT id FROM conversations ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (n,)).fetchall()
        return [{"role": r["role"], "text": r["text"]} for r in rows]


# ---------- long-term ----------
def remember(key: str, value: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO facts (key, value, ts) VALUES (?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                     (key.lower().strip(), value, time.time()))


def recall() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT key, value FROM facts ORDER BY id").fetchall()
        return [{"key": r["key"], "value": r["value"]} for r in rows]


def forget(key: str) -> bool:
    with _lock, _conn() as conn:
        cur = conn.execute("DELETE FROM facts WHERE key = ?", (key.lower().strip(),))
        return cur.rowcount > 0


def clear_memory() -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM facts")
        conn.execute("DELETE FROM conversations")


# ---------- task memory ----------
def log_action(action: str, result: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("INSERT INTO actions (ts, action, result) VALUES (?,?,?)",
                     (time.time(), action[:300], result[:500]))
        conn.execute("DELETE FROM actions WHERE id NOT IN (SELECT id FROM actions ORDER BY id DESC LIMIT 100)")


def recent_actions(n: int = 20) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT ts, action, result FROM actions WHERE id IN "
            "(SELECT id FROM actions ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (n,)).fetchall()
        return [{"ts": r["ts"], "action": r["action"], "result": r["result"]} for r in rows]


# ---------- routines ----------
def add_routine(name: str, time_str: str, actions: list[str]) -> int:
    with _lock, _conn() as conn:
        cur = conn.execute("INSERT INTO routines (name, time, actions, enabled) VALUES (?,?,?,1)",
                           (name, time_str, json.dumps(actions)))
        return int(cur.lastrowid)


def get_routines() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM routines").fetchall()
        out = []
        for r in rows:
            out.append({"id": r["id"], "name": r["name"], "time": r["time"],
                        "actions": json.loads(r["actions"]), "enabled": bool(r["enabled"])})
        return out


def set_routine_enabled(routine_id: int, enabled: bool) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE routines SET enabled=? WHERE id=?", (1 if enabled else 0, routine_id))


def delete_routine(routine_id: int) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))


# ---------- devices ----------
def upsert_device(name: str, kind: str, **stats) -> None:
    now = time.time()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO devices (name, kind, status, battery, cpu, mem, last_seen) VALUES (?,?,'online',?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, status='online', "
            "battery=excluded.battery, cpu=excluded.cpu, mem=excluded.mem, last_seen=excluded.last_seen",
            (name, kind, stats.get("battery", -1), stats.get("cpu", -1),
             stats.get("mem", -1), now))
        # anything unseen for 5 minutes is offline - a short grace keeps a
        # phone 'online' through Wi-Fi blips, backgrounded tabs and the
        # throttled timers of a locked screen (the phone pings every ~15-20s)
        conn.execute("UPDATE devices SET status='offline' WHERE last_seen < ?", (now - 300,))


def get_devices() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY status='online' DESC, name").fetchall()
        return [dict(r) for r in rows]


def mark_device_offline(name: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("UPDATE devices SET status='offline' WHERE name=?", (name,))
