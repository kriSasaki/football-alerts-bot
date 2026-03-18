import base64
import hashlib
import hmac
import json
import time

from auth import get_admin_ids, get_web_revoked_after, is_authorized
from config import TELEGRAM_BOT_TOKEN, WEB_AUTH_SECRET, WEB_AUTH_TTL_SECONDS


def _secret() -> bytes:
    seed = WEB_AUTH_SECRET or TELEGRAM_BOT_TOKEN or "sports-alerts-web"
    return seed.encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_web_token(user_id: int, ttl_seconds: int | None = None) -> str:
    now = int(time.time())
    exp = now + int(ttl_seconds or WEB_AUTH_TTL_SECONDS)
    payload = {"uid": int(user_id), "exp": exp, "iat": now, "admin": user_id in get_admin_ids()}
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_part = _b64encode(payload_bytes)
    sig = hmac.new(_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{_b64encode(sig)}"


def verify_web_token(token: str) -> int | None:
    try:
        payload_part, sig_part = token.split(".", 1)
        expected = hmac.new(_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(sig_part)):
            return None
        payload = json.loads(_b64decode(payload_part))
        user_id = int(payload["uid"])
        exp = int(payload["exp"])
        iat = int(payload.get("iat", 0))
    except Exception:
        return None

    if exp < int(time.time()):
        return None
    if iat <= int(get_web_revoked_after(user_id)):
        return None
    if not is_authorized(user_id):
        return None
    return user_id


def token_is_admin(token: str) -> bool:
    try:
        payload_part, _ = token.split(".", 1)
        payload = json.loads(_b64decode(payload_part))
        user_id = int(payload["uid"])
        return bool(payload.get("admin")) and user_id in get_admin_ids()
    except Exception:
        return False
