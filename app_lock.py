"""Application-lock credentials and the worker/UI handoff (no OS locking)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time


ITERATIONS = 600_000


def validate_password_record(record: str) -> bool:
    try:
        algorithm, rounds, salt, digest = record.split("$")
        return (
            algorithm == "pbkdf2_sha256"
            and rounds == str(ITERATIONS)
            and len(salt) == 32
            and len(digest) == 64
            and len(bytes.fromhex(salt)) == 16
            and len(bytes.fromhex(digest)) == 32
        )
    except (ValueError, AttributeError):
        return False


def hash_password(password: str) -> str:
    if not 8 <= len(password) <= 128 or any(
        ord(char) < 33 or ord(char) > 126 for char in password
    ):
        raise ValueError("密码须为 8–128 位英文字母、数字或英文符号（不含空格）")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, record: str) -> bool:
    if not validate_password_record(record) or len(password) > 128:
        return False
    _, _, salt, expected = record.split("$")
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), ITERATIONS
    )
    return hmac.compare_digest(actual, bytes.fromhex(expected))


class UnlockGate:
    """Snapshot credentials for this lock; rate-limit repeated guesses."""

    def __init__(self, record: str, clock=time.monotonic) -> None:
        if not validate_password_record(record):
            raise ValueError("尚未设置有效的应用锁屏密码")
        self._record = record
        self._clock = clock
        self.failures = 0
        self.retry_at = 0.0

    @property
    def retry_seconds(self) -> int:
        return max(0, int(self.retry_at - self._clock() + 0.999))

    def attempt(self, password: str) -> bool:
        if self.retry_seconds:
            return False
        if verify_password(password, self._record):
            self.failures = 0
            return True
        self.failures += 1
        if self.failures >= 5:
            self.retry_at = self._clock() + min(60, 5 * (self.failures - 4))
        return False


class AppLockSignal:
    """No Tk calls on the camera thread; never unlock on camera/input activity."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._state = "idle"
        self.error = ""

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    @property
    def busy(self) -> bool:
        return self.state in {"pending", "locked"}

    def request(self) -> None:
        with self._condition:
            if self._state not in {"pending", "locked"}:
                self.error = ""
                self._state = "pending"
                self._condition.notify_all()

    def complete(self, state: str, error: str = "") -> None:
        if state not in {"locked", "unlocked", "failed", "cancelled"}:
            raise ValueError("Invalid application-lock state")
        with self._condition:
            self._state = state
            self.error = error
            self._condition.notify_all()

    def wait(self, stop_event: threading.Event | None) -> str:
        started = time.monotonic()
        with self._condition:
            while self._state in {"pending", "locked"}:
                if stop_event is not None and stop_event.is_set():
                    # Stopping a worker never removes a displayed lock.
                    if self._state == "pending":
                        self._state = "cancelled"
                    return "cancelled"
                if self._state == "pending" and time.monotonic() - started > 15:
                    self._state = "failed"
                    self.error = "应用锁屏界面没有及时响应"
                    break
                self._condition.wait(0.1)
            return self._state
