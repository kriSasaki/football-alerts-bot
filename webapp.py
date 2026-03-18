import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

import database as db
import sports_api as api
from auth import get_access_state, get_admin_ids, is_authorized, revoke_web_sessions
from config import (
    APP_NAME,
    BASKETBALL_STATS,
    FOOTBALL_STATS,
    SPORTS,
    WEB_APP_HOST,
    WEB_APP_PORT,
    WEB_APP_PORT_MAX_TRIES,
    WEB_APP_PUBLIC_URL,
    WEB_APP_ENABLED,
    WEB_PUSH_VAPID_PUBLIC_KEY,
)
from web_auth import token_is_admin, verify_web_token

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web"
_runtime_base_url = ""


def _base_payload() -> dict:
    return {
        "app_name": APP_NAME,
        "sports": SPORTS,
        "stats_catalog": _stats_catalog(),
        "operators": [">", ">=", "==", "<=", "<"],
        "public_url": get_runtime_web_url(),
        "vapid_public_key": WEB_PUSH_VAPID_PUBLIC_KEY,
    }


def get_runtime_web_url() -> str:
    return _runtime_base_url or WEB_APP_PUBLIC_URL


def _stats_catalog() -> dict:
    catalog = {
        "football": {},
        "basketball": {},
    }
    for key, value in FOOTBALL_STATS.items():
        catalog["football"][key] = {
            "label": value["label"],
            "mode": "compare",
            "supports_team": True,
        }
    for key, value in BASKETBALL_STATS.items():
        catalog["basketball"][key] = {
            "label": value["label"],
            "mode": "boolean" if key in {"q1_even", "q2_even", "q1q2_even"} else "compare",
            "supports_team": False,
        }
    return catalog


def _require_web_user(request: web.Request) -> int:
    token = request.query.get("token", "").strip()
    if not token and request.can_read_body:
        raise web.HTTPUnauthorized(text="Missing access token")
    user_id = verify_web_token(token)
    if user_id is None or not is_authorized(user_id):
        raise web.HTTPUnauthorized(text="Access denied")
    return user_id


def _require_admin(request: web.Request) -> int:
    token = request.query.get("token", "").strip()
    if not token and request.can_read_body:
        raise web.HTTPUnauthorized(text="Missing access token")
    user_id = verify_web_token(token)
    if user_id is None or user_id not in get_admin_ids() or not token_is_admin(token):
        raise web.HTTPForbidden(text="Admin access required")
    return user_id


async def handle_index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_manifest(request: web.Request):
    return web.FileResponse(STATIC_DIR / "manifest.webmanifest")


async def handle_service_worker(request: web.Request):
    response = web.FileResponse(STATIC_DIR / "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


async def api_bootstrap(request: web.Request):
    token = request.query.get("token", "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")
    return web.json_response(
        {
            **_base_payload(),
            "web_user_token": token,
            "user_id": user_id,
            "is_admin": user_id in get_admin_ids(),
        }
    )


async def api_matches(request: web.Request):
    _require_web_user(request)
    sport = request.query.get("sport", "football").strip().lower()
    day_offset = int(request.query.get("day_offset", "0"))
    target_date = datetime.now() + timedelta(days=day_offset)
    date_str = target_date.strftime("%Y-%m-%d")

    if sport == "football":
        events = await api.football_by_date(date_str)
    elif sport == "basketball":
        events = await api.basketball_by_date(date_str)
    else:
        raise web.HTTPBadRequest(text="Unknown sport")

    payload = []
    for event in events:
        event_id = api.get_event_id(event)
        home_team = event.get("homeTeam", {}) or {}
        away_team = event.get("awayTeam", {}) or {}
        payload.append(
            {
                "fixture_id": event_id,
                "match_label": api.get_match_label(event),
                "kickoff_at": api.get_kickoff_timestamp(event),
                "status": api.get_event_status(event),
                "summary": api.format_football_short(event) if sport == "football" else _format_basketball_short(event),
                "home_team": {
                    "name": home_team.get("name", ""),
                    "id": home_team.get("id"),
                },
                "away_team": {
                    "name": away_team.get("name", ""),
                    "id": away_team.get("id"),
                },
            }
        )
    return web.json_response({"date": date_str, "events": payload})


def _format_basketball_short(event: dict) -> str:
    parsed = api.parse_basketball_scores(event)
    if api.is_not_started(event):
        ts = event.get("startTimestamp")
        start = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "TBD"
        return f"{parsed['home_name']} vs {parsed['away_name']} ({start})"
    return f"{parsed['home_name']} {parsed['home_total'] or 0}:{parsed['away_total'] or 0} {parsed['away_name']}"


async def api_get_alerts(request: web.Request):
    user_id = _require_web_user(request)
    alerts = await db.get_user_alerts(user_id)
    subscriptions = await db.get_web_push_subscriptions(user_id)
    return web.json_response({"alerts": alerts, "subscriptions": subscriptions})


async def api_create_alert(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")

    sport = (payload.get("sport") or "").strip().lower()
    stat_key = (payload.get("stat_key") or "").strip().lower()
    fixture_id = int(payload.get("fixture_id"))
    if sport not in SPORTS:
        raise web.HTTPBadRequest(text="Unknown sport")
    metadata = _stats_catalog()[sport].get(stat_key)
    if metadata is None:
        raise web.HTTPBadRequest(text="Unknown stat")
    operator = (payload.get("operator") or "").strip() if metadata["mode"] == "compare" else "=="
    threshold = float(payload.get("threshold")) if metadata["mode"] == "compare" else 1.0
    team = (payload.get("team") or "total").strip().lower() if metadata["supports_team"] else "total"
    match_label = (payload.get("match_label") or "").strip()
    kickoff_at = float(payload.get("kickoff_at") or 0)
    if metadata["mode"] == "compare" and operator not in {">", "<", ">=", "<=", "=="}:
        raise web.HTTPBadRequest(text="Unknown operator")
    alert_id = await db.add_alert(
        user_id=user_id,
        chat_id=user_id,
        sport=sport,
        fixture_id=fixture_id,
        stat_key=stat_key,
        operator=operator,
        threshold=threshold,
        team=team,
        kickoff_at=kickoff_at,
        match_label=match_label,
    )
    return web.json_response({"ok": True, "alert_id": alert_id})


async def api_bulk_alerts(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")

    sport = (payload.get("sport") or "").strip().lower()
    stat_key = (payload.get("stat_key") or "").strip().lower()
    if sport not in SPORTS:
        raise web.HTTPBadRequest(text="Unknown sport")
    metadata = _stats_catalog()[sport].get(stat_key)
    if metadata is None:
        raise web.HTTPBadRequest(text="Unknown stat")

    operator = (payload.get("operator") or "").strip() if metadata["mode"] == "compare" else "=="
    threshold = float(payload.get("threshold")) if metadata["mode"] == "compare" else 1.0
    team = (payload.get("team") or "total").strip().lower() if metadata["supports_team"] else "total"
    matches = payload.get("matches") or []
    if not matches:
        raise web.HTTPBadRequest(text="No matches selected")

    normalized = []
    for item in matches:
        normalized.append(
            {
                "fixture_id": int(item["fixture_id"]),
                "kickoff_at": float(item.get("kickoff_at") or 0),
                "match_label": (item.get("match_label") or "").strip(),
            }
        )
    ids = await db.add_alerts_bulk(
        user_id=user_id,
        chat_id=user_id,
        sport=sport,
        matches=normalized,
        stat_key=stat_key,
        operator=operator,
        threshold=threshold,
        team=team,
    )
    return web.json_response({"ok": True, "count": len(ids), "alert_ids": ids})


async def api_delete_alert(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    alert_id = int(payload.get("alert_id"))
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")
    deleted = await db.deactivate_alert(alert_id, user_id)
    return web.json_response({"ok": deleted})


async def api_subscribe(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    subscription = payload.get("subscription") or {}
    user_id = verify_web_token(token)
    if user_id is None or "endpoint" not in subscription:
        raise web.HTTPBadRequest(text="Invalid subscription")

    await db.save_web_push_subscription(
        user_id=user_id,
        endpoint=subscription["endpoint"],
        subscription_json=json.dumps(subscription),
        user_agent=request.headers.get("User-Agent", ""),
    )
    return web.json_response({"ok": True})


async def api_unsubscribe(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")
    endpoint = (payload.get("endpoint") or "").strip()
    if not endpoint:
        raise web.HTTPBadRequest(text="Missing endpoint")
    await db.delete_web_push_subscription(endpoint)
    return web.json_response({"ok": True})


async def api_test_push(request: web.Request):
    from webpush import send_user_push

    payload = await request.json()
    token = (payload.get("token") or "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")
    sent, removed = await send_user_push(user_id, "<b>Тестовое уведомление</b>\nПроверка web push выполнена.")
    return web.json_response({"ok": True, "sent": sent, "removed": removed})


async def api_logout(request: web.Request):
    _require_web_user(request)
    return web.json_response({"ok": True})


async def api_revoke_sessions(request: web.Request):
    payload = await request.json()
    token = (payload.get("token") or "").strip()
    user_id = verify_web_token(token)
    if user_id is None:
        raise web.HTTPUnauthorized(text="Access denied")
    revoke_web_sessions(user_id)
    return web.json_response({"ok": True})


async def api_admin_access_state(request: web.Request):
    _require_admin(request)
    state = get_access_state()
    for item in state["pending"]:
        item["has_access"] = is_authorized(item["user_id"])
    return web.json_response(state)


async def api_match_detail(request: web.Request):
    _require_web_user(request)
    sport = request.query.get("sport", "football").strip().lower()
    fixture_id = int(request.query.get("fixture_id", "0"))
    if not fixture_id:
        raise web.HTTPBadRequest(text="Missing fixture")

    if sport == "football":
        event = await api.football_event(fixture_id)
        stats = await api.football_statistics(fixture_id) if event and api.is_live(event) else {"home": {}, "away": {}}
        return web.json_response({"event": _football_detail(event, stats)})
    if sport == "basketball":
        event = await api.basketball_event(fixture_id)
        return web.json_response({"event": _basketball_detail(event)})
    raise web.HTTPBadRequest(text="Unknown sport")


def _football_detail(event: dict | None, stats: dict) -> dict:
    if not event:
        return {}
    reverse_stats = {value["api_name"]: value["label"] for value in FOOTBALL_STATS.values()}
    home_stats = []
    away_stats = []
    for key, value in list((stats or {}).get("home", {}).items())[:8]:
        home_stats.append({"label": reverse_stats.get(key, key), "value": value})
    for key, value in list((stats or {}).get("away", {}).items())[:8]:
        away_stats.append({"label": reverse_stats.get(key, key), "value": value})
    return {
        "title": api.get_match_label(event),
        "tournament": api.get_tournament_name(event),
        "status": api.get_event_status(event),
        "kickoff_at": api.get_kickoff_timestamp(event),
        "score": {
            "home": api.get_home_score(event),
            "away": api.get_away_score(event),
            "minute": api.get_minute(event),
        },
        "stats": {"home": home_stats, "away": away_stats},
    }


def _basketball_detail(event: dict | None) -> dict:
    if not event:
        return {}
    parsed = api.parse_basketball_scores(event)
    return {
        "title": api.get_match_label(event),
        "tournament": api.get_tournament_name(event),
        "status": api.get_event_status(event),
        "kickoff_at": api.get_kickoff_timestamp(event),
        "score": {
            "home": parsed.get("home_total"),
            "away": parsed.get("away_total"),
            "status_text": parsed.get("status_desc") or parsed.get("status_type"),
            "quarters": [parsed.get(f"q{i}_total") for i in range(1, 5)],
        },
    }


async def api_health(request: web.Request):
    return web.json_response({"status": "ok", "web_app_enabled": WEB_APP_ENABLED})


async def start_web_app() -> tuple[web.AppRunner | None, web.BaseSite | None]:
    global _runtime_base_url
    if not WEB_APP_ENABLED:
        logger.info("Web app disabled by config")
        return None, None

    if not STATIC_DIR.exists():
        raise RuntimeError(f"Missing web assets at {STATIC_DIR}")

    app = web.Application()
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/manifest.webmanifest", handle_manifest),
            web.get("/sw.js", handle_service_worker),
            web.static("/assets", str(STATIC_DIR)),
            web.get("/api/bootstrap", api_bootstrap),
            web.get("/api/matches", api_matches),
            web.get("/api/match-detail", api_match_detail),
            web.get("/api/alerts", api_get_alerts),
            web.post("/api/alerts", api_create_alert),
            web.post("/api/alerts/bulk", api_bulk_alerts),
            web.delete("/api/alerts", api_delete_alert),
            web.post("/api/subscribe", api_subscribe),
            web.post("/api/unsubscribe", api_unsubscribe),
            web.post("/api/push/test", api_test_push),
            web.post("/api/auth/logout", api_logout),
            web.post("/api/auth/revoke-sessions", api_revoke_sessions),
            web.get("/api/admin/access-state", api_admin_access_state),
            web.get("/healthz", api_health),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    last_error = None

    for port in range(WEB_APP_PORT, WEB_APP_PORT + max(1, WEB_APP_PORT_MAX_TRIES)):
        site = web.TCPSite(runner, WEB_APP_HOST, port)
        try:
            await site.start()
            public_host = "localhost" if WEB_APP_HOST == "0.0.0.0" else WEB_APP_HOST
            _runtime_base_url = WEB_APP_PUBLIC_URL or f"http://{public_host}:{port}"
            logger.info("Web app listening on http://%s:%d", WEB_APP_HOST, port)
            if port != WEB_APP_PORT:
                logger.warning("Preferred web port %d was busy, switched to %d", WEB_APP_PORT, port)
            return runner, site
        except OSError as exc:
            last_error = exc
            logger.warning("Web app port %d unavailable: %s", port, exc)

    await runner.cleanup()
    raise last_error if last_error is not None else RuntimeError("Unable to start web app")


async def stop_web_app(runner: web.AppRunner | None):
    global _runtime_base_url
    if runner:
        await runner.cleanup()
    _runtime_base_url = ""
