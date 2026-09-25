from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .models import AuthConfig

PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


@dataclass(frozen=True)
class AuthSession:
    username: str
    csrf_token: str
    expires_at: int


class AuthManager:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self._failed_logins: dict[str, deque[float]] = {}

    def verify_credentials(self, username: str, password: str) -> bool:
        if not self.config.enabled or not self.config.password_hash:
            return False
        username_ok = hmac.compare_digest(username, self.config.username)
        try:
            password_ok = PASSWORD_HASHER.verify(self.config.password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            password_ok = False
        return username_ok and password_ok

    def create_session(self) -> tuple[str, AuthSession]:
        now = int(time.time())
        session = AuthSession(
            username=self.config.username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + self.config.session_ttl_seconds,
        )
        payload = {
            "u": session.username,
            "iat": now,
            "exp": session.expires_at,
            "csrf": session.csrf_token,
            "sid": secrets.token_urlsafe(18),
        }
        encoded = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = self._sign(encoded)
        return f"{encoded}.{signature}", session

    def verify_session(self, token: str | None) -> AuthSession | None:
        if not token or not self.config.enabled or not self.config.session_secret:
            return None
        try:
            encoded, signature = token.split(".", 1)
            if not hmac.compare_digest(signature, self._sign(encoded)):
                return None
            payload = json.loads(_base64url_decode(encoded))
            username = str(payload["u"])
            expires_at = int(payload["exp"])
            csrf_token = str(payload["csrf"])
        except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if expires_at <= int(time.time()):
            return None
        if not hmac.compare_digest(username, self.config.username):
            return None
        return AuthSession(username=username, csrf_token=csrf_token, expires_at=expires_at)

    def login_allowed(self, key: str) -> tuple[bool, int]:
        attempts = self._active_attempts(key)
        if len(attempts) < self.config.login_max_attempts:
            return True, 0
        retry_after = max(1, int(self.config.login_window_seconds - (time.monotonic() - attempts[0])))
        return False, retry_after

    def record_login_failure(self, key: str) -> None:
        attempts = self._active_attempts(key)
        attempts.append(time.monotonic())

    def clear_login_failures(self, key: str) -> None:
        self._failed_logins.pop(key, None)

    def _active_attempts(self, key: str) -> deque[float]:
        attempts = self._failed_logins.get(key)
        if attempts is None:
            if len(self._failed_logins) >= 10_000:
                self._failed_logins.pop(next(iter(self._failed_logins)))
            attempts = deque()
            self._failed_logins[key] = attempts
        cutoff = time.monotonic() - self.config.login_window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        return attempts

    def _sign(self, encoded: str) -> str:
        assert self.config.session_secret is not None
        signature = hmac.new(
            self.config.session_secret.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).digest()
        return _base64url_encode(signature)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    return PASSWORD_HASHER.hash(password)


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
