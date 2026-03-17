"""
🏟 Sports Alerts Bot v2 — SofaScore Edition
Features:
  - Browse matches by day with ◀ ▶ pagination
  - Bulk alerts: set alert on ALL matches of a day
  - Smart polling with in-memory cache (1 request serves all alerts)
  - Live stats view per alert
  - Match start detection: warn if match is overdue
  - Q1Q2 even alerts for basketball
"""
import logging
import asyncio
import time
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN, POLL_INTERVAL_SECONDS,
    FOOTBALL_STATS, BASKETBALL_STATS,
    SUPPORTED_OPERATORS, SPORTS, MATCH_START_TOLERANCE,
)
from auth import is_authorized, pending_requests, approve_user, reject_user, get_admin_ids, add_pending_request
import database as db
import sports_api as api
from alert_engine import (
    check_football_alert, check_basketball_alert,
    format_football_notification, format_basketball_notification,
    format_alert_summary, format_live_alert_status,
)

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DAY_NAMES = {
    0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
    4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}


# ═══════════════════════════════════════════════════════
#  MENU / NAVIGATION
# ═══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        # Check if already pending
        if user_id in pending_requests:
            await update.message.reply_text(
                "⏳ <b>Твоя заявка на рассмотрении.</b>\nАдмин скоро проверит.",
                parse_mode=ParseMode.HTML)
            return
        # Send access request to admins
        user = update.effective_user
        name = user.full_name or user.username or str(user_id)
        username = f"@{user.username}" if user.username else "нет username"
        add_pending_request(user_id, name)
        for admin_id in get_admin_ids():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🔐 <b>Запрос доступа</b>\n\n"
                        f"👤 {name} ({username})\n"
                        f"🆔 <code>{user_id}</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Одобрить", callback_data=f"auth_approve:{user_id}"),
                         InlineKeyboardButton("❌ Отклонить", callback_data=f"auth_reject:{user_id}")]
                    ]))
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)
        await update.message.reply_text(
            "🔐 <b>Доступ ограничен</b>\n\n"
            "Заявка отправлена администратору.\n"
            "Ожидай подтверждения!",
            parse_mode=ParseMode.HTML)
        return
    await _show_main_menu(update.message, user_id=update.effective_user.id)


async def _show_main_menu(target, edit=False, user_id=None):
    # Count active alerts if user_id is known
    alert_count = 0
    if user_id:
        alert_count = await db.count_user_active_alerts(user_id)

    text = "🏟 <b>Sports Alerts Bot v2</b>\n<i>SofaScore • Бесплатно • Без API ключа</i>\n\nВыбери спорт:"
    buttons = [
        [InlineKeyboardButton(f"{v['emoji']} {v['label']}", callback_data=f"sport:{k}")]
        for k, v in SPORTS.items()
    ]
    alert_label = f"📋 Мои алерты ({alert_count})" if alert_count else "📋 Мои алерты"
    buttons.append([InlineKeyboardButton(alert_label, callback_data="myalerts:0")])
    if alert_count > 0:
        buttons.append([
            InlineKeyboardButton(f"❌ Отменить ВСЕ алерты ({alert_count})", callback_data="cancel_all_confirm"),
        ])
    buttons.append([InlineKeyboardButton("❓ Помощь", callback_data="help")])
    keyboard = InlineKeyboardMarkup(buttons)
    if edit:
        await target.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def _show_sport_menu(query, sport: str):
    info = SPORTS[sport]
    text = f"{info['emoji']} <b>{info['label']}</b>\n\nЧто хочешь сделать?"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Live матчи", callback_data=f"live:{sport}")],
        [InlineKeyboardButton("📅 Матчи по дням", callback_data=f"day:{sport}:0")],
        [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts:0")],
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ═══════════════════════════════════════════════════════
#  DAY BROWSER
# ═══════════════════════════════════════════════════════

async def _show_day(query, sport: str, day_offset: int):
    target_date = datetime.now() + timedelta(days=day_offset)
    date_str = target_date.strftime("%Y-%m-%d")
    day_name = DAY_NAMES.get(target_date.weekday(), "")
    date_display = target_date.strftime("%d.%m.%Y")

    await query.edit_message_text(f"🔍 Загружаю матчи на {date_display}...")

    if sport == "football":
        events = await api.football_by_date(date_str)
    else:
        events = await api.basketball_by_date(date_str)

    if not events:
        text = f"📅 <b>{day_name}, {date_display}</b>\n\nНет матчей на этот день."
        nav = _day_nav_buttons(sport, day_offset)
        nav.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(nav))
        return

    live_ev = [e for e in events if api.is_live(e)]
    sched_ev = [e for e in events if api.is_not_started(e)]
    fin_ev = [e for e in events if api.is_finished(e)]

    emoji = SPORTS[sport]["emoji"]
    lines = [f"📅 <b>{day_name}, {date_display}</b>"]
    lines.append(f"{emoji} Всего: {len(events)} матчей\n")

    match_buttons = []

    if live_ev:
        lines.append(f"🔴 <b>Live ({len(live_ev)}):</b>")
        for ev in live_ev[:15]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {api.format_football_short(ev)}")
            else:
                lines.append(f"  <code>{eid}</code> | {api.format_basketball_short(ev)}")
            _add_match_btn(match_buttons, ev, sport)
        lines.append("")

    if sched_ev:
        lines.append(f"⏰ <b>Запланированы ({len(sched_ev)}):</b>")
        sched_ev.sort(key=lambda e: api.get_kickoff_timestamp(e))
        for ev in sched_ev[:20]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {api.format_football_short(ev)}")
            else:
                home = api.get_home_name(ev)
                away = api.get_away_name(ev)
                ts = api.get_kickoff_timestamp(ev)
                t = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "TBD"
                lines.append(f"  <code>{eid}</code> | {home} vs {away} (⏰ {t})")
            _add_match_btn(match_buttons, ev, sport)
        lines.append("")

    if fin_ev:
        lines.append(f"✅ <b>Завершены ({len(fin_ev)}):</b>")
        for ev in fin_ev[:10]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {api.format_football_short(ev)}")
            else:
                lines.append(f"  <code>{eid}</code> | {api.format_basketball_short(ev)}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n\n... (список обрезан)"

    buttons = []
    buttons.extend(match_buttons[:10])

    # BULK ALERT buttons
    if sched_ev or live_ev:
        bulk_targets = sched_ev + live_ev
        if sport == "basketball" and bulk_targets:
            buttons.append([InlineKeyboardButton(
                f"🎲 Q1Q2 ЧЁТ на ВСЕ ({len(bulk_targets)})",
                callback_data=f"bulk:{sport}:{date_str}:q1q2_even")])
        if bulk_targets:
            buttons.append([InlineKeyboardButton(
                f"🔔 Алерт на ВСЕ матчи ({len(bulk_targets)})",
                callback_data=f"bulk_select:{sport}:{date_str}")])

    buttons.extend(_day_nav_buttons(sport, day_offset))
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


def _day_nav_buttons(sport, day_offset):
    nav_row = []
    if day_offset > -7:
        nav_row.append(InlineKeyboardButton("◀ Пред.", callback_data=f"day:{sport}:{day_offset - 1}"))
    if day_offset != 0:
        nav_row.append(InlineKeyboardButton("📍 Сегодня", callback_data=f"day:{sport}:0"))
    if day_offset < 7:
        nav_row.append(InlineKeyboardButton("След. ▶", callback_data=f"day:{sport}:{day_offset + 1}"))
    return [nav_row] if nav_row else []


def _add_match_btn(buttons, ev, sport):
    eid = api.get_event_id(ev)
    home = api.get_home_name(ev)[:10]
    away = api.get_away_name(ev)[:10]
    emoji = "⚽" if sport == "football" else "🏀"
    btn = InlineKeyboardButton(f"{emoji} {home}-{away}", callback_data=f"match:{sport}:{eid}")
    if buttons and isinstance(buttons[-1], list) and len(buttons[-1]) == 1:
        buttons[-1].append(btn)
    else:
        buttons.append([btn])


# ═══════════════════════════════════════════════════════
#  LIVE LIST
# ═══════════════════════════════════════════════════════

async def _show_live(query, sport: str):
    await query.edit_message_text("🔍 Загружаю live...")

    if sport == "football":
        events = await api.football_live()
    else:
        events = await api.basketball_live()

    if not events:
        await query.edit_message_text(
            f"Сейчас нет live-матчей ⏳\n\nПопробуй '📅 Матчи по дням'",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Матчи по дням", callback_data=f"day:{sport}:0")],
                [InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
            ]))
        return

    emoji = SPORTS[sport]["emoji"]
    lines = [f"⚡ <b>Live {SPORTS[sport]['label']} ({len(events)}):</b>\n"]
    buttons = []

    if sport == "football":
        # Group by tournament
        tournaments = {}
        for ev in events:
            t_name = api.get_tournament_name(ev)
            tournaments.setdefault(t_name, []).append(ev)
        shown = 0
        for t_name in sorted(tournaments.keys()):
            if shown >= 20:
                break
            lines.append(f"\n🏆 <b>{t_name}</b>")
            for ev in tournaments[t_name][:5]:
                lines.append(f"  <code>{api.get_event_id(ev)}</code> | {api.format_football_short(ev)}")
                shown += 1
    else:
        for ev in events[:15]:
            lines.append(f"  <code>{api.get_event_id(ev)}</code> | {api.format_basketball_short(ev)}")

    lines.append("\n👇 Нажми на матч:")
    row = []
    for ev in events[:10]:
        home = api.get_home_name(ev)[:8]
        away = api.get_away_name(ev)[:8]
        row.append(InlineKeyboardButton(
            f"{emoji} {home}-{away}",
            callback_data=f"match:{sport}:{api.get_event_id(ev)}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Bulk alert on all live
    if sport == "basketball":
        buttons.append([InlineKeyboardButton(
            f"🎲 Q1Q2 ЧЁТ на ВСЕ live ({len(events)})",
            callback_data=f"bulk_live:{sport}:q1q2_even")])

    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n..."
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  MATCH DETAIL
# ═══════════════════════════════════════════════════════

async def _show_match(query, sport, match_id):
    if sport == "football":
        ev = await api.football_event(match_id)
        if not ev:
            await query.edit_message_text("❌ Матч не найден.")
            return
        if api.is_live(ev) or api.is_finished(ev):
            stats = await api.football_statistics(match_id)
            text = _format_football_detail(ev, stats)
        else:
            text = _format_football_detail(ev, None)
    else:
        ev = await api.basketball_event(match_id)
        if not ev:
            await query.edit_message_text("❌ Матч не найден.")
            return
        text = api.format_basketball_detail(ev)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Создать алерт", callback_data=f"alert_type:{sport}:{match_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"match:{sport}:{match_id}"),
         InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
    ])
    if len(text) > 4000:
        text = text[:4000]
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def _format_football_detail(ev, stats):
    lines = [f"⚽ <b>{api.format_football_short(ev)}</b>"]
    t_name = api.get_tournament_name(ev)
    if t_name:
        lines.append(f"🏆 {t_name}")

    if api.is_not_started(ev):
        ts = api.get_kickoff_timestamp(ev)
        if ts:
            lines.append(f"\n⏰ <b>Начало:</b> {datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}")
        lines.append("\n💡 Можешь поставить алерт заранее!")

    if stats and (stats.get("home") or stats.get("away")):
        lines.append("\n📊 <b>Статистика:</b>")
        stat_display = [
            ("ballPossession", "Владение"),
            ("totalShots", "Удары"),
            ("shotsOnTarget", "В створ"),
            ("cornerKicks", "Угловые"),
            ("fouls", "Фолы"),
            ("yellowCards", "Жёлтые"),
            ("redCards", "Красные"),
            ("offsides", "Офсайды"),
        ]
        for api_name, label in stat_display:
            h = stats.get("home", {}).get(api_name)
            a = stats.get("away", {}).get(api_name)
            if h is not None or a is not None:
                lines.append(f"  {str(h or '-'):>6}  {label:<16} {str(a or '-')}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  ALERT CREATION
# ═══════════════════════════════════════════════════════

async def _show_alert_type_selector(query, sport, match_id):
    stats_dict = FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS
    emoji = "⚽" if sport == "football" else "🏀"
    buttons = []
    row = []
    for key, info in stats_dict.items():
        row.append(InlineKeyboardButton(
            f"{info['emoji']} {info['label']}",
            callback_data=f"alert_stat:{sport}:{match_id}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"match:{sport}:{match_id}")])
    await query.edit_message_text(
        f"{emoji} Матч <code>{match_id}</code>\n\n📊 <b>Выбери тип алерта:</b>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_alert_stat_selected(query, context, sport, match_id, stat_key):
    stat_info = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {})
    label = stat_info.get("label", stat_key)

    # Auto-create alerts for even/odd types (no threshold needed)
    if stat_key in ("q1_even", "q2_even", "q1q2_even"):
        kickoff_at = await _get_kickoff_for_alert(sport, match_id)
        match_label = await _get_match_label(sport, match_id)
        alert_id = await db.add_alert(
            user_id=query.from_user.id, chat_id=query.message.chat_id,
            fixture_id=match_id, stat_key=stat_key, operator="==", threshold=1,
            team="total", sport=sport, kickoff_at=kickoff_at, match_label=match_label)
        await query.edit_message_text(
            f"✅ <b>Алерт #{alert_id} создан!</b>\n\n🏀 {match_label}\n📊 Тип: <b>{label}</b>\n{_scheduled_text(kickoff_at)}",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ещё алерт", callback_data=f"alert_type:{sport}:{match_id}")],
                [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
                [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))
        return

    # Show preset options
    context.user_data["pending_alert"] = {"sport": sport, "fixture_id": match_id, "stat_key": stat_key}
    presets_map = {
        "corners": ["> 5", "> 7", "> 9", "> 11"],
        "goals": ["> 1", "> 2", "> 3", ">= 4"],
        "yellow": ["> 2", "> 3", "> 4", "> 5"],
        "fouls": ["> 10", "> 15", "> 20", "> 25"],
        "possession": ["> 55", "> 60", "> 65", "> 70"],
        "points": ["> 150", "> 180", "> 200", "> 220"],
        "q1_total": ["> 40", "> 45", "> 50", "> 55"],
        "q2_total": ["> 40", "> 45", "> 50", "> 55"],
        "q3_total": ["> 40", "> 45", "> 50", "> 55"],
        "q4_total": ["> 40", "> 45", "> 50", "> 55"],
        "half1": ["> 90", "> 100", "> 110", "> 120"],
    }
    presets = presets_map.get(stat_key, ["> 3", "> 5", "> 8", "> 10"])
    buttons = []
    row = []
    for p in presets:
        row.append(InlineKeyboardButton(p, callback_data=f"alert_quick:{sport}:{match_id}:{stat_key}:{p}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"alert_type:{sport}:{match_id}")])
    await query.edit_message_text(
        f"📊 <b>{label}</b> | Матч {match_id}\n\nВыбери условие или напиши своё:\n<code>> 8</code> или <code>>= 5 home</code>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_quick_alert(query, sport, match_id, stat_key, condition):
    parts = condition.split()
    oper, threshold = parts[0], float(parts[1])
    kickoff_at = await _get_kickoff_for_alert(sport, match_id)
    match_label = await _get_match_label(sport, match_id)
    alert_id = await db.add_alert(
        user_id=query.from_user.id, chat_id=query.message.chat_id,
        fixture_id=match_id, stat_key=stat_key, operator=oper, threshold=threshold,
        team="total", sport=sport, kickoff_at=kickoff_at, match_label=match_label)
    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    await query.edit_message_text(
        f"✅ <b>Алерт #{alert_id} создан!</b>\n\n{SPORTS[sport]['emoji']} {match_label}\n📊 {stat_label} {oper} {threshold}\n{_scheduled_text(kickoff_at)}",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ещё алерт", callback_data=f"alert_type:{sport}:{match_id}")],
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


# ═══════════════════════════════════════════════════════
#  BULK ALERTS
# ═══════════════════════════════════════════════════════

async def _handle_bulk_alert(query, sport, date_str, stat_key):
    """Create alert on ALL matches for a given date."""
    if sport == "football":
        events = await api.football_by_date(date_str)
    else:
        events = await api.basketball_by_date(date_str)

    targets = [e for e in events if api.is_not_started(e) or api.is_live(e)]
    if not targets:
        await query.edit_message_text("Нет доступных матчей для алертов.")
        return

    matches = []
    for ev in targets:
        matches.append({
            "fixture_id": api.get_event_id(ev),
            "kickoff_at": api.get_kickoff_timestamp(ev),
            "match_label": api.get_match_label(ev),
        })

    # For even alerts: operator==, threshold=1
    if stat_key in ("q1_even", "q2_even", "q1q2_even"):
        ids = await db.add_alerts_bulk(
            user_id=query.from_user.id, chat_id=query.message.chat_id,
            sport=sport, matches=matches,
            stat_key=stat_key, operator="==", threshold=1)
    else:
        # For bulk with presets we just do a selector
        await _show_bulk_stat_selector(query, sport, date_str)
        return

    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    await query.edit_message_text(
        f"✅ <b>Создано {len(ids)} алертов!</b>\n\n"
        f"📊 {stat_label} на все матчи {date_str}\n"
        f"🆔 #{ids[0]}—#{ids[-1]}",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


async def _handle_bulk_live(query, sport, stat_key):
    """Create alert on ALL currently live matches."""
    if sport == "football":
        events = await api.football_live()
    else:
        events = await api.basketball_live()

    if not events:
        await query.edit_message_text("Нет live-матчей.")
        return

    matches = []
    for ev in events:
        matches.append({
            "fixture_id": api.get_event_id(ev),
            "kickoff_at": 0,
            "match_label": api.get_match_label(ev),
        })

    ids = await db.add_alerts_bulk(
        user_id=query.from_user.id, chat_id=query.message.chat_id,
        sport=sport, matches=matches,
        stat_key=stat_key, operator="==", threshold=1)

    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    await query.edit_message_text(
        f"✅ <b>Создано {len(ids)} алертов!</b>\n\n"
        f"📊 {stat_label} на все live-матчи\n"
        f"🆔 #{ids[0]}—#{ids[-1]}",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


async def _show_bulk_stat_selector(query, sport, date_str):
    """Show stat type selector for bulk alerts."""
    stats_dict = FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS
    buttons = []
    row = []
    for key, info in stats_dict.items():
        row.append(InlineKeyboardButton(
            f"{info['emoji']} {info['label']}",
            callback_data=f"bulk:{sport}:{date_str}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"day:{sport}:0")])
    await query.edit_message_text(
        "📊 <b>Выбери тип алерта для ВСЕХ матчей:</b>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  MY ALERTS (with live stats)
# ═══════════════════════════════════════════════════════

ALERTS_PER_PAGE = 10


async def _show_my_alerts(query, page=0):
    alerts = await db.get_user_alerts(query.from_user.id)
    if not alerts:
        await query.edit_message_text(
            "У тебя нет активных алертов.\n\nСоздай через главное меню!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))
        return

    total = len(alerts)
    total_pages = (total + ALERTS_PER_PAGE - 1) // ALERTS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * ALERTS_PER_PAGE
    end = min(start + ALERTS_PER_PAGE, total)
    page_alerts = alerts[start:end]

    now = time.time()
    lines = [f"🔔 <b>Активные алерты ({total}):</b>"]
    if total_pages > 1:
        lines.append(f"📄 Страница {page + 1}/{total_pages}")
    lines.append("")

    buttons = []

    # Cancel all / Clear all buttons FIRST (always visible)
    if total > 1:
        buttons.append([
            InlineKeyboardButton(f"❌ Отменить ВСЕ ({total})", callback_data="cancel_all_confirm"),
            InlineKeyboardButton("🗑 Очистить", callback_data="clear_all_confirm"),
        ])

    for a in page_alerts:
        summary = format_alert_summary(a)
        if a.get("match_label"):
            summary = f"{SPORTS.get(a['sport'], {}).get('emoji', '🏟')} {a['match_label']}\n{summary}"

        if a.get("kickoff_at", 0) > now:
            summary += f"\n   ⏰ Ждёт начала: {datetime.fromtimestamp(a['kickoff_at']).strftime('%d.%m %H:%M')}"
        else:
            summary += "\n   ⚡ Мониторинг активен"

        lines.append(summary)
        lines.append("")

        btn_row = [
            InlineKeyboardButton(f"📊 #{a['id']}", callback_data=f"alert_status:{a['id']}"),
            InlineKeyboardButton(f"❌ #{a['id']}", callback_data=f"cancel_alert:{a['id']}:{page}"),
        ]
        buttons.append(btn_row)

    # Pagination buttons
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀ Назад", callback_data=f"myalerts:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"myalerts:{page + 1}"))
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"myalerts:{page}")])
    buttons.append([InlineKeyboardButton("🏠 Главная", callback_data="main_menu")])

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000]
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        if "Message is not modified" in str(e):
            pass  # Same content — ignore
        else:
            raise


async def _show_alert_status(query, alert_id):
    """Show live statistics for a specific alert."""
    alert = await db.get_alert_by_id(alert_id)
    if not alert or not alert["active"]:
        await query.answer("Алерт не найден или неактивен", show_alert=True)
        return

    sport = alert["sport"]
    fid = alert["fixture_id"]
    status_line = "⏳ загружаю..."

    try:
        if sport == "football":
            ev = await api.football_event(fid)
            if ev:
                stats = {}
                if api.is_live(ev):
                    stats = await api.football_statistics(fid)
                status_line = format_live_alert_status(alert, stats=stats, event=ev)
                match_info = api.format_football_short(ev)
            else:
                status_line = "❌ матч не найден"
                match_info = f"ID: {fid}"
        else:
            ev = await api.basketball_event(fid)
            if ev:
                parsed = api.parse_basketball_scores(ev)
                status_line = format_live_alert_status(alert, parsed_bb=parsed)
                match_info = f"{parsed['home_name']} {parsed['home_total'] or 0}:{parsed['away_total'] or 0} {parsed['away_name']}"
            else:
                status_line = "❌ матч не найден"
                match_info = f"ID: {fid}"
    except Exception as e:
        logger.error("Alert status error: %s", e)
        status_line = f"❌ ошибка: {e}"
        match_info = f"ID: {fid}"

    stat_label = format_alert_summary(alert)
    text = (
        f"📊 <b>Статус алерта #{alert_id}</b>\n\n"
        f"{SPORTS.get(sport, {}).get('emoji', '')} {match_info}\n"
        f"{stat_label}\n\n"
        f"<b>Текущее значение:</b> {status_line}"
    )
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"alert_status:{alert_id}")],
            [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_alert:{alert_id}")],
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
        ]))


# ═══════════════════════════════════════════════════════
#  HELP
# ═══════════════════════════════════════════════════════

async def _show_help(query):
    text = (
        "📖 <b>Как пользоваться:</b>\n\n"
        "<b>1.</b> Выбери спорт (⚽ или 🏀)\n"
        "<b>2.</b> '📅 Матчи по дням' → листай ◀ ▶\n"
        "<b>3.</b> Нажми на матч → выбери тип алерта\n"
        "<b>4.</b> Выбери условие из списка или напиши своё\n"
        "<b>5.</b> Получи уведомление!\n\n"
        "<b>🎲 Массовые алерты:</b>\n"
        "В списке матчей есть кнопка 'на ВСЕ' —\n"
        "ставит алерт сразу на все матчи дня.\n\n"
        "<b>📊 Статус алерта:</b>\n"
        "В 'Мои алерты' → кнопка 📊 покажет\n"
        "текущее значение статистики live.\n\n"
        "<b>🕐 Алерты на будущие матчи:</b>\n"
        "Бот спит до начала — 0 запросов.\n"
        "Если матч задерживается >1ч — предупредит.\n\n"
        "<b>🔄 Источник данных:</b> SofaScore (бесплатно)\n"
        "Опрос: каждые 2 мин (live-матчи)\n"
        "Кэш: запросы не дублируются\n\n"
        "Вручную: <code>/setalert football 12345 corners > 8</code>"
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════

async def _get_kickoff_for_alert(sport, match_id):
    if sport == "football":
        ev = await api.football_event(match_id)
        if ev and api.is_not_started(ev):
            return api.get_kickoff_timestamp(ev)
    elif sport == "basketball":
        ev = await api.basketball_event(match_id)
        if ev and api.is_not_started(ev):
            return api.get_kickoff_timestamp(ev)
    return 0


async def _get_match_label(sport, match_id):
    if sport == "football":
        ev = await api.football_event(match_id)
        if ev:
            return api.get_match_label(ev)
    elif sport == "basketball":
        ev = await api.basketball_event(match_id)
        if ev:
            return api.get_match_label(ev)
    return f"Матч #{match_id}"


def _scheduled_text(kickoff_at):
    if kickoff_at <= 0 or kickoff_at <= time.time():
        return f"\n⚡ Мониторинг активен — проверяю каждые {POLL_INTERVAL_SECONDS} сек."
    dt = datetime.fromtimestamp(kickoff_at)
    return (
        f"\n⏰ Матч начнётся: <b>{dt.strftime('%d.%m %H:%M')}</b>\n"
        f"😴 Бот спит до начала — 0 запросов.\n"
        f"⚡ Мониторинг включится автоматически!"
    )


# ═══════════════════════════════════════════════════════
#  CALLBACK ROUTER
# ═══════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # "Query is too old" — ignore stale callbacks
    data = query.data

    # Auth callbacks don't require authorization
    if not data.startswith("auth_") and not is_authorized(query.from_user.id):
        try:
            await query.edit_message_text("🔐 Доступ ограничен. Нажми /start для запроса доступа.")
        except: pass
        return

    try:
        if data == "main_menu":
            await _show_main_menu(query, edit=True, user_id=query.from_user.id)
        elif data.startswith("sport:"):
            await _show_sport_menu(query, data.split(":")[1])
        elif data.startswith("day:"):
            _, s, o = data.split(":")
            await _show_day(query, s, int(o))
        elif data.startswith("live:"):
            await _show_live(query, data.split(":")[1])
        elif data.startswith("match:"):
            _, s, m = data.split(":")
            await _show_match(query, s, int(m))
        elif data.startswith("alert_type:"):
            _, s, m = data.split(":")
            await _show_alert_type_selector(query, s, int(m))
        elif data.startswith("alert_stat:"):
            _, s, m, k = data.split(":")
            await _handle_alert_stat_selected(query, context, s, int(m), k)
        elif data.startswith("alert_quick:"):
            p = data.split(":", 4)
            await _handle_quick_alert(query, p[1], int(p[2]), p[3], p[4])
        elif data.startswith("alert_status:"):
            aid = int(data.split(":")[1])
            await _show_alert_status(query, aid)
        elif data.startswith("bulk:"):
            # bulk:sport:date:stat_key
            _, s, d, k = data.split(":")
            await _handle_bulk_alert(query, s, d, k)
        elif data.startswith("bulk_live:"):
            # bulk_live:sport:stat_key
            _, s, k = data.split(":")
            await _handle_bulk_live(query, s, k)
        elif data.startswith("bulk_select:"):
            _, s, d = data.split(":")
            await _show_bulk_stat_selector(query, s, d)
        elif data.startswith("myalerts"):
            # myalerts or myalerts:PAGE
            parts = data.split(":")
            page = int(parts[1]) if len(parts) > 1 else 0
            await _show_my_alerts(query, page=page)
        elif data == "noop":
            pass  # Do nothing (page counter button)
        elif data.startswith("cancel_alert:"):
            parts = data.split(":")
            aid = int(parts[1])
            page = int(parts[2]) if len(parts) > 2 else 0
            ok = await db.deactivate_alert(aid, query.from_user.id)
            try:
                await query.answer(f"Алерт #{aid} отменён ✅" if ok else "Не удалось", show_alert=True)
            except Exception:
                pass
            await _show_my_alerts(query, page=page)
        elif data == "cancel_all_confirm":
            # Show confirmation
            count = await db.count_user_active_alerts(query.from_user.id)
            await query.edit_message_text(
                f"⚠️ <b>Отменить ВСЕ {count} алертов?</b>\n\nЭто действие нельзя отменить!",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Да, отменить все {count}", callback_data="cancel_all_yes"),
                     InlineKeyboardButton("← Назад", callback_data="myalerts:0")]
                ]))
        elif data == "cancel_all_yes":
            await db.clear_all_user_alerts(query.from_user.id)
            try:
                await query.answer("❌ Все алерты отменены!", show_alert=True)
            except Exception:
                pass
            await _show_my_alerts(query)
        elif data == "clear_all_confirm":
            count = await db.count_user_active_alerts(query.from_user.id)
            await query.edit_message_text(
                f"🗑 <b>Очистить все {count} алертов?</b>\n\nЭто действие нельзя отменить!",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Да, очистить все", callback_data="clear_all_yes"),
                     InlineKeyboardButton("← Назад", callback_data="myalerts:0")]
                ]))
        elif data == "clear_all_yes":
            await db.clear_all_user_alerts(query.from_user.id)
            try:
                await query.answer("🗑 Все алерты удалены!", show_alert=True)
            except Exception:
                pass
            await _show_my_alerts(query)
        elif data == "help":
            await _show_help(query)
        elif data.startswith("auth_approve:"):
            target_id = int(data.split(":")[1])
            if query.from_user.id in get_admin_ids():
                approve_user(target_id)
                name = pending_requests.pop(target_id, {}).get("name", str(target_id)) if isinstance(pending_requests.get(target_id), dict) else pending_requests.pop(target_id, str(target_id))
                await query.edit_message_text(f"✅ Пользователь {target_id} одобрен!")
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="✅ <b>Доступ одобрен!</b>\nНажми /start чтобы начать.",
                        parse_mode=ParseMode.HTML)
                except: pass
        elif data.startswith("auth_reject:"):
            target_id = int(data.split(":")[1])
            if query.from_user.id in get_admin_ids():
                reject_user(target_id)
                pending_requests.pop(target_id, None)
                await query.edit_message_text(f"❌ Пользователь {target_id} отклонён.")
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="❌ <b>Заявка отклонена.</b>\nОбратись к администратору.",
                        parse_mode=ParseMode.HTML)
                except: pass
    except Exception as e:
        logger.error("Callback error [%s]: %s", data, e, exc_info=True)
        try:
            await query.edit_message_text(f"❌ Ошибка: {e}\n\nПопробуй /start")
        except:
            pass


# ═══════════════════════════════════════════════════════
#  TEXT INPUT
# ═══════════════════════════════════════════════════════

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_alert")
    if not pending:
        await _show_main_menu(update.message)
        return

    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("❌ Формат: <code>> 8</code> или <code>>= 5 home</code>", parse_mode=ParseMode.HTML)
        return

    oper = parts[0]
    if oper not in SUPPORTED_OPERATORS:
        await update.message.reply_text("❌ Оператор: > < >= <= ==")
        return

    try:
        threshold = float(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Значение должно быть числом.")
        return

    team = parts[2].lower() if len(parts) >= 3 and parts[2].lower() in ("home", "away") else "total"
    sport, match_id = pending["sport"], pending["fixture_id"]
    kickoff_at = await _get_kickoff_for_alert(sport, match_id)
    match_label = await _get_match_label(sport, match_id)
    alert_id = await db.add_alert(
        user_id=update.effective_user.id, chat_id=update.effective_chat.id,
        fixture_id=match_id, stat_key=pending["stat_key"], operator=oper,
        threshold=threshold, team=team, sport=sport,
        kickoff_at=kickoff_at, match_label=match_label)
    context.user_data.pop("pending_alert", None)
    await update.message.reply_text(
        f"✅ <b>Алерт #{alert_id} создан!</b>\n📊 {pending['stat_key']} {oper} {threshold} ({team})\n{_scheduled_text(kickoff_at)}",
        parse_mode=ParseMode.HTML)


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 5:
        await update.message.reply_text(
            "📝 <code>/setalert football 12345 corners > 8</code>\nИли кнопки: /start",
            parse_mode=ParseMode.HTML)
        return

    sport = args[0].lower()
    if sport not in SPORTS:
        await update.message.reply_text("❌ Спорт: football или basketball")
        return

    try:
        fixture_id = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ ID — число.")
        return

    stat_key = args[2].lower()
    all_stats = {**FOOTBALL_STATS, **BASKETBALL_STATS}
    if stat_key not in all_stats:
        await update.message.reply_text(f"❌ Неизвестный тип: {stat_key}")
        return

    oper = args[3]
    if oper not in SUPPORTED_OPERATORS:
        await update.message.reply_text("❌ Оператор: > < >= <= ==")
        return

    try:
        threshold = float(args[4])
    except ValueError:
        await update.message.reply_text("❌ Значение — число.")
        return

    team = args[5].lower() if len(args) >= 6 and args[5].lower() in ("home", "away") else "total"
    kickoff_at = await _get_kickoff_for_alert(sport, fixture_id)
    match_label = await _get_match_label(sport, fixture_id)
    alert_id = await db.add_alert(
        user_id=update.effective_user.id, chat_id=update.effective_chat.id,
        fixture_id=fixture_id, stat_key=stat_key, operator=oper,
        threshold=threshold, team=team, sport=sport,
        kickoff_at=kickoff_at, match_label=match_label)
    await update.message.reply_text(
        f"✅ Алерт #{alert_id} ({sport} | {stat_key} {oper} {threshold})\n{_scheduled_text(kickoff_at)}",
        parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════
#  POLLING ENGINE (optimized)
# ═══════════════════════════════════════════════════════

FINISHED_TYPES = {"finished"}
BB_FINISHED_TYPES = {"finished"}


async def poll_and_check(context: ContextTypes.DEFAULT_TYPE):
    """Main polling loop. Optimized:
    - 1 request for ALL live football events (shared by all football alerts)
    - 1 request for ALL live basketball events (shared by all basketball alerts)
    - Statistics fetched only if needed, with cache
    """
    try:
        watched = await db.get_watched_by_sport()
        if not watched:
            return

        fb_ids = watched.get("football", set())
        if fb_ids:
            await _poll_football(context, fb_ids)

        bb_ids = watched.get("basketball", set())
        if bb_ids:
            await _poll_basketball(context, bb_ids)

        # Cleanup cache periodically
        api.cleanup_cache()

    except Exception as e:
        logger.error("Polling error: %s", e, exc_info=True)


async def _poll_football(context, watched_ids):
    logger.info("Polling %d football events via SofaScore...", len(watched_ids))

    # 1 request — get all live football events
    all_live = await api.football_live()
    live_index = {api.get_event_id(ev): ev for ev in all_live}

    for fid in watched_ids:
        ev = live_index.get(fid)

        if not ev:
            # Not in live list — check individually (uses cache)
            ev = await api.football_event(fid)
            if not ev:
                await db.deactivate_fixture_alerts(fid, "football")
                continue
            if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
                await db.deactivate_fixture_alerts(fid, "football")
                continue
            # Still not started — skip
            if api.is_not_started(ev):
                continue

        if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
            await db.deactivate_fixture_alerts(fid, "football")
            continue

        alerts = await db.get_active_alerts(fid, "football")
        if not alerts:
            continue

        # Check if we need detailed stats
        needs_stats = any(a["stat_key"] not in {"goals"} for a in alerts)
        stats = {}
        if needs_stats:
            stats = await api.football_statistics(fid)

        for alert in alerts:
            triggered, value = check_football_alert(alert, stats, ev)
            if triggered:
                msg = format_football_notification(alert, value, ev)
                try:
                    await context.bot.send_message(chat_id=alert["chat_id"], text=msg, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error("Send failed: %s", e)
                await db.mark_triggered(alert["id"], value, msg)


async def _poll_basketball(context, watched_ids):
    logger.info("Polling %d basketball events via SofaScore...", len(watched_ids))

    # 1 request — get all live basketball events
    all_live = await api.basketball_live()
    live_index = {api.get_event_id(ev): ev for ev in all_live}

    for gid in watched_ids:
        ev = live_index.get(gid)

        if not ev:
            ev = await api.basketball_event(gid)
            if not ev:
                await db.deactivate_fixture_alerts(gid, "basketball")
                continue
            if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
                await db.deactivate_fixture_alerts(gid, "basketball")
                continue
            if api.is_not_started(ev):
                continue

        if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
            await db.deactivate_fixture_alerts(gid, "basketball")
            continue

        alerts = await db.get_active_alerts(gid, "basketball")
        if not alerts:
            continue

        parsed = api.parse_basketball_scores(ev)

        for alert in alerts:
            triggered, value, extra = check_basketball_alert(alert, parsed)
            if triggered:
                msg = format_basketball_notification(alert, value, ev, extra)
                try:
                    await context.bot.send_message(chat_id=alert["chat_id"], text=msg, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error("Send failed: %s", e)
                await db.mark_triggered(alert["id"], value or 0, msg)


async def check_overdue_matches(context: ContextTypes.DEFAULT_TYPE):
    """Periodic check: auto-cleanup old alerts + warn about today's overdue.
    
    Strategy:
    1. Auto-deactivate alerts older than 24h (silently, no spam)
    2. For today's overdue: check if match started/canceled via API
    3. Group notifications by user+chat — ONE summary message, not per-alert
    4. Mark as notified so we never repeat
    """
    try:
        # Step 1: Auto-cleanup stale alerts (>24h old) — silent, no messages
        cleaned = await db.auto_cleanup_stale_alerts(max_age_hours=24)
        if cleaned:
            logger.info("Auto-cleaned %d stale alerts (kickoff >24h ago)", cleaned)

        # Step 2: Get today's overdue alerts that haven't been notified
        overdue = await db.get_overdue_alerts_not_notified(MATCH_START_TOLERANCE)
        if not overdue:
            return

        # Group by fixture to batch API checks
        by_fixture: dict[tuple[str, int], list[dict]] = {}
        for a in overdue:
            key = (a["sport"], a["fixture_id"])
            by_fixture.setdefault(key, []).append(a)

        # Track what to notify and what to deactivate
        canceled_ids = []  # alert IDs to deactivate (canceled/finished)
        canceled_matches = []  # (match_label, reason) for summary
        overdue_ids = []  # alert IDs that are overdue but still waiting
        overdue_matches = []  # match_labels for summary

        for (sport, fid), alerts in by_fixture.items():
            if sport == "football":
                ev = await api.football_event(fid)
            else:
                ev = await api.basketball_event(fid)

            if ev and (api.is_live(ev) or api.is_finished(ev)):
                # Match started or finished — mark notified, don't message
                for a in alerts:
                    canceled_ids.append(a["id"])
                if api.is_finished(ev):
                    await db.deactivate_fixture_alerts(fid, sport)
                continue

            if ev and api.is_canceled_or_postponed(ev):
                label = alerts[0].get("match_label", str(fid))
                canceled_matches.append(label)
                for a in alerts:
                    canceled_ids.append(a["id"])
                await db.deactivate_fixture_alerts(fid, sport)
                continue

            # Still not started — overdue
            label = alerts[0].get("match_label", str(fid))
            overdue_matches.append(label)
            for a in alerts:
                overdue_ids.append(a["id"])

        # Mark all as notified (so we don't repeat)
        await db.mark_overdue_notified(canceled_ids + overdue_ids)

        # Step 3: Send ONE grouped summary per user+chat
        all_affected = overdue + [a for alerts_list in by_fixture.values() for a in alerts_list]
        by_chat: dict[int, int] = {}  # chat_id -> user_id (dedup)
        for a in overdue:
            by_chat[a["chat_id"]] = a["user_id"]

        if not canceled_matches and not overdue_matches:
            return

        for chat_id in by_chat:
            lines = []
            if canceled_matches:
                lines.append(f"❌ <b>Отменено/перенесено ({len(canceled_matches)}):</b>")
                for m in canceled_matches[:10]:
                    lines.append(f"  • {m}")
                if len(canceled_matches) > 10:
                    lines.append(f"  ... и ещё {len(canceled_matches) - 10}")
                lines.append("Алерты деактивированы.")
                lines.append("")

            if overdue_matches:
                lines.append(f"⚠️ <b>Задерживаются ({len(overdue_matches)}):</b>")
                for m in overdue_matches[:10]:
                    lines.append(f"  • {m}")
                if len(overdue_matches) > 10:
                    lines.append(f"  ... и ещё {len(overdue_matches) - 10}")
                lines.append("Алерты пока активны, жду начала.")

            if lines:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="\n".join(lines),
                        parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error("Failed to send overdue summary to %s: %s", chat_id, e)

    except Exception as e:
        logger.error("Overdue check error: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════

async def post_init(app: Application):
    await db.init_db()
    logger.info("Database initialized")
    # Auto-cleanup stale alerts on startup (>24h old) — no spam
    cleaned = await db.auto_cleanup_stale_alerts(max_age_hours=24)
    if cleaned:
        logger.info("Startup cleanup: deactivated %d stale alerts", cleaned)
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("setalert", "Создать алерт вручную"),
        BotCommand("help", "Помощь"),
    ])


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Set TELEGRAM_BOT_TOKEN in .env!")
        return

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .get_updates_connect_timeout(60)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Main polling job
    app.job_queue.run_repeating(
        poll_and_check,
        interval=POLL_INTERVAL_SECONDS,
        first=10,
        name="poll_live_stats")

    # Check for overdue matches every 10 minutes
    app.job_queue.run_repeating(
        check_overdue_matches,
        interval=600,
        first=60,
        name="check_overdue")

    logger.info("Polling every %d sec via SofaScore", POLL_INTERVAL_SECONDS)
    logger.info("Starting Sports Alerts Bot v2 (SofaScore)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()
