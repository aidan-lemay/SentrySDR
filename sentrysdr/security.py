"""Password hashing, tokens, encrypted secrets, TOTP, rate limiting, client IP."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from .config import Config

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()  # argon2id with library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    """Constant-work verify: an unknown account still costs a full hash."""
    try:
        ok = _hasher.verify(stored or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(ok and stored)


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are 256-bit random values, so a fast hash is appropriate."""
    return hashlib.sha256(token.encode()).hexdigest()


class SecretBox:
    """Encrypts secrets at rest (TOTP seeds, upload API keys)."""

    def __init__(self, key_file: Path):
        key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = key_file.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
        base64.urlsafe_b64decode(key)
        self._fernet = Fernet(key)

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str | None:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="SentrySDR")


def verify_totp(secret: str, code: str, last_step: int) -> int | None:
    """Return the matched time step, or None. Refuses to reuse a used code."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    for step in (current - 1, current, current + 1):
        if step > last_step and secrets.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


class RateLimiter:
    """In-memory sliding window. Single-process, which matches the deployment."""

    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] < now - window:
            hits.popleft()
        return hits

    def blocked(self, key: str, limit: int, window: float) -> bool:
        with self._lock:
            hits = self._prune(key, window, time.monotonic())
            blocked = len(hits) >= limit
            if not hits:
                del self._hits[key]
            return blocked

    def hit(self, key: str, window: float) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, window, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def client_ip(cfg: Config, peer: str | None, headers) -> str:
    """Resolve the real client address.

    A forwarding header is honoured only when the direct peer is a configured
    trusted proxy. cloudflared and reverse proxies connect from loopback, so a
    loopback peer is never treated as proof that the client is local.
    """
    peer = peer or "unknown"
    if not cfg.client_ip_header or not _in_networks(peer, cfg.trusted_networks):
        return peer
    value = headers.get(cfg.client_ip_header)
    if not value:
        return peer
    if cfg.client_ip_header.lower() == "x-forwarded-for":
        # Walk right to left: the rightmost untrusted hop is the real client.
        for hop in reversed([h.strip() for h in value.split(",")]):
            if not _valid_ip(hop):
                return peer
            if not _in_networks(hop, cfg.trusted_networks):
                return hop
        return peer
    value = value.strip()
    return value if _valid_ip(value) else peer


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _in_networks(ip: str, networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in networks)


def is_direct_loopback(cfg: Config, peer: str | None, headers) -> bool:
    """True only for a real local browser: loopback peer and no proxy involved."""
    if not peer:
        return False
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return False
    except ValueError:
        return False
    if cfg.client_ip_header and headers.get(cfg.client_ip_header):
        return False
    return not (headers.get("x-forwarded-for") or headers.get("cf-connecting-ip"))
