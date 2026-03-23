"""
Alert engine for football and basketball notifications.
"""
import html
import logging
import operator as op

from config import BASKETBALL_STATS, FOOTBALL_STATS, SPORTS
from sports_api import basketball_period_alert_is_fresh

logger = logging.getLogger(__name__)


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


OPERATORS = {
    ">": op.gt,
    "<": op.lt,
    ">=": op.ge,
    "<=": op.le,
    "==": op.eq,
}


def check_football_alert(alert: dict, stats: dict, event: dict) -> tuple[bool, float | None]:
    stat_key = alert["stat_key"]
    threshold = alert["threshold"]
    oper_str = alert["operator"]
    team = alert.get("team", "total")

    compare_fn = OPERATORS.get(oper_str)
    if not compare_fn:
        return False, None

    if stat_key == "goals":
        value = _get_football_goals(event, team)
    else:
        info = FOOTBALL_STATS.get(stat_key)
        if not info:
            return False, None
        value = _extract_football_stat(stats, info["api_name"], team)

    if value is None:
        return False, None

    return compare_fn(value, threshold), value


def _get_football_goals(event: dict, team: str) -> float | None:
    hs = event.get("homeScore", {}).get("current")
    as_ = event.get("awayScore", {}).get("current")
    if team == "home":
        return float(hs) if hs is not None else None
    if team == "away":
        return float(as_) if as_ is not None else None
    if hs is None and as_ is None:
        return None
    return float((hs or 0) + (as_ or 0))


def _extract_football_stat(stats: dict, api_name: str, team: str) -> float | None:
    if team == "total":
        home = _to_num(stats.get("home", {}).get(api_name))
        away = _to_num(stats.get("away", {}).get(api_name))
        if home is None and away is None:
            return None
        return (home or 0) + (away or 0)
    return _to_num(stats.get(team, {}).get(api_name))


def check_basketball_alert(alert: dict, parsed_scores: dict) -> tuple[bool, float | None, str]:
    stat_key = alert["stat_key"]
    threshold = alert["threshold"]
    oper_str = alert["operator"]

    if stat_key == "q1_even":
        return _check_even_period(parsed_scores, "q1")
    if stat_key == "q2_even":
        return _check_even_period(parsed_scores, "q2")
    if stat_key == "q1q2_even":
        return _check_even_pair(parsed_scores)

    compare_fn = OPERATORS.get(oper_str)
    if not compare_fn:
        return False, None, ""

    field_map = {
        "points": "points",
        "q1_total": "q1_total",
        "q2_total": "q2_total",
        "q3_total": "q3_total",
        "q4_total": "q4_total",
        "half1": "half1_total",
    }
    final_flags = {
        "q1_total": "q1_finished",
        "q2_total": "q2_finished",
        "q3_total": "q3_finished",
        "q4_total": "q4_finished",
        "half1_total": "half1_finished",
    }

    field = field_map.get(stat_key)
    if not field:
        return False, None, f"Unknown stat: {stat_key}"

    final_flag = final_flags.get(field)
    if final_flag and not parsed_scores.get(final_flag):
        return False, None, "Period is not finished yet"
    if field == "q1_total" and not basketball_period_alert_is_fresh(parsed_scores, 1):
        return False, None, "Q1 alert window has expired"
    if field in {"q2_total", "half1_total"} and not basketball_period_alert_is_fresh(parsed_scores, 2):
        return False, None, "Q2 alert window has expired"
    if field == "q3_total" and not basketball_period_alert_is_fresh(parsed_scores, 3):
        return False, None, "Q3 alert window has expired"
    if field == "q4_total" and not basketball_period_alert_is_fresh(parsed_scores, 4):
        return False, None, "Q4 alert window has expired"

    value = parsed_scores.get(field)
    if value is None:
        return False, None, "Data is not available yet"

    value = float(value)
    return compare_fn(value, threshold), value, ""


def _check_even_period(parsed_scores: dict, period_key: str) -> tuple[bool, float | None, str]:
    finished_key = f"{period_key}_finished"
    total_key = f"{period_key}_total"
    even_key = f"{period_key}_even"
    label = period_key.upper()

    if not parsed_scores.get(finished_key):
        return False, None, f"{label} is not finished yet"
    if not basketball_period_alert_is_fresh(parsed_scores, int(period_key[1:])):
        return False, None, f"{label} alert window has expired"

    total = parsed_scores.get(total_key)
    if total is None:
        return False, None, f"{label} data unavailable"

    if parsed_scores.get(even_key):
        return True, float(total), f"{label} total = {total} (EVEN)"
    return False, float(total), f"{label} total = {total} (ODD)"


def _check_even_pair(parsed_scores: dict) -> tuple[bool, float | None, str]:
    if not parsed_scores.get("q1_finished") or not parsed_scores.get("q2_finished"):
        return False, None, "Q1 and Q2 are not both finished yet"
    if not basketball_period_alert_is_fresh(parsed_scores, 2):
        return False, None, "Q1/Q2 alert window has expired"

    q1_total = parsed_scores.get("q1_total")
    q2_total = parsed_scores.get("q2_total")
    if q1_total is None or q2_total is None:
        return False, None, "Q1/Q2 data unavailable"

    q1_state = "EVEN" if parsed_scores.get("q1_even") else "ODD"
    q2_state = "EVEN" if parsed_scores.get("q2_even") else "ODD"
    info = f"Q1={q1_total}({q1_state}), Q2={q2_total}({q2_state})"

    if parsed_scores.get("q1q2_even"):
        return True, float(q1_total + q2_total), f"Q1+Q2 BOTH EVEN\n{info}"
    return False, float(q1_total + q2_total), f"Not both even\n{info}"


def format_live_alert_status(alert: dict, stats: dict = None, event: dict = None, parsed_bb: dict = None) -> str:
    sport = alert.get("sport", "football")
    stat_key = alert["stat_key"]

    if sport == "football":
        if event is None:
            return "No data"
        from sports_api import is_finished, is_not_started

        if is_not_started(event):
            return "Not started yet"
        if is_finished(event):
            return "Finished"

        if stat_key == "goals":
            value = _get_football_goals(event, alert.get("team", "total"))
        else:
            info = FOOTBALL_STATS.get(stat_key)
            value = _extract_football_stat(stats, info["api_name"], alert.get("team", "total")) if info and stats else None
        if value is not None:
            return f"{value} (need {alert['operator']} {alert['threshold']})"
        return "Stat unavailable"

    if sport == "basketball":
        if parsed_bb is None:
            return "No data"

        if stat_key == "q1_even":
            if not parsed_bb.get("q1_finished"):
                return "Q1 is still in progress"
            if not basketball_period_alert_is_fresh(parsed_bb, 1):
                return "Q1 alert window has expired"
            total = parsed_bb.get("q1_total")
            if total is None:
                return "Q1 data unavailable"
            return f"Q1={total} ({'EVEN' if total % 2 == 0 else 'ODD'})"

        if stat_key == "q2_even":
            if not parsed_bb.get("q2_finished"):
                return "Q2 is still in progress"
            if not basketball_period_alert_is_fresh(parsed_bb, 2):
                return "Q2 alert window has expired"
            total = parsed_bb.get("q2_total")
            if total is None:
                return "Q2 data unavailable"
            return f"Q2={total} ({'EVEN' if total % 2 == 0 else 'ODD'})"

        if stat_key == "q1q2_even":
            if not parsed_bb.get("q1_finished") or not parsed_bb.get("q2_finished"):
                return "Waiting for Q1 and Q2 to finish"
            if not basketball_period_alert_is_fresh(parsed_bb, 2):
                return "Q1/Q2 alert window has expired"
            q1_total = parsed_bb.get("q1_total")
            q2_total = parsed_bb.get("q2_total")
            if q1_total is None or q2_total is None:
                return "Q1/Q2 data unavailable"
            state = "BOTH EVEN" if parsed_bb.get("q1q2_even") else "NOT BOTH EVEN"
            return f"Q1={q1_total}+Q2={q2_total} ({state})"

        field_map = {
            "points": "points",
            "q1_total": "q1_total",
            "q2_total": "q2_total",
            "q3_total": "q3_total",
            "q4_total": "q4_total",
            "half1": "half1_total",
        }
        final_flags = {
            "q1_total": "q1_finished",
            "q2_total": "q2_finished",
            "q3_total": "q3_finished",
            "q4_total": "q4_finished",
            "half1_total": "half1_finished",
        }
        field = field_map.get(stat_key)
        if field:
            final_flag = final_flags.get(field)
            if final_flag and not parsed_bb.get(final_flag):
                return "Period is not finished yet"
            if field == "q1_total" and not basketball_period_alert_is_fresh(parsed_bb, 1):
                return "Q1 alert window has expired"
            if field in {"q2_total", "half1_total"} and not basketball_period_alert_is_fresh(parsed_bb, 2):
                return "Q2 alert window has expired"
            if field == "q3_total" and not basketball_period_alert_is_fresh(parsed_bb, 3):
                return "Q3 alert window has expired"
            if field == "q4_total" and not basketball_period_alert_is_fresh(parsed_bb, 4):
                return "Q4 alert window has expired"
            value = parsed_bb.get(field)
            if value is not None:
                return f"{value} (need {alert['operator']} {alert['threshold']})"
        return "Data unavailable"

    return "?"


def format_football_notification(alert: dict, value: float, event: dict) -> str:
    home = event.get("homeTeam", {}).get("name", "?")
    away = event.get("awayTeam", {}).get("name", "?")
    hs = event.get("homeScore", {}).get("current", "?")
    as_ = event.get("awayScore", {}).get("current", "?")
    from sports_api import get_minute

    minute = get_minute(event)
    stat_display = alert["stat_key"].replace("_", " ").title()

    return (
        f"🔔 <b>АЛЕРТ СРАБОТАЛ!</b>\n\n"
        f"⚽ {_esc(home)} {hs}:{as_} {_esc(away)} ({_esc(minute)})\n\n"
        f"📊 <b>{_esc(stat_display)}:</b> {value}\n"
        f"🎯 Условие: {_esc(alert['stat_key'])} {alert['operator']} {alert['threshold']}\n\n"
        f"Алерт #{alert['id']} ✅"
    )


def format_basketball_notification(alert: dict, value: float, event: dict, extra: str = "") -> str:
    from sports_api import parse_basketball_scores

    scores = parse_basketball_scores(event)
    stat_info = BASKETBALL_STATS.get(alert["stat_key"], {})
    stat_label = stat_info.get("label", alert["stat_key"])

    lines = [
        "🔔 <b>АЛЕРТ СРАБОТАЛ!</b>\n",
        f"🏀 {_esc(scores['home_name'])} {scores['home_total'] or 0}:{scores['away_total'] or 0} {_esc(scores['away_name'])}",
        f"📍 {_esc(scores['status_desc'] or scores['status_type'] or '?')}\n",
    ]

    for idx in range(4):
        total = scores.get(f"q{idx + 1}_total")
        if total is not None:
            parity = "ЧЁТ ✅" if total % 2 == 0 else "НЕЧЕТ"
            lines.append(f"  Q{idx + 1}: {total} ({parity})")

    lines.append(f"\n📊 <b>{_esc(stat_label)}:</b> {value}")
    if extra:
        lines.append(f"ℹ️ {_esc(extra)}")
    lines.append(f"\n🎯 Алерт #{alert['id']} ✅")
    return "\n".join(lines)


def format_alert_summary(alert: dict) -> str:
    sport_emoji = SPORTS.get(alert.get("sport", "football"), {}).get("emoji", "🏟")
    stat_key = alert["stat_key"]
    special_labels = {
        "q1_even": "Q1 чётный",
        "q2_even": "Q2 чётный",
        "q1q2_even": "Q1+Q2 чётные",
    }
    stat_display = special_labels.get(stat_key, stat_key)
    team_str = f" [{alert['team']}]" if alert["team"] != "total" else ""
    return f"{sport_emoji} #{alert['id']} | {stat_display}{team_str} {alert['operator']} {alert['threshold']}"


# ─── Проверка невозможности срабатывания ────────────────

def alert_is_impossible(alert: dict, parsed_scores: dict) -> bool:
    """
    Возвращает True если условие алерта уже точно не пройдёт:
    - четверть завершилась с нечётным тоталом (для even-алертов)
    - четверть завершилась, но тотал не достигнет порога (N < threshold при операторе >)
    - матч завершён
    """
    sport = alert.get("sport", "basketball")
    stat_key = alert["stat_key"]
    operator = alert["operator"]
    threshold = alert["threshold"]

    if sport != "basketball":
        # Для футбола: матч завершён — больше угловых/ударов не будет
        status = parsed_scores.get("status_type", "")
        if status == "finished":
            return True
        return False

    status = parsed_scores.get("status_type", "")

    # Если матч завершён и алерт ещё активен — он не сработал, невозможно
    if status == "finished":
        return True

    def _period_impossible_even(finished_key: str, even_key: str) -> bool:
        """Четверть завершилась и тотал нечётный — even-алерт невозможен."""
        if not parsed_scores.get(finished_key):
            return False  # ещё не завершилась — ещё возможно
        return parsed_scores.get(even_key) is False  # False = нечётный

    def _period_impossible_threshold(finished_key: str, total_key: str) -> bool:
        """Четверть завершилась и тотал не достигнет порога."""
        if not parsed_scores.get(finished_key):
            return False
        value = parsed_scores.get(total_key)
        if value is None:
            return False
        # Невозможно если: значение < порог при операторе > или >=
        if operator in (">", ">=") and value <= threshold:
            return True
        # значение > порог при операторе < или <=
        if operator in ("<", "<=") and value >= threshold:
            return True
        return False

    # q1_even: Q1 завершилась нечётно
    if stat_key == "q1_even":
        return _period_impossible_even("q1_finished", "q1_even")

    # q2_even: Q2 завершилась нечётно
    if stat_key == "q2_even":
        return _period_impossible_even("q2_finished", "q2_even")

    # q1q2_even: Q1 завершилась нечётно (даже если Q2 ещё идёт)
    if stat_key == "q1q2_even":
        if parsed_scores.get("q1_finished") and parsed_scores.get("q1_even") is False:
            return True  # Q1 нечётный — q1q2_even уже невозможен
        if parsed_scores.get("q2_finished") and parsed_scores.get("q2_even") is False:
            return True  # Q2 нечётный
        return False

    # q1_total, q2_total, q3_total, q4_total, half1
    period_map = {
        "q1_total": ("q1_finished", "q1_total"),
        "q2_total": ("q2_finished", "q2_total"),
        "q3_total": ("q3_finished", "q3_total"),
        "q4_total": ("q4_finished", "q4_total"),
        "half1":    ("half1_finished", "half1_total"),
    }
    if stat_key in period_map:
        fk, tk = period_map[stat_key]
        return _period_impossible_threshold(fk, tk)

    # points (тотал матча): если матч завершён
    if stat_key == "points" and status == "finished":
        value = parsed_scores.get("points", 0)
        if operator in (">", ">=") and (value or 0) <= threshold:
            return True
        if operator in ("<", "<=") and (value or 0) >= threshold:
            return True

    return False


def _to_num(value) -> float | None:
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
