"""No provider or Google credentials leave this process unencrypted."""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import (
    MAX_SESSION_TTL_SECONDS,
    MIN_SESSION_TTL_SECONDS,
    settings,
)

SESSION_COOKIE = "studio_session"
_EPOCH_FILE = "session_epoch"


def _fernet():
    if not settings.encryption_key:
        raise RuntimeError("必须配置 ENCRYPTION_KEY；请运行 scripts/init_env.py")
    return Fernet(settings.encryption_key.encode())


def encrypt_secret(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("secret不能为空")
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, TypeError, ValueError) as exc:
        raise ValueError("无法解密凭据") from exc


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if any(
                x in k.lower() for x in ("secret", "token", "password", "cookie", "authorization", "api_key")
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def hash_request(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def sign_session(payload):
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET未配置")
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def session_ttl_seconds() -> int:
    """Return the configured finite lifetime, or raise if it is outside the bound."""
    try:
        value = int(settings.session_ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SESSION_TTL_SECONDS 无效") from exc
    if value < MIN_SESSION_TTL_SECONDS or value > MAX_SESSION_TTL_SECONDS:
        raise RuntimeError(
            "SESSION_TTL_SECONDS 必须介于 "
            f"{MIN_SESSION_TTL_SECONDS} 与 {MAX_SESSION_TTL_SECONDS} 之间"
        )
    return value


def _epoch_path() -> Path:
    return Path(settings.data_dir) / _EPOCH_FILE


def session_epoch() -> int:
    """Logout generation stored in DATA_DIR. Missing file means generation 0."""
    try:
        value = int(_epoch_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if value >= 0 else 0


def bump_session_epoch() -> int:
    directory = Path(settings.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    nxt = session_epoch() + 1
    temporary = directory / f".{_EPOCH_FILE}.tmp"
    temporary.write_text(str(nxt), encoding="utf-8")
    os.replace(temporary, directory / _EPOCH_FILE)
    return nxt


def issue_session(uid: str = "owner", role: str = "admin") -> tuple[str, int]:
    ttl = session_ttl_seconds()
    now = int(time.time())
    token = sign_session(
        {"uid": uid, "role": role, "iat": now, "exp": now + ttl, "sv": session_epoch()}
    )
    return token, ttl


def cookie_secure() -> bool:
    return settings.site_url.startswith("https://")


def read_session(value):
    try:
        raw, sig = value.rsplit(".", 1)
        expected = hmac.new(settings.session_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not settings.session_secret or not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) <= time.time() or payload.get("iat", 0) > time.time() + 60:
            return None
        # Tokens issued before session generations omit sv and match epoch 0 only.
        try:
            generation = int(payload.get("sv", 0))
        except (TypeError, ValueError):
            return None
        if generation != session_epoch():
            return None
        return payload
    except (ValueError, AttributeError, TypeError):
        return None
