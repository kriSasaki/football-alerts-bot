"""
Fonbet API client — парсинг live и scheduled матчей напрямую с fon.bet.

Использует внутренний JSON API, который браузер запрашивает при загрузке страницы.
Endpoint: https://line-lb54-w.bk6bba-resources.com/ma/events/listBase

Данные, доступные по каждому матчу (live):
  - Счёт по четвертям (Q1..Q4) с разбивкой home/away
  - Текущий таймер четверти
  - Тотал тайма

Не требует Cloudflare-bypass — работает через обычный HTTPS.
"""

import asyncio
import logging
import time
from datetime import datetime

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _USE_CURL = True
except ImportError:
    _USE_CURL = False

logger = logging.getLogger(__name__)

# ─── Конфигурация ──────────────────────────────────────
# Несколько зеркал на случай блокировки одного из них
_FONBET_MIRRORS = [
    "https://line-lb52-w.bk6bba-resources.ru/ma",   # .ru - работает без SSL ошибок
    "https://line-lb61-w.bk6bba-resources.com/ma",
    "https://line-lb54-w.bk6bba-resources.com/ma",
    "https://line-lb51-w.bk6bba-resources.com/ma",
]
FONBET_BASE = _FONBET_MIRRORS[0]  # текущее активное зеркало
FONBET_SCOPE = "1600"
FONBET_LANG = "ru"

_mirror_index = 0  # индекс текущего зеркала

# ID спорта в Fonbet: 1=Футбол, 3=Баскетбол
FONBET_SPORT_IDS = {
    "football": 1,
    "basketball": 3,
}

FONBET_CACHE_TTL = 12      # секунды — live данные
FONBET_SCHED_TTL = 120     # секунды — расписание

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://fon.bet",
    "Referer": "https://fon.bet/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ─── Сессия ────────────────────────────────────────────
_session = None
_session_lock = asyncio.Lock()


async def _get_session():
    global _session
    if _session is not None:
        return _session
    async with _session_lock:
        if _session is not None:
            return _session
        if _USE_CURL:
            _session = CurlSession(impersonate="chrome124", verify=False, timeout=15)
        logger.info("Fonbet API: сессия создана (curl_cffi=%s)", _USE_CURL)
        return _session


async def close_session():
    global _session
    if _session and _USE_CURL:
        await _session.close()
    _session = None


# ─── Кеш ───────────────────────────────────────────────
class _Cache:
    def __init__(self):
        self._store: dict = {}

    def get(self, key: str, ttl: float):
        if key in self._store:
            data, ts = self._store[key]
            if time.time() - ts < ttl:
                return data
        return None

    def get_stale(self, key: str, max_age: float = 120):
        """Вернуть устаревшие данные если все зеркала недоступны."""
        if key in self._store:
            data, ts = self._store[key]
            if time.time() - ts < max_age:
                return data
        return None

    def set(self, key: str, data):
        self._store[key] = (data, time.time())

    def cleanup(self, max_age: float = 3600):
        now = time.time()
        dead = [k for k, (_, ts) in self._store.items() if now - ts > max_age]
        for k in dead:
            del self._store[k]


_cache = _Cache()


def cleanup_cache():
    _cache.cleanup()


# ─── HTTP ───────────────────────────────────────────────
async def _get(endpoint: str, params: dict | None = None) -> dict | None:
    """GET с автоматической ротацией зеркал при ошибке."""
    global _mirror_index, FONBET_BASE

    default_params = {"lang": FONBET_LANG, "scopeMarket": FONBET_SCOPE}
    if params:
        default_params.update(params)

    # Пробуем все зеркала по очереди, начиная с текущего
    for attempt in range(len(_FONBET_MIRRORS)):
        mirror = _FONBET_MIRRORS[(_mirror_index + attempt) % len(_FONBET_MIRRORS)]
        url = f"{mirror}/{endpoint}"
        try:
            result = await _do_get(url, default_params)
            if result is not None:
                # Успех — запоминаем рабочее зеркало
                if attempt > 0:
                    _mirror_index = (_mirror_index + attempt) % len(_FONBET_MIRRORS)
                    FONBET_BASE = mirror
                    logger.info("Fonbet: switched to mirror %s", mirror)
                return result
        except Exception as e:
            logger.debug("Fonbet mirror %s failed: %s", mirror, e)
            continue

    logger.warning("Fonbet: all mirrors unavailable for %s", endpoint)
    return None


async def _do_get(url: str, params: dict) -> dict | None:
    """Один HTTP-запрос без retry."""
    try:
        if _USE_CURL:
            sess = await _get_session()
            resp = await sess.get(url, params=params, headers=_HEADERS)
            if resp.status_code != 200:
                logger.debug("Fonbet %s → %s", url, resp.status_code)
                return None
            return resp.json()
        else:
            import aiohttp
            import ssl
            try:
                import certifi
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ssl_ctx = ssl.create_default_context()
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url, headers=_HEADERS, params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=ssl_ctx,
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Fonbet %s → %s", url, resp.status)
                        return None
                    return await resp.json()
    except Exception as e:
        # Поднимаем исключение чтобы _get мог переключиться на следующее зеркало
        raise


# ─── Вспомогательные функции ────────────────────────────
def _build_sport_ids(sports: list[dict], root_id: int) -> set[int]:
    """Рекурсивно собирает все дочерние sportId для данного корня."""
    by_id = {s["id"]: s for s in sports}

    def is_child(sport_id: int) -> bool:
        s = by_id.get(sport_id)
        while s:
            if s["id"] == root_id:
                return True
            pid = s.get("parentId")
            if not pid:
                return False
            s = by_id.get(pid)
        return False

    result = {root_id}
    for s in sports:
        if is_child(s["id"]):
            result.add(s["id"])
    return result


def _si(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _is_esports(event: dict) -> bool:
    """Виртуальные/киберспорт — ник игрока в скобках в названии команды."""
    t1 = event.get("team1", "")
    t2 = event.get("team2", "")
    return "(" in t1 or "(" in t2


def _parse_quarter_scores(live_info: dict | None) -> dict:
    """Конвертирует liveInfo Fonbet в структуру совместимую с parse_basketball_scores."""
    if not live_info:
        return {}

    scores_arr = live_info.get("scores", [])
    total_score = scores_arr[0][0] if scores_arr and scores_arr[0] else {}
    quarter_arr = scores_arr[1] if len(scores_arr) > 1 else []

    home_total = _si(total_score.get("c1"))
    away_total = _si(total_score.get("c2"))
    home_q: list = []
    away_q: list = []
    for q in quarter_arr[:4]:
        home_q.append(_si(q.get("c1")))
        away_q.append(_si(q.get("c2")))
    while len(home_q) < 4:
        home_q.append(None)
        away_q.append(None)

    q_totals = []
    for i in range(4):
        if home_q[i] is not None and away_q[i] is not None:
            q_totals.append(home_q[i] + away_q[i])
        else:
            q_totals.append(None)

    q_even = [qt % 2 == 0 if qt is not None else None for qt in q_totals]
    q1q2_even = (
        (q_even[0] and q_even[1])
        if (q_even[0] is not None and q_even[1] is not None)
        else None
    )
    half1 = (
        (q_totals[0] + q_totals[1])
        if (q_totals[0] is not None and q_totals[1] is not None)
        else None
    )

    # Текущая четверть из subscores
    subscores = live_info.get("subscores", [])
    period_map = {"100401": 1, "100402": 2, "100403": 3, "100404": 4}
    current_period = 0
    if subscores:
        last_sub = subscores[-1]
        current_period = period_map.get(str(last_sub.get("kindId", "")), 0)

    # Определяем сколько четвертей реально СЫГРАНО по scoreComment
    # scoreComment выглядит так: "(19-29 31-31 22-34)" — 3 завершённых четверти
    # Если Q4 = 0-0 и она не в subscores — это ещё не сыгранная четверть
    score_comment = live_info.get("scoreComment", "")
    played_quarters = len(score_comment.split()) if score_comment else 0
    # scoreComment содержит только завершённые или текущую четверть
    # Точнее: считаем элементы в quarter_arr у которых хотя бы один не-нулевой
    # ИЛИ которые соответствуют current_period
    def _is_real_quarter(q_idx: int) -> bool:
        """Проверяет, что данные по четверти реальные, а не заглушка 0-0."""
        if home_q[q_idx] is None and away_q[q_idx] is None:
            return False  # нет данных вообще
        h = home_q[q_idx] or 0
        a = away_q[q_idx] or 0
        # Четверть реальна если: есть ненулевые очки, ИЛИ это текущая активная четверть
        if h > 0 or a > 0:
            return True
        if current_period == q_idx + 1:
            return True  # текущая четверть, пока 0-0 — реальна
        return False  # 0-0 и не активная — это заглушка Fonbet

    timer_str = live_info.get("timer", "")
    timer_seconds_raw = live_info.get("timerSeconds", 0) or 0
    # timerSeconds у Fonbet = секунды С НАЧАЛА МАТЧА (нарастающий, для обоих direction)
    # basketball_period_alert_is_fresh ждёт секунды С НАЧАЛА ТЕКУЩЕЙ четверти
    # elapsed_in_quarter = total_seconds - (current_period - 1) * quarter_duration
    _QUARTER_SECONDS = 600  # 10 минут по умолчанию
    if current_period >= 1:
        timer_seconds = max(0, timer_seconds_raw - (current_period - 1) * _QUARTER_SECONDS)
    else:
        timer_seconds = timer_seconds_raw

    def _quarter_finished(q_idx: int) -> bool:
        if not _is_real_quarter(q_idx):
            return False  # данных нет или это заглушка
        if current_period == q_idx + 1:
            return False  # эта четверть сейчас идёт
        if current_period > q_idx + 1:
            return True   # следующая уже началась — эта завершена
        # current_period == 0: перерыв или матч окончен
        # Четверть завершена если она не заглушка (проверено выше)
        return True

    q1_finished = _quarter_finished(0)
    q2_finished = _quarter_finished(1)
    q3_finished = _quarter_finished(2)
    q4_finished = _quarter_finished(3)

    # Сбрасываем заглушки в None чтобы они не вшли в тоталы
    for i in range(4):
        if not _is_real_quarter(i):
            home_q[i] = None
            away_q[i] = None

    # Пересчитываем q_totals уже с очищенными данными
    for i in range(4):
        if home_q[i] is not None and away_q[i] is not None:
            q_totals[i] = home_q[i] + away_q[i]
        else:
            q_totals[i] = None

    # Обновляем q_even и q1q2_even с очищенными данными
    q_even = [qt % 2 == 0 if qt is not None else None for qt in q_totals]
    q1q2_even = (
        (q_even[0] and q_even[1])
        if (q_even[0] is not None and q_even[1] is not None)
        else None
    )
    half1 = (
        (q_totals[0] + q_totals[1])
        if (q_totals[0] is not None and q_totals[1] is not None)
        else None
    )

    home_total_int = home_total if home_total is not None else sum(q for q in home_q if q is not None)
    away_total_int = away_total if away_total is not None else sum(q for q in away_q if q is not None)

    return {
        "home_q": home_q,
        "away_q": away_q,
        "home_total": home_total_int,
        "away_total": away_total_int,
        "q1_total": q_totals[0], "q2_total": q_totals[1],
        "q3_total": q_totals[2], "q4_total": q_totals[3],
        "q1_finished": q1_finished, "q2_finished": q2_finished,
        "q3_finished": q3_finished, "q4_finished": q4_finished,
        "half1_finished": q2_finished,
        "q1_even": q_even[0], "q2_even": q_even[1],
        "q3_even": q_even[2], "q4_even": q_even[3],
        "q1q2_even": q1q2_even,
        "half1_total": half1,
        "points": (home_total_int or 0) + (away_total_int or 0),
        "current_period": current_period,
        "clock_seconds": timer_seconds,
        "status_type": "inprogress",
        "status_desc": timer_str,
    }


# ─── Главная функция получения данных ──────────────────
async def _fetch_all() -> dict | None:
    cached = _cache.get("fonbet_all", FONBET_CACHE_TTL)
    if cached is not None:
        return cached
    data = await _get("events/listBase")
    if data:
        _cache.set("fonbet_all", data)
        return data
    # Все зеркала недоступны — возвращаем устаревшие данные если есть (не старше 2 мин)
    return _cache.get_stale("fonbet_all", max_age=120)


def _enrich_events(data: dict, sport_key: str) -> list[dict]:
    """Возвращает обогащённые события нужного спорта."""
    events = data.get("events", [])
    sports = data.get("sports", [])
    liveInfos = data.get("liveEventInfos", [])

    root_id = FONBET_SPORT_IDS.get(sport_key, 0)
    sport_ids = _build_sport_ids(sports, root_id)
    sport_by_id = {s["id"]: s for s in sports}
    live_info_by_id = {li["eventId"]: li for li in liveInfos}

    result = []
    for e in events:
        if e.get("sportId") not in sport_ids:
            continue
        if e.get("level", 1) != 1:
            continue
        ev = dict(e)
        ev["_live_info"] = live_info_by_id.get(e["id"])
        ev["_league"] = sport_by_id.get(e.get("sportId"), {}).get("name", "")
        ev["_is_esports"] = _is_esports(e)
        result.append(ev)
    return result


# ─── Публичный API ──────────────────────────────────────

async def basketball_live(include_esports: bool = False) -> list[dict]:
    data = await _fetch_all()
    if not data:
        return []
    events = _enrich_events(data, "basketball")
    result = [e for e in events if e.get("place") == "live"]
    if not include_esports:
        result = [e for e in result if not e["_is_esports"]]
    return result


async def football_live(include_esports: bool = False) -> list[dict]:
    data = await _fetch_all()
    if not data:
        return []
    events = _enrich_events(data, "football")
    result = [e for e in events if e.get("place") == "live"]
    if not include_esports:
        result = [e for e in result if not e["_is_esports"]]
    return result


async def basketball_scheduled(include_esports: bool = False) -> list[dict]:
    data = await _fetch_all()
    if not data:
        return []
    events = _enrich_events(data, "basketball")
    result = [e for e in events if e.get("place") == "line"]
    if not include_esports:
        result = [e for e in result if not e["_is_esports"]]
    return sorted(result, key=lambda e: e.get("startTime", 0))


async def football_scheduled(include_esports: bool = False) -> list[dict]:
    data = await _fetch_all()
    if not data:
        return []
    events = _enrich_events(data, "football")
    result = [e for e in events if e.get("place") == "line"]
    if not include_esports:
        result = [e for e in result if not e["_is_esports"]]
    return sorted(result, key=lambda e: e.get("startTime", 0))


async def get_event(event_id: int) -> dict | None:
    data = await _fetch_all()
    if not data:
        return None
    events = data.get("events", [])
    sports = data.get("sports", [])
    liveInfos = data.get("liveEventInfos", [])
    sport_by_id = {s["id"]: s for s in sports}
    live_info_by_id = {li["eventId"]: li for li in liveInfos}
    for e in events:
        if e.get("id") == event_id:
            ev = dict(e)
            ev["_live_info"] = live_info_by_id.get(e["id"])
            ev["_league"] = sport_by_id.get(e.get("sportId"), {}).get("name", "")
            ev["_is_esports"] = _is_esports(e)
            return ev
    return None


# ─── Адаптеры (совместимость с bot.py и alert_engine.py) ─

def get_event_id(event: dict) -> int:
    return event.get("id", 0)


def get_home_name(event: dict) -> str:
    return event.get("team1", "???")


def get_away_name(event: dict) -> str:
    return event.get("team2", "???")


def get_match_label(event: dict) -> str:
    return f"{get_home_name(event)} vs {get_away_name(event)}"


def get_kickoff_timestamp(event: dict) -> float:
    return float(event.get("startTime", 0))


def get_league_name(event: dict) -> str:
    return event.get("_league", "")


def is_live(event: dict) -> bool:
    return event.get("place") == "live"


def is_scheduled(event: dict) -> bool:
    return event.get("place") == "line"


def parse_basketball_scores(event: dict) -> dict:
    """Конвертирует Fonbet-событие в формат совместимый с alert_engine."""
    live_info = event.get("_live_info")
    parsed = _parse_quarter_scores(live_info)
    parsed["home_name"] = get_home_name(event)
    parsed["away_name"] = get_away_name(event)
    if not live_info:
        parsed.update({
            "status_type": "notstarted",
            "status_desc": "Не начался",
            "home_total": 0,
            "away_total": 0,
            "points": 0,
        })
    return parsed


def format_list_item(event: dict) -> str:
    home = get_home_name(event)
    away = get_away_name(event)

    if is_live(event):
        live_info = event.get("_live_info")
        if live_info and live_info.get("scores"):
            total = live_info["scores"][0][0] if live_info["scores"] else {}
            h = total.get("c1", "?")
            a = total.get("c2", "?")
            subs = live_info.get("subscores", [])
            period_map = {"100401": "Q1", "100402": "Q2", "100403": "Q3", "100404": "Q4"}
            period = period_map.get(str(subs[-1]["kindId"]) if subs else "", "live") if subs else "live"
            timer = live_info.get("timer", "")
            return f"{home} {h}:{a} {away} | {period} {timer}"
        return f"{home} - {away} | live"
    else:
        ts = get_kickoff_timestamp(event)
        t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "TBD"
        return f"{home} - {away} | старт {t}"


def format_detail(event: dict, sport: str = "basketball") -> str:
    home = get_home_name(event)
    away = get_away_name(event)
    league = get_league_name(event)
    emoji = "🏀" if sport == "basketball" else "⚽"

    lines = [f"{emoji} <b>{home} — {away}</b>"]
    if league:
        lines.append(f"🏆 {league}")
    lines.append(f"📡 Источник: <b>Fonbet</b>")

    if is_live(event):
        s = parse_basketball_scores(event)
        lines.append(f"\n📊 Счёт: <b>{s.get('home_total', 0)}:{s.get('away_total', 0)}</b>")
        lines.append("\n📈 По четвертям:")
        for i in range(4):
            qt = s.get(f"q{i+1}_total")
            if qt is not None:
                hq = s["home_q"][i] or 0
                aq = s["away_q"][i] or 0
                parity = "✅ ЧЁТ" if qt % 2 == 0 else "❌ НЕЧЕТ"
                fin = " ✓" if s.get(f"q{i+1}_finished") else " ▶"
                lines.append(f"  Q{i+1}: {hq}+{aq}={qt} — {parity}{fin}")
        if s.get("q1q2_even") is not None:
            both = "✅ ОБЕ ЧЁТНЫЕ" if s["q1q2_even"] else "❌ нет"
            lines.append(f"\n🎯 Q1+Q2: {both}")
        if s.get("half1_total") is not None:
            lines.append(f"📊 1-й тайм: {s['half1_total']} очков")
    else:
        ts = get_kickoff_timestamp(event)
        if ts:
            lines.append(f"\n⏰ Начало: {datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}")
        lines.append("\n💡 Можешь поставить алерт заранее!")

    return "\n".join(lines)


def format_notification(alert: dict, value: float, event: dict, extra: str = "") -> str:
    from config import BASKETBALL_STATS
    s = parse_basketball_scores(event)
    stat_info = BASKETBALL_STATS.get(alert["stat_key"], {})
    stat_label = stat_info.get("label", alert["stat_key"])
    league = get_league_name(event)

    lines = [
        "🔔 <b>АЛЕРТ СРАБОТАЛ!</b>",
        f"📡 Источник: <b>Fonbet</b>\n",
        f"🏀 {s['home_name']} {s.get('home_total',0)}:{s.get('away_total',0)} {s['away_name']}",
    ]
    if league:
        lines.append(f"🏆 {league}")
    lines.append("")

    for i in range(4):
        qt = s.get(f"q{i+1}_total")
        if qt is not None:
            parity = "ЧЁТ ✅" if qt % 2 == 0 else "НЕЧЕТ"
            lines.append(f"  Q{i+1}: {qt} ({parity})")

    lines.append(f"\n📊 <b>{stat_label}:</b> {value}")
    if extra:
        lines.append(f"ℹ️ {extra}")
    lines.append(f"\n🎯 Алерт #{alert['id']} ✅")
    return "\n".join(lines)
