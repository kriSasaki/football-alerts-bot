"""
Authorization module for Sports Alerts Bot.

Supports two modes:
  1. Whitelist mode (AUTHORIZED_USERS in .env) — only listed users can access
  2. Admin approval mode (ADMIN_USERS in .env) — new users request access,
     admins approve/reject via inline buttons

If neither AUTHORIZED_USERS nor ADMIN_USERS is set, the bot is open to everyone.
"""
import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Configuration ──────────────────────────────────────
# Comma-separated Telegram user IDs
_ADMIN_IDS_RAW = os.getenv("ADMIN_USERS", "").strip()
_AUTHORIZED_IDS_RAW = os.getenv("AUTHORIZED_USERS", "").strip()

ADMIN_IDS: set[int] = set()
if _ADMIN_IDS_RAW:
    ADMIN_IDS = {int(x.strip()) for x in _ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

AUTHORIZED_USERS: set[int] = set()
if _AUTHORIZED_IDS_RAW:
    AUTHORIZED_USERS = {int(x.strip()) for x in _AUTHORIZED_IDS_RAW.split(",") if x.strip().isdigit()}

# Admins are always authorized
AUTHORIZED_USERS |= ADMIN_IDS

# If no admins and no whitelist defined — bot is open to everyone
OPEN_ACCESS = not ADMIN_IDS and not AUTHORIZED_USERS

# ─── Persistent approved users file ────────────────────
_APPROVED_FILE = Path(os.getenv("APPROVED_USERS_FILE", "approved_users.json"))
_approved_users: set[int] = set()
_WEB_SESSIONS_FILE = Path(os.getenv("WEB_SESSIONS_FILE", "web_sessions.json"))
_web_session_state: dict[str, float] = {}

def _load_approved():
    global _approved_users
    if _APPROVED_FILE.exists():
        try:
            with open(_APPROVED_FILE, "r") as f:
                data = json.load(f)
                _approved_users = {int(x) for x in data}
        except Exception as e:
            logger.error("Failed to load approved users: %s", e)
            _approved_users = set()

def _save_approved():
    try:
        with open(_APPROVED_FILE, "w") as f:
            json.dump(list(_approved_users), f)
    except Exception as e:
        logger.error("Failed to save approved users: %s", e)

_load_approved()


def _load_web_sessions():
    global _web_session_state
    if _WEB_SESSIONS_FILE.exists():
        try:
            with open(_WEB_SESSIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                _web_session_state = {str(k): float(v) for k, v in raw.items()}
        except Exception as e:
            logger.error("Failed to load web session state: %s", e)
            _web_session_state = {}


def _save_web_sessions():
    try:
        with open(_WEB_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_web_session_state, f)
    except Exception as e:
        logger.error("Failed to save web session state: %s", e)


_load_web_sessions()

# ─── Pending requests (in-memory) ──────────────────────
pending_requests: dict[int, str] = {}  # user_id -> name


# ─── Public API ─────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    """Check if a user is authorized to use the bot."""
    if OPEN_ACCESS:
        return True
    return user_id in AUTHORIZED_USERS or user_id in _approved_users


def approve_user(user_id: int):
    """Approve a user (persisted to file)."""
    _approved_users.add(user_id)
    _save_approved()
    logger.info("User %d approved", user_id)


def reject_user(user_id: int):
    """Reject a user (just remove from pending)."""
    _approved_users.discard(user_id)
    _save_approved()
    logger.info("User %d rejected", user_id)


def revoke_user(user_id: int):
    """Revoke access for a user."""
    _approved_users.discard(user_id)
    AUTHORIZED_USERS.discard(user_id)
    revoke_web_sessions(user_id)
    _save_approved()
    logger.info("User %d revoked", user_id)


def get_admin_ids() -> set[int]:
    return ADMIN_IDS


def add_pending_request(user_id: int, name: str):
    pending_requests[user_id] = name


def get_all_authorized() -> set[int]:
    """Return all currently authorized user IDs."""
    if OPEN_ACCESS:
        return set()  # meaningless when open
    return AUTHORIZED_USERS | _approved_users


def revoke_web_sessions(user_id: int, revoked_at: float | None = None):
    import time

    _web_session_state[str(int(user_id))] = float(revoked_at or time.time())
    _save_web_sessions()


def get_web_revoked_after(user_id: int) -> float:
    return float(_web_session_state.get(str(int(user_id)), 0.0))


def get_access_state() -> dict:
    return {
        "open_access": OPEN_ACCESS,
        "admins": sorted(ADMIN_IDS),
        "authorized": sorted(AUTHORIZED_USERS),
        "approved": sorted(_approved_users),
        "pending": [{"user_id": user_id, "name": name} for user_id, name in pending_requests.items()],
        "web_session_revocations": {k: float(v) for k, v in _web_session_state.items()},
    }
