"""SPAGASUS JARVIS - routine scheduler.

Runs enabled daily routines at their configured HH:MM time. A routine is
a list of command strings that go through the normal assistant pipeline.
"""
from __future__ import annotations

import threading
import time

from . import memory


class RoutineScheduler:
    def __init__(self, executor) -> None:
        """executor(text) runs a command through the assistant."""
        self.executor = executor
        self._stop = False
        self._ran_today: set[tuple[int, str]] = set()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()
        print("[routines] scheduler started", flush=True)

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            cur = time.strftime("%H:%M", now)
            for routine in memory.get_routines():
                if not routine["enabled"]:
                    continue
                key = (routine["id"], today)
                if key in self._ran_today:
                    continue
                # fire only when the clock matches the routine's minute - a
                # routine must never run just because its time has passed
                if cur == routine["time"]:
                    self._ran_today.add(key)
                    print(f"[routines] running '{routine['name']}'", flush=True)
                    for action in routine["actions"]:
                        try:
                            self.executor(action)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[routines] '{routine['name']}' action failed: {exc}", flush=True)
            time.sleep(20)
