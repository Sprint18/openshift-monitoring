from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.db.database import Database


PASSWORD_ITERATIONS = 600_000
SESSION_IDLE_TIMEOUT_SECONDS = 15 * 60
MAX_ACTIVE_SESSIONS = 10_000


def hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def bootstrap(self, username: str, password: str) -> bool:
        if not username or not password:
            raise ValueError("bootstrap credentials are required")
        now = datetime.now(timezone.utc).isoformat()
        salt = secrets.token_bytes(32)
        digest = hash_password(password, salt, PASSWORD_ITERATIONS)
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM portal_user LIMIT 1"
            ).fetchone()
            if existing:
                return False
            connection.execute(
                """INSERT INTO portal_user
                   (username, password_hash, salt, iterations, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, digest, salt, PASSWORD_ITERATIONS, now, now),
            )
        return True

    def verify(self, username: str, password: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT password_hash, salt, iterations FROM portal_user
                   WHERE username = ?""",
                (username,),
            ).fetchone()
        if row is None:
            # Keep unknown-user timing close to a bad-password check.
            hash_password(password, b"\0" * 32, PASSWORD_ITERATIONS)
            return False
        actual = hash_password(password, bytes(row["salt"]), int(row["iterations"]))
        return hmac.compare_digest(actual, bytes(row["password_hash"]))

    def change_password(self, username: str, current: str, new: str) -> bool:
        if not self.verify(username, current):
            return False
        salt = secrets.token_bytes(32)
        digest = hash_password(new, salt, PASSWORD_ITERATIONS)
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE portal_user SET password_hash = ?, salt = ?,
                   iterations = ?, updated_at = ? WHERE username = ?""",
                (
                    digest, salt, PASSWORD_ITERATIONS,
                    datetime.now(timezone.utc).isoformat(), username,
                ),
            )
        return True


class LocalAuthProvider:
    """Local authentication boundary; future providers can implement verify()."""

    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    def verify(self, username: str, password: str) -> bool:
        return self.repository.verify(username, password)


@dataclass
class Session:
    username: str
    created_at: float
    last_activity: float


class SessionStore:
    def __init__(
        self,
        ttl_seconds: int = SESSION_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
        max_sessions: int = MAX_ACTIVE_SESSIONS,
    ) -> None:
        if ttl_seconds <= 0 or max_sessions <= 0:
            raise ValueError("session limits must be positive")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(48)
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            if len(self._sessions) >= self._max_sessions:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].last_activity)
                self._sessions.pop(oldest, None)
            self._sessions[token] = Session(username, now, now)
        return token

    def username(self, token: str | None, *, touch: bool = True) -> str | None:
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            session = self._sessions.get(token)
            if session is None:
                return None
            if touch:
                session.last_activity = now
            return session.username

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_locked(self, now: float) -> None:
        expired = [
            key for key, value in self._sessions.items()
            if now - value.last_activity >= self.ttl_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)
