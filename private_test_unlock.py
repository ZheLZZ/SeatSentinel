"""Optional local-only recovery chord for a time-limited development test.

No chord is compiled into the app. The local record is outside the repository
and distribution. Missing, malformed, expired or mismatched records disable it.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

import config
from app_lock import UnlockGate


RECORD_PATH = config.USER_DATA_DIRECTORY / "private-test-unlock.json"


class PrivateTestUnlock:
    def __init__(self, gate: UnlockGate, expires_at: float, clock=time.time):
        self.gate = gate
        self.expires_at = expires_at
        self.clock = clock

    def attempt(self, candidate: str) -> bool:
        return self.clock() < self.expires_at and self.gate.attempt(candidate)


def load_private_test_unlock(path: Path | None = None, *, clock=time.time):
    try:
        record = json.loads((path or RECORD_PATH).read_text(encoding="utf-8"))
        if record["version"] != config.APPLICATION_VERSION:
            return None
        expires = float(record["expires_at"])
        if not clock() < expires <= clock() + 24 * 60 * 60:
            return None
        return PrivateTestUnlock(UnlockGate(record["shortcut_hash"]), expires, clock)
    except (OSError, ValueError, TypeError, KeyError):
        return None
