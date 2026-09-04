from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from app.db.database import Database


PASSWORD_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 8 * 60 * 60


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


@dataclass(frozen=True)
class Session:
    username: str
    expires_at: float


class SessionStore:
    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(48)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = Session(username, time.time() + self.ttl_seconds)
        return token

    def username(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            self._purge_locked()
            session = self._sessions.get(token)
            return session.username if session else None

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)
