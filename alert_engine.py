"""
Alert Engine — checks conditions for football and basketball.
Supports standard comparisons AND special even/odd basketball alerts.
"""
import logging
import operator as op
import html
from config import FOOTBALL_STATS, BASKETBALL_STATS, SPORTS

logger = logging.getLogger(__name__)


def _esc(value) -> str:
    return html.escape(str(value), quote=True)

OPERATORS = {
    ">":  op.gt,
    "<":  op.lt,
    ">=": op.ge,
    "<=": op.le,
    "==": op.eq,
}


# ═════════════════════════════════════════════════════════
#  FOOTBALL
# ═════════════════════════════════════════════════════════

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
    elif team == "away":
        return float(as_) if as_ is not None else None
    else:
        if hs is None and as_ is None:
            return None
        return float((hs or 0) + (as_ or 0))


def _extract_football_stat(stats: dict, api_name: str, team: str) -> float | None:
    if team == "total":
        h = _to_num(stats.get("home", {}).get(api_name))
        a = _to_num(stats.get("away", {}).get(api_name))
        if h is None and a is None:
            return None
        return (h or 0) + (a or 0)
    return _to_num(stats.get(team, {}).get(api_name))


# ═════════════════════════════════════════════════════════
#  BASKETBALL
# ═════════════════════════════════════════════════════════

def check_basketball_alert(alert: dict, parsed_scores: dict) -> tuple[bool, float | None, str]:
    stat_key = alert["stat_key"]
    threshold = alert["threshold"]
    oper_str = alert["operator"]

    # Special even/odd alerts
    if stat_key == "q1_even":
        val = parsed_scores.get("q1_even")
        if val is None:
            return False, None, "Q1 ещё не сыгран"
        qt = parsed_scores.get("q1_total", 0)
        if val:
            return True, float(qt), f"Q1 тотал = {qt} (ЧЁТ ✅)"
        return False, float(qt), f"Q1 тотал = {qt} (НЕЧЕТ)"

    if stat_key == "q2_even":
        val = parsed_scores.get("q2_even")
        if val is None:
            return False, None, "Q2 ещё не сыгран"
        qt = parsed_scores.get("q2_total", 0)
        if val:
            return True, float(qt), f"Q2 тотал = {qt} (ЧЁТ ✅)"
        return False, float(qt), f"Q2 тотал = {qt} (НЕЧЕТ)"

    if stat_key == "q1q2_even":
        val = parsed_scores.get("q1q2_even")
        if val is None:
            return False, None, "Q1 и Q2 ещё не оба сыграны"
        q1t = parsed_scores.get("q1_total", 0)
        q2t = parsed_scores.get("q2_total", 0)
        q1e = "ЧЁТ" if parsed_scores.get("q1_even") else "НЕЧЕТ"
        q2e = "ЧЁТ" if parsed_scores.get("q2_even") else "НЕЧЕТ"
        info = f"Q1={q1t}({q1e}), Q2={q2t}({q2e})"
        if val:
            return True, float(q1t + q2t), f"Q1+Q2 ОБЕ ЧЁТНЫЕ ✅\n{info}"
        return False, float(q1t + q2t), f"Не обе чётные\n{info}"

    # Standard numeric alerts
    compare_fn = OPERATORS.get(oper_str)
    if not compare_fn:
        return False, None, ""

    field_map = {
        "points":   "points",
        "q1_total": "q1_total",
        "q2_total": "q2_total",
        "q3_total": "q3_total",
        "q4_total": "q4_total",
        "half1":    "half1_total",
    }
    field = field_map.get(stat_key)
    if not field:
        return False, None, f"Неизвестный stat: {stat_key}"

    value = parsed_scores.get(field)
    if value is None:
        return False, None, "Данные ещё недоступны"

    value = float(value)
    triggered = compare_fn(value, threshold)
    return triggered, value, ""


# ═════════════════════════════════════════════════════════
#  LIVE STATS STRING (for alert status display)
# ═════════════════════════════════════════════════════════

def format_live_alert_status(alert: dict, stats: dict = None, event: dict = None, parsed_bb: dict = None) -> str:
    """Return a short status line for an alert showing current value."""
    sport = alert.get("sport", "football")
    stat_key = alert["stat_key"]

    if sport == "football":
        if event is None:
            return "⏳ нет данных"
        from sports_api import is_live, is_finished, is_not_started
        if is_not_started(event):
            return "⏰ не начался"
        if is_finished(event):
            return "✅ завершён"

        if stat_key == "goals":
            val = _get_football_goals(event, alert.get("team", "total"))
        else:
            info = FOOTBALL_STATS.get(stat_key)
            if info and stats:
                val = _extract_football_stat(stats, info["api_name"], alert.get("team", "total"))
            else:
                val = None
        if val is not None:
            return f"📊 {val} (нужно {alert['operator']} {alert['threshold']})"
        return "⏳ стат недоступна"

    elif sport == "basketball":
        if parsed_bb is None:
            return "⏳ нет данных"

        special_keys = {"q1_even", "q2_even", "q1q2_even"}
        if stat_key in special_keys:
            q_key = stat_key.replace("_even", "_total") if stat_key != "q1q2_even" else None
            if stat_key == "q1_even":
                qt = parsed_bb.get("q1_total")
                if qt is None: return "⏳ Q1 не сыгран"
                return f"Q1={qt} ({'ЧЁТ ✅' if qt % 2 == 0 else 'НЕЧЕТ ❌'})"
            elif stat_key == "q2_even":
                qt = parsed_bb.get("q2_total")
                if qt is None: return "⏳ Q2 не сыгран"
                return f"Q2={qt} ({'ЧЁТ ✅' if qt % 2 == 0 else 'НЕЧЕТ ❌'})"
            elif stat_key == "q1q2_even":
                q1 = parsed_bb.get("q1_total")
                q2 = parsed_bb.get("q2_total")
                if q1 is None: return "⏳ Q1 не сыгран"
                if q2 is None: return f"Q1={q1}, Q2 в процессе"
                return f"Q1={q1}+Q2={q2} ({'ОБЕ ЧЁТ ✅' if parsed_bb.get('q1q2_even') else '❌'})"

        field_map = {"points": "points", "q1_total": "q1_total", "q2_total": "q2_total",
                     "q3_total": "q3_total", "q4_total": "q4_total", "half1": "half1_total"}
        field = field_map.get(stat_key)
        if field:
            val = parsed_bb.get(field)
            if val is not None:
                return f"📊 {val} (нужно {alert['operator']} {alert['threshold']})"
        return "⏳ данные недоступны"

    return "?"


# ═════════════════════════════════════════════════════════
#  FORMATTING
# ═════════════════════════════════════════════════════════

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
    s = parse_basketball_scores(event)

    stat_info = BASKETBALL_STATS.get(alert["stat_key"], {})
    stat_label = stat_info.get("label", alert["stat_key"])

    lines = [
        f"🔔 <b>АЛЕРТ СРАБОТАЛ!</b>\n",
        f"🏀 {_esc(s['home_name'])} {s['home_total'] or 0}:{s['away_total'] or 0} {_esc(s['away_name'])}",
        f"📍 {_esc(s['status_desc'] or s['status_type'] or '?')}\n",
    ]

    for i in range(4):
        qt = s.get(f"q{i+1}_total")
        if qt is not None:
            parity = "ЧЁТ ✅" if qt % 2 == 0 else "НЕЧЕТ"
            lines.append(f"  Q{i+1}: {qt} ({parity})")

    lines.append(f"\n📊 <b>{_esc(stat_label)}:</b> {value}")
    if extra:
        lines.append(f"ℹ️ {_esc(extra)}")
    lines.append(f"\n🎯 Алерт #{alert['id']} ✅")

    return "\n".join(lines)


def format_alert_summary(alert: dict) -> str:
    sport_emoji = SPORTS.get(alert.get("sport", "football"), {}).get("emoji", "🏟")
    stat_key = alert["stat_key"]

    special_labels = {
        "q1_even":   "Q1 чётный",
        "q2_even":   "Q2 чётный",
        "q1q2_even": "Q1+Q2 чётные",
    }
    stat_display = special_labels.get(stat_key, stat_key)

    team_str = ""
    if alert["team"] != "total":
        team_str = f" [{alert['team']}]"

    return (
        f"{sport_emoji} #{alert['id']} | "
        f"{stat_display}{team_str} {alert['operator']} {alert['threshold']}"
    )


# ═════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════

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
