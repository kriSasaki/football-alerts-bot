"""
SofaScore API client — Football + Basketball.
Free, no API key needed.

Uses curl_cffi to impersonate Chrome TLS fingerprint
and bypass Cloudflare protection on SofaScore.

Key optimization: in-memory cache with TTL so that multiple alerts
on the same match / same poll cycle share a single HTTP request.
"""
import asyncio
import logging
import time
import json
from datetime import datetime, timezone

from config import SOFASCORE_BASE, SOFASCORE_MIN_INTERVAL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
#  HTTP CLIENT — curl_cffi with Chrome impersonation
# ═══════════════════════════════════════════════════════

# Try curl_cffi first (best Cloudflare bypass), fall back to aiohttp
_USE_CURL_CFFI = False
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _USE_CURL_CFFI = True
    logger.info("Using curl_cffi for SofaScore requests (Chrome impersonation)")
except ImportError:
    logger.warning("curl_cffi not installed, falling back to aiohttp (may get blocked by Cloudflare)")

_last_request_time = 0.0
_rate_lock = asyncio.Lock()

# Shared async session (reuse connections)
_curl_session: "CurlAsyncSession | None" = None


import os

# Proxy support: set SOFASCORE_PROXY in .env if SofaScore blocks datacenter IPs
_PROXY = os.getenv("SOFASCORE_PROXY", "").strip() or None
if _PROXY:
    logger.info("Using proxy for SofaScore: %s", _PROXY.split("@")[-1] if "@" in _PROXY else _PROXY)

# Flaresolverr support: run Cloudflare-bypassing headless Chrome
# docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
_FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "").strip() or None
if _FLARESOLVERR_URL:
    logger.info("Using Flaresolverr at %s", _FLARESOLVERR_URL)


async def _get_curl_session():
    global _curl_session
    if _curl_session is None and _USE_CURL_CFFI:
        kwargs = {
            "impersonate": "chrome124",
            "verify": False,
            "timeout": 20,
        }
        if _PROXY:
            kwargs["proxy"] = _PROXY
        _curl_session = CurlAsyncSession(**kwargs)
    return _curl_session


async def close_session():
    """Call on shutdown."""
    global _curl_session
    if _curl_session:
        await _curl_session.close()
        _curl_session = None


_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


async def _rate_wait():
    """Ensure minimum interval between requests."""
    global _last_request_time
    async with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < SOFASCORE_MIN_INTERVAL:
            await asyncio.sleep(SOFASCORE_MIN_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


async def _get(endpoint: str, params: dict | None = None) -> dict | list | None:
    """GET request to SofaScore API with rate limiting and Cloudflare bypass."""
    await _rate_wait()
    url = f"{SOFASCORE_BASE}/{endpoint}"

    # Try Flaresolverr first if configured
    if _FLARESOLVERR_URL:
        result = await _get_flaresolverr(url, params)
        if result is not None:
            return result
        # Fall through to direct request

    if _USE_CURL_CFFI:
        return await _get_curl(url, params)
    else:
        return await _get_aiohttp(url, params)


# FlareSolverr session cookies (obtained after passing Cloudflare challenge)
_flaresolverr_cookies: dict = {}
_flaresolverr_ua: str = ""
_flaresolverr_cookies_time: float = 0
_COOKIE_TTL = 1800  # refresh cookies every 30 min


async def _ensure_flaresolverr_cookies():
    """Visit sofascore.com main page via FlareSolverr to get Cloudflare cookies."""
    global _flaresolverr_cookies, _flaresolverr_ua, _flaresolverr_cookies_time

    if _flaresolverr_cookies and (time.time() - _flaresolverr_cookies_time < _COOKIE_TTL):
        return  # cookies still fresh

    logger.info("FlareSolverr: obtaining Cloudflare cookies...")
    payload = json.dumps({
        "cmd": "request.get",
        "url": "https://www.sofascore.com/",
        "maxTimeout": 60000,
    })

    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            resp = await session.post(
                _FLARESOLVERR_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=65,
            )
            wrapper = json.loads(resp.content)

        if wrapper.get("status") != "ok":
            logger.error("FlareSolverr cookie fetch failed: %s", wrapper.get("message"))
            return

        solution = wrapper.get("solution", {})
        cookies_list = solution.get("cookies", [])
        _flaresolverr_ua = solution.get("userAgent", "")
        _flaresolverr_cookies = {c["name"]: c["value"] for c in cookies_list if "name" in c and "value" in c}
        _flaresolverr_cookies_time = time.time()
        logger.info("FlareSolverr: got %d cookies, UA: %s", len(_flaresolverr_cookies), _flaresolverr_ua[:50])

    except Exception as e:
        logger.error("FlareSolverr cookie error: %s", e)


async def _get_flaresolverr(url: str, params: dict | None = None) -> dict | list | None:
    """Route every request through FlareSolverr headless Chrome.
    Slower (~5s per request) but reliable against Cloudflare."""
    full_url = url
    if params:
        from urllib.parse import urlencode
        full_url = f"{url}?{urlencode(params)}"

    payload = json.dumps({
        "cmd": "request.get",
        "url": full_url,
        "maxTimeout": 30000,
    })

    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            resp = await session.post(
                _FLARESOLVERR_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=35,
            )
            wrapper = json.loads(resp.content)

        if wrapper.get("status") != "ok":
            logger.warning("FlareSolverr error: %s", wrapper.get("message", "unknown"))
            return None

        solution = wrapper.get("solution", {})
        body = solution.get("response", "")
        if not body:
            logger.warning("FlareSolverr empty body for %s", full_url)
            return None

        # FlareSolverr wraps JSON in HTML <pre> tags — extract it
        if body.strip().startswith("<"):
            import re
            # Extract content between <pre> tags
            m = re.search(r"<pre[^>]*>(.*?)</pre>", body, re.DOTALL)
            if m:
                body = m.group(1).strip()
            else:
                # Try stripping all HTML tags
                body = re.sub(r"<[^>]+>", "", body).strip()

        if not body or body.startswith("<"):
            logger.warning("FlareSolverr returned HTML, not JSON for %s", full_url)
            return None

        data = json.loads(body)

        # Check if SofaScore returned an error inside JSON
        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            logger.warning("SofaScore API error via FlareSolverr: %s (url: %s)", err, full_url)
            return None

        return data

    except json.JSONDecodeError as e:
        logger.error("FlareSolverr JSON parse error: %s (url: %s)", e, full_url)
        return None
    except Exception as e:
        logger.error("FlareSolverr request error: %s (url: %s)", e, full_url)
        return None


async def _get_curl(url: str, params: dict | None = None) -> dict | list | None:
    """Make request using curl_cffi (Chrome impersonation)."""
    try:
        session = await _get_curl_session()
        resp = await session.get(url, params=params, headers=_HEADERS)

        if resp.status_code == 403:
            logger.warning("SofaScore 403 (Cloudflare) on %s — retrying with new session...", url)
            global _curl_session
            try:
                await _curl_session.close()
            except:
                pass
            _curl_session = None
            await asyncio.sleep(2)

            # Retry 1: fresh session
            session = await _get_curl_session()
            resp = await session.get(url, params=params, headers=_HEADERS)

            if resp.status_code == 403:
                # Retry 2: try api.sofascore.com subdomain
                alt_url = url.replace("www.sofascore.com/api/v1", "api.sofascore.com/api/v1")
                if alt_url != url:
                    logger.info("Trying alt endpoint: %s", alt_url)
                    alt_headers = {**_HEADERS, "Referer": "https://www.sofascore.com/", "Origin": "https://www.sofascore.com"}
                    resp = await session.get(alt_url, params=params, headers=alt_headers)

            if resp.status_code == 403:
                logger.error("SofaScore 403 persistent on %s", url)
                return None

        if resp.status_code == 404:
            logger.debug("SofaScore 404: %s", url)
            return None

        if resp.status_code != 200:
            logger.error("SofaScore %s → %s", url, resp.status_code)
            return None

        return resp.json()

    except Exception as e:
        logger.error("SofaScore curl request error: %s — %s", url, e)
        return None


async def _get_aiohttp(url: str, params: dict | None = None) -> dict | list | None:
    """Fallback: aiohttp without Cloudflare bypass."""
    try:
        import aiohttp
    except ImportError:
        logger.error("aiohttp not installed and curl_cffi unavailable — cannot make requests")
        return None
    import ssl
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    headers = {**_HEADERS, "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )}

    try:
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, params=params,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 403:
                    logger.warning("SofaScore 403 (Cloudflare?) on %s", url)
                    return None
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.error("SofaScore %s → %s", url, resp.status)
                    return None
                return await resp.json()
    except asyncio.TimeoutError:
        logger.warning("SofaScore timeout: %s", url)
        return None
    except Exception as e:
        logger.error("SofaScore aiohttp error: %s — %s", url, e)
        return None


# ═══════════════════════════════════════════════════════
#  IN-MEMORY CACHE
# ═══════════════════════════════════════════════════════

class _Cache:
    """Simple TTL cache. Key → (data, timestamp)."""
    def __init__(self):
        self._store: dict[str, tuple[any, float]] = {}

    def get(self, key: str, ttl: float) -> any:
        if key in self._store:
            data, ts = self._store[key]
            if time.time() - ts < ttl:
                return data
            del self._store[key]
        return None

    def set(self, key: str, data: any):
        self._store[key] = (data, time.time())

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def cleanup(self, max_age: float = 3600):
        """Remove entries older than max_age seconds."""
        now = time.time()
        expired = [k for k, (_, ts) in self._store.items() if now - ts > max_age]
        for k in expired:
            del self._store[k]


cache = _Cache()


def cleanup_cache():
    """Call periodically to free memory."""
    cache.cleanup(max_age=3600)


# ═══════════════════════════════════════════════════════
#  FOOTBALL — SOFASCORE
# ═══════════════════════════════════════════════════════

async def football_live() -> list[dict]:
    """All currently live football events. Uses cache."""
    from config import LIVE_CACHE_TTL
    cached = cache.get("football_live", LIVE_CACHE_TTL)
    if cached is not None:
        return cached

    data = await _get("sport/football/events/live")
    if data is None:
        return []
    events = data.get("events", [])
    cache.set("football_live", events)
    return events


async def football_by_date(date_str: str) -> list[dict]:
    """All football events for a date (YYYY-MM-DD). Uses cache."""
    from config import SCHEDULE_CACHE_TTL
    cache_key = f"football_date_{date_str}"
    cached = cache.get(cache_key, SCHEDULE_CACHE_TTL)
    if cached is not None:
        return cached

    data = await _get(f"sport/football/scheduled-events/{date_str}")
    if data is None:
        return []
    events = data.get("events", [])
    cache.set(cache_key, events)
    return events


async def football_event(event_id: int) -> dict | None:
    """Single event details."""
    cache_key = f"football_event_{event_id}"
    cached = cache.get(cache_key, 60)
    if cached is not None:
        return cached

    data = await _get(f"event/{event_id}")
    if data is None:
        return None
    event = data.get("event", data)
    cache.set(cache_key, event)
    return event


async def football_statistics(event_id: int) -> dict:
    """Match statistics. Returns {'home': {...}, 'away': {...}}."""
    cache_key = f"football_stats_{event_id}"
    cached = cache.get(cache_key, 45)
    if cached is not None:
        return cached

    data = await _get(f"event/{event_id}/statistics")
    if data is None:
        return {"home": {}, "away": {}}

    result = {"home": {}, "away": {}}
    statistics = data.get("statistics", [])
    if statistics:
        # SofaScore returns periods, we want "ALL" or the first one
        period_stats = None
        for period in statistics:
            if period.get("period") == "ALL":
                period_stats = period
                break
        if period_stats is None and statistics:
            period_stats = statistics[0]

        if period_stats:
            groups = period_stats.get("groups", [])
            for group in groups:
                for item in group.get("statisticsItems", []):
                    key = item.get("key", "")
                    home_val = item.get("homeValue") if item.get("homeValue") is not None else item.get("home")
                    away_val = item.get("awayValue") if item.get("awayValue") is not None else item.get("away")
                    result["home"][key] = home_val
                    result["away"][key] = away_val

    cache.set(cache_key, result)
    return result


# ─── Football formatters ───────────────────────────────

def get_event_status(event: dict) -> dict:
    """Extract status info from a SofaScore event."""
    status = event.get("status", {})
    return {
        "code": status.get("code", 0),
        "type": status.get("type", ""),
        "description": status.get("description", ""),
    }


def is_live(event: dict) -> bool:
    st = get_event_status(event)
    return st["type"] == "inprogress"


def is_finished(event: dict) -> bool:
    st = get_event_status(event)
    return st["type"] == "finished"


def is_not_started(event: dict) -> bool:
    st = get_event_status(event)
    return st["type"] == "notstarted"


def is_canceled_or_postponed(event: dict) -> bool:
    st = get_event_status(event)
    return st["type"] in ("canceled", "postponed")


def get_home_name(event: dict) -> str:
    return event.get("homeTeam", {}).get("name", "???")


def get_away_name(event: dict) -> str:
    return event.get("awayTeam", {}).get("name", "???")


def get_home_score(event: dict) -> int | None:
    return event.get("homeScore", {}).get("current")


def get_away_score(event: dict) -> int | None:
    return event.get("awayScore", {}).get("current")


def get_kickoff_timestamp(event: dict) -> float:
    """Unix timestamp of kickoff."""
    ts = event.get("startTimestamp", 0)
    return float(ts) if ts else 0


def get_match_label(event: dict) -> str:
    return f"{get_home_name(event)} vs {get_away_name(event)}"


def get_minute(event: dict) -> str:
    """Current match minute for live games."""
    sc = event.get("statusDescription") or event.get("status", {}).get("description", "")
    if sc:
        return str(sc)
    return ""


def format_football_short(event: dict) -> str:
    home = get_home_name(event)
    away = get_away_name(event)
    hs = get_home_score(event)
    as_ = get_away_score(event)

    if is_not_started(event):
        ts = get_kickoff_timestamp(event)
        if ts:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
        else:
            t = "TBD"
        return f"{home} vs {away} (⏰ {t})"
    elif is_finished(event):
        return f"{home} {hs}:{as_} {away} (завершён)"
    elif is_canceled_or_postponed(event):
        return f"{home} vs {away} (отменён/перенесён)"
    else:
        minute = get_minute(event)
        return f"{home} {hs if hs is not None else '?'}:{as_ if as_ is not None else '?'} {away} ({minute})"


def get_event_id(event: dict) -> int:
    return event.get("id", 0)


def get_tournament_name(event: dict) -> str:
    t = event.get("tournament", {})
    cat = t.get("category", {}).get("name", "")
    name = t.get("name", "")
    return f"{cat} — {name}" if cat else name


# ═══════════════════════════════════════════════════════
#  BASKETBALL — SOFASCORE
# ═══════════════════════════════════════════════════════

async def basketball_live() -> list[dict]:
    """All currently live basketball events."""
    from config import LIVE_CACHE_TTL
    cached = cache.get("basketball_live", LIVE_CACHE_TTL)
    if cached is not None:
        return cached

    data = await _get("sport/basketball/events/live")
    if data is None:
        return []
    events = data.get("events", [])
    cache.set("basketball_live", events)
    return events


async def basketball_by_date(date_str: str) -> list[dict]:
    """All basketball events for a date."""
    from config import SCHEDULE_CACHE_TTL
    cache_key = f"basketball_date_{date_str}"
    cached = cache.get(cache_key, SCHEDULE_CACHE_TTL)
    if cached is not None:
        return cached

    data = await _get(f"sport/basketball/scheduled-events/{date_str}")
    if data is None:
        return []
    events = data.get("events", [])
    cache.set(cache_key, events)
    return events


async def basketball_event(event_id: int) -> dict | None:
    """Single basketball event."""
    cache_key = f"basketball_event_{event_id}"
    cached = cache.get(cache_key, 60)
    if cached is not None:
        return cached

    data = await _get(f"event/{event_id}")
    if data is None:
        return None
    event = data.get("event", data)
    cache.set(cache_key, event)
    return event


def parse_basketball_scores(event: dict) -> dict:
    """Parse SofaScore basketball event into a structured dict.

    SofaScore stores period scores in homeScore/awayScore like:
    homeScore: { current: 105, period1: 28, period2: 30, period3: 25, period4: 22 }
    """
    home_sc = event.get("homeScore", {})
    away_sc = event.get("awayScore", {})

    home_total = home_sc.get("current")
    away_total = away_sc.get("current")

    # Extract quarter scores
    home_q = []
    away_q = []
    for i in range(1, 5):
        hq = home_sc.get(f"period{i}")
        aq = away_sc.get(f"period{i}")
        home_q.append(_si(hq))
        away_q.append(_si(aq))

    home_total_int = _si(home_total) or sum(q for q in home_q if q is not None)
    away_total_int = _si(away_total) or sum(q for q in away_q if q is not None)

    q_totals = []
    for i in range(4):
        if home_q[i] is not None and away_q[i] is not None:
            q_totals.append(home_q[i] + away_q[i])
        else:
            q_totals.append(None)

    q_even = [qt % 2 == 0 if qt is not None else None for qt in q_totals]
    q1q2_even = (q_even[0] and q_even[1]) if (q_even[0] is not None and q_even[1] is not None) else None
    half1 = (q_totals[0] + q_totals[1]) if (q_totals[0] is not None and q_totals[1] is not None) else None

    status = get_event_status(event)

    return {
        "home_name": get_home_name(event),
        "away_name": get_away_name(event),
        "home_total": home_total_int,
        "away_total": away_total_int,
        "home_q": home_q,
        "away_q": away_q,
        "status_type": status["type"],
        "status_desc": status["description"],
        "points": (home_total_int or 0) + (away_total_int or 0),
        "q1_total": q_totals[0], "q2_total": q_totals[1],
        "q3_total": q_totals[2], "q4_total": q_totals[3],
        "q1_even": q_even[0], "q2_even": q_even[1],
        "q3_even": q_even[2], "q4_even": q_even[3],
        "q1q2_even": q1q2_even, "half1_total": half1,
    }


def format_basketball_short(event: dict) -> str:
    s = parse_basketball_scores(event)
    status_desc = s["status_desc"] or s["status_type"] or ""
    home_qs = "/".join(str(q) if q is not None else "-" for q in s["home_q"])
    away_qs = "/".join(str(q) if q is not None else "-" for q in s["away_q"])
    return (
        f"{s['home_name']} {s['home_total'] or 0}:{s['away_total'] or 0} {s['away_name']}\n"
        f"      Q: [{home_qs}] vs [{away_qs}] | {status_desc}"
    )


def format_basketball_detail(event: dict) -> str:
    s = parse_basketball_scores(event)
    tournament = get_tournament_name(event)

    lines = [f"🏀 <b>{s['home_name']} {s['home_total'] or 0}:{s['away_total'] or 0} {s['away_name']}</b>"]
    if tournament:
        lines.append(f"🏆 {tournament}")
    lines.append(f"📍 {s['status_desc'] or s['status_type'] or '?'}")
    lines.append("\n📊 <b>По четвертям:</b>")
    for i in range(4):
        qt = s.get(f"q{i+1}_total")
        if qt is not None:
            parity = "✅ ЧЁТ" if qt % 2 == 0 else "❌ НЕЧЕТ"
            lines.append(f"  Q{i+1}: {s['home_q'][i]}+{s['away_q'][i]}={qt} — {parity}")
    if s["q1q2_even"] is not None:
        both = "✅ ОБЕ ЧЁТНЫЕ" if s["q1q2_even"] else "❌ нет"
        lines.append(f"\n  🎯 Q1+Q2: {both}")
    if s["half1_total"] is not None:
        lines.append(f"  📊 1-й тайм: {s['half1_total']} очков")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════

def _si(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def to_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("%", "").strip())
        except ValueError:
            return None
    return None
