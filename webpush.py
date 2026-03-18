import asyncio
import html
import json
import logging
import re
from urllib.parse import urljoin

from pywebpush import WebPushException, webpush

import database as db
from config import (
    WEB_APP_PUBLIC_URL,
    WEB_PUSH_VAPID_PRIVATE_KEY,
    WEB_PUSH_VAPID_PUBLIC_KEY,
    WEB_PUSH_VAPID_SUBJECT,
)

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def webpush_is_configured() -> bool:
    return bool(WEB_PUSH_VAPID_PUBLIC_KEY and WEB_PUSH_VAPID_PRIVATE_KEY and WEB_PUSH_VAPID_SUBJECT)


def strip_html_text(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    return _WS_RE.sub(" ", text).strip()


def build_push_payload(message: str, default_title: str = "Sports Alert") -> dict:
    text = strip_html_text(message)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else default_title
    body = "\n".join(lines[1:4]) if len(lines) > 1 else text
    if len(body) > 220:
        body = body[:217] + "..."
    payload = {
        "title": title[:80],
        "body": body,
        "tag": "sports-alert",
    }
    public_url = WEB_APP_PUBLIC_URL
    try:
        from webapp import get_runtime_web_url

        public_url = get_runtime_web_url() or public_url
    except Exception:
        pass
    if public_url:
        payload["url"] = urljoin(public_url.rstrip("/") + "/", "")
    return payload


async def send_user_push(user_id: int, message: str, title: str = "Sports Alert") -> tuple[int, int]:
    if not webpush_is_configured():
        logger.warning("Web push requested but VAPID keys are not configured")
        return 0, 0

    subscriptions = await db.get_web_push_subscriptions(user_id)
    if not subscriptions:
        return 0, 0

    payload = json.dumps(build_push_payload(message, default_title=title), ensure_ascii=False)
    sent = 0
    removed = 0

    for item in subscriptions:
        subscription_info = json.loads(item["subscription_json"])
        ok = await asyncio.to_thread(_send_push_sync, subscription_info, payload)
        if ok:
            sent += 1
            continue
        removed += 1
        await db.delete_web_push_subscription(item["endpoint"])

    return sent, removed


def _send_push_sync(subscription_info: dict, payload: str) -> bool:
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=WEB_PUSH_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": WEB_PUSH_VAPID_SUBJECT},
            ttl=120,
        )
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        logger.warning("Web push failed with status %s: %s", status, exc)
        return False
    except Exception as exc:
        logger.warning("Web push failed: %s", exc)
        return False
