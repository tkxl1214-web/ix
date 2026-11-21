"""Password hashing and short-lived HMAC signed bearer tokens (stdlib only)."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import SECRET_KEY


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("비밀번호는 10자 이상이어야 합니다.")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(derived).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        expected = decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), decode(salt_text), int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def issue_token(user_id: int, role: str) -> str:
    payload = {"sub": user_id, "role": role, "exp": int(time.time()) + 8 * 60 * 60}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return encoded + "." + signed


def read_token(token: str) -> dict | None:
    try:
        encoded, supplied = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(supplied, expected):
            return None
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        if int(payload["exp"]) < time.time():
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
