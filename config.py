"""
Configuration for Sports Alerts Bot v2.
Uses SofaScore API (free, no key needed).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
ALLOW_WEB_ONLY_FALLBACK = os.getenv("ALLOW_WEB_ONLY_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "").strip()
APP_NAME = os.getenv("APP_NAME", "Pulse Alerts").strip() or "Pulse Alerts"

# ─── SofaScore API ──────────────────────────────────────
SOFASCORE_BASE = "https://www.sofascore.com/api/v1"
# Rate limiting: min seconds between SofaScore requests
SOFASCORE_MIN_INTERVAL = float(os.getenv("SOFASCORE_MIN_INTERVAL", "0.50"))

# ─── Polling ────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "15"))

# ─── Cache TTL (seconds) ───────────────────────────────
LIVE_CACHE_TTL = int(os.getenv("LIVE_CACHE_TTL", "10"))       # live events cache
EVENT_CACHE_TTL = int(os.getenv("EVENT_CACHE_TTL", "20"))     # single event cache
STATS_CACHE_TTL = int(os.getenv("STATS_CACHE_TTL", "10"))     # live stats cache
SCHEDULE_CACHE_TTL = int(os.getenv("SCHEDULE_CACHE_TTL", "300"))  # daily schedule cache

# Only fixtures close to kickoff are checked individually when absent from live feed.
KICKOFF_LOOKAROUND_SECONDS = int(os.getenv("KICKOFF_LOOKAROUND_SECONDS", "900"))

# ─── Match start tolerance (seconds) ──────────────────
# If match hasn't started within this time after scheduled kickoff, warn user
MATCH_START_TOLERANCE = int(os.getenv("MATCH_START_TOLERANCE", "3600"))  # 1 hour

# ─── Database ──────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "alerts.db")

# ─── Web Push / PWA ───────────────────────────────────
WEB_APP_ENABLED = os.getenv("WEB_APP_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
WEB_APP_HOST = os.getenv("WEB_APP_HOST", "0.0.0.0")
WEB_APP_PORT = int(os.getenv("WEB_APP_PORT", "8080"))
WEB_APP_PORT_MAX_TRIES = int(os.getenv("WEB_APP_PORT_MAX_TRIES", "10"))
WEB_APP_PUBLIC_URL = os.getenv("WEB_APP_PUBLIC_URL", "").strip()
WEB_AUTH_TTL_SECONDS = int(os.getenv("WEB_AUTH_TTL_SECONDS", str(30 * 86400)))
WEB_AUTH_SECRET = os.getenv("WEB_AUTH_SECRET", "").strip()
WEB_PUSH_VAPID_PUBLIC_KEY = os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
WEB_PUSH_VAPID_PRIVATE_KEY = os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
WEB_PUSH_VAPID_SUBJECT = os.getenv("WEB_PUSH_VAPID_SUBJECT", "mailto:admin@example.com").strip()

# ─── Football stat fields ──────────────────────────────
# api_name = key in SofaScore statistics response
FOOTBALL_STATS = {
    "corners":    {"api_name": "cornerKicks",     "emoji": "🚩", "label": "Угловые"},
    "shots":      {"api_name": "totalShots",      "emoji": "🎯", "label": "Удары (всего)"},
    "shots_on":   {"api_name": "shotsOnTarget",   "emoji": "🥅", "label": "В створ"},
    "fouls":      {"api_name": "fouls",           "emoji": "⚠️",  "label": "Фолы"},
    "offsides":   {"api_name": "offsides",        "emoji": "🏳️",  "label": "Офсайды"},
    "possession": {"api_name": "ballPossession",  "emoji": "⏱️",  "label": "Владение %"},
    "yellow":     {"api_name": "yellowCards",     "emoji": "🟡", "label": "Жёлтые"},
    "red":        {"api_name": "redCards",        "emoji": "🔴", "label": "Красные"},
    "goals":      {"api_name": "goals",           "emoji": "⚽", "label": "Голы"},
}

# ─── Basketball stat fields ────────────────────────────
BASKETBALL_STATS = {
    "points":     {"api_name": "points",      "emoji": "🏀", "label": "Очки (тотал)"},
    "q1_total":   {"api_name": "q1_total",    "emoji": "1️⃣",  "label": "Очки Q1 (тотал)"},
    "q2_total":   {"api_name": "q2_total",    "emoji": "2️⃣",  "label": "Очки Q2 (тотал)"},
    "q3_total":   {"api_name": "q3_total",    "emoji": "3️⃣",  "label": "Очки Q3 (тотал)"},
    "q4_total":   {"api_name": "q4_total",    "emoji": "4️⃣",  "label": "Очки Q4 (тотал)"},
    "q1_even":    {"api_name": "q1_even",     "emoji": "🎲", "label": "Q1 чётный тотал"},
    "q2_even":    {"api_name": "q2_even",     "emoji": "🎲", "label": "Q2 чётный тотал"},
    "q1q2_even":  {"api_name": "q1q2_even",   "emoji": "🎯", "label": "Q1+Q2 обе чётные"},
    "half1":      {"api_name": "half1_total",  "emoji": "⏸️",  "label": "Очки 1-й тайм"},
}

SUPPORTED_OPERATORS = {">", "<", ">=", "<=", "=="}

# ─── Sport types ────────────────────────────────────────
SPORTS = {
    "football":   {"emoji": "⚽", "label": "Футбол",     "sofascore_slug": "football"},
    "basketball": {"emoji": "🏀", "label": "Баскетбол",  "sofascore_slug": "basketball"},
}
