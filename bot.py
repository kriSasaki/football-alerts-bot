"""
🏟 Sports Alerts Bot v3 — SofaScore + Fonbet Edition
Features:
  - Два источника данных: SofaScore и Fonbet (выбор в главном меню)
  - Одновременная работа обоих источников — алерты работают независимо
  - Просмотр матчей и создание алертов для каждого источника отдельно
  - В уведомлениях указывается источник данных
  - Фильтр киберспорта для Fonbet
  - Все возможности v2 сохранены для SofaScore
"""
import logging
import logging.handlers
import asyncio
import time
import os
import sys
import html
import httpx
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.error import TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN, POLL_INTERVAL_SECONDS,
    FOOTBALL_STATS, BASKETBALL_STATS,
    SUPPORTED_OPERATORS, SPORTS, MATCH_START_TOLERANCE,
    KICKOFF_LOOKAROUND_SECONDS,
    TELEGRAM_ENABLED, ALLOW_WEB_ONLY_FALLBACK,
    TELEGRAM_PROXY, TELEGRAM_BASE_URL, APP_NAME,
)
from auth import is_authorized, pending_requests, approve_user, reject_user, get_admin_ids, add_pending_request
import database as db
import sports_api as api
import fonbet_api as fb
from webapp import start_web_app, stop_web_app, get_runtime_web_url
from webpush import send_user_push
from web_auth import issue_web_token
from alert_engine import (
    check_football_alert, check_basketball_alert,
    format_football_notification, format_basketball_notification,
    format_alert_summary, format_live_alert_status,
)

# ═══════════════════════════════════════════════════════
#  LOGGING SETUP
# ═══════════════════════════════════════════════════════

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_log_format = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
_console_h = logging.StreamHandler(sys.stdout)
_console_h.setFormatter(_log_format)
_console_h.setLevel(logging.INFO)

_file_h = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
_file_h.setFormatter(_log_format)
_file_h.setLevel(logging.DEBUG)

_err_h = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "errors.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_err_h.setFormatter(_log_format)
_err_h.setLevel(logging.WARNING)

logging.basicConfig(level=logging.DEBUG, handlers=[_console_h, _file_h, _err_h])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.INFO)

logger = logging.getLogger(__name__)
_poll_lock = asyncio.Lock()
_web_runner = None
_schedule_prefetch_inflight: set[tuple[str, str]] = set()

# Источники данных
SOURCE_SOFASCORE = "sofascore"
SOURCE_FONBET = "fonbet"
SOURCE_BOTH = "both"

SOURCE_LABELS = {
    SOURCE_SOFASCORE: "SofaScore 📊",
    SOURCE_FONBET: "Fonbet 🎰",
    SOURCE_BOTH: "Оба источника 🔀",
}

SOURCE_EMOJI = {
    SOURCE_SOFASCORE: "📊",
    SOURCE_FONBET: "🎰",
    SOURCE_BOTH: "🔀",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _plain_from_html(text: str) -> str:
    plain = text
    for old, new in (
        ("<b>", ""), ("</b>", ""), ("<i>", ""), ("</i>", ""),
        ("<code>", ""), ("</code>", ""),
        ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&quot;", '"'),
    ):
        plain = plain.replace(old, new)
    return plain


async def _safe_edit(query, text: str, keyboard=None):
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as e:
        err_str = str(e)
        if "Message is not modified" in err_str:
            pass
        elif "Timed out" in err_str:
            try:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                pass
        elif "Can't parse entities" in err_str:
            plain = _plain_from_html(text)
            try:
                await query.edit_message_text(plain, reply_markup=keyboard)
            except Exception:
                await query.message.reply_text(plain, reply_markup=keyboard)
        else:
            raise


DAY_NAMES = {
    0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
    4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}


# ═══════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        if user_id in pending_requests:
            await update.message.reply_text(
                "⏳ <b>Твоя заявка на рассмотрении.</b>\nАдмин скоро проверит.",
                parse_mode=ParseMode.HTML)
            return
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
                        f"👤 {_esc(name)} ({_esc(username)})\n"
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
            "🔐 <b>Доступ ограничен</b>\n\nЗаявка отправлена администратору.",
            parse_mode=ParseMode.HTML)
        return
    await _show_main_menu(update.message, user_id=update.effective_user.id)


async def _show_main_menu(target, edit=False, user_id=None):
    alert_count = await db.count_user_active_alerts(user_id) if user_id else 0
    settings = await db.get_user_settings(user_id) if user_id else {}
    source = settings.get("data_source", SOURCE_SOFASCORE)
    source_label = SOURCE_LABELS.get(source, "SofaScore 📊")

    text = (
        f"🏟 <b>{APP_NAME}</b>\n"
        f"<i>Персональные уведомления по матчам</i>\n\n"
        f"📡 Источник: <b>{source_label}</b>\n\n"
        f"Выбери спорт:"
    )
    buttons = [
        [InlineKeyboardButton(f"{v['emoji']} {v['label']}", callback_data=f"sport:{k}")]
        for k, v in SPORTS.items()
    ]
    alert_label = f"📋 Мои алерты ({alert_count})" if alert_count else "📋 Мои алерты"
    buttons.append([InlineKeyboardButton(alert_label, callback_data="myalerts:0")])
    buttons.append([InlineKeyboardButton("🌐 Веб-панель", callback_data="web_link")])

    # Кнопка выбора источника
    buttons.append([InlineKeyboardButton(f"📡 Источник: {source_label}", callback_data="source_menu")])

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


# ═══════════════════════════════════════════════════════
#  SOURCE SELECTION
# ═══════════════════════════════════════════════════════

async def _show_source_menu(query, user_id: int):
    settings = await db.get_user_settings(user_id)
    current = settings.get("data_source", SOURCE_SOFASCORE)

    text = (
        "📡 <b>Выбор источника данных</b>\n\n"
        "<b>SofaScore 📊</b> — широкое покрытие, детальная статистика (угловые, удары, владение). "
        "Не все матчи Fonbet есть здесь.\n\n"
        "<b>Fonbet 🎰</b> — все матчи букмекера, счёт по четвертям в реальном времени. "
        "Нет детальной статистики (угловые и т.д.).\n\n"
        "<b>Оба источника 🔀</b> — отображаются матчи из обоих источников, "
        "алерты работают независимо, в уведомлениях указывается источник.\n\n"
        f"Сейчас выбрано: <b>{SOURCE_LABELS.get(current)}</b>"
    )

    def _mark(src):
        return "✅ " if current == src else ""

    buttons = [
        [InlineKeyboardButton(f"{_mark(SOURCE_SOFASCORE)}SofaScore 📊", callback_data=f"set_source:{SOURCE_SOFASCORE}")],
        [InlineKeyboardButton(f"{_mark(SOURCE_FONBET)}Fonbet 🎰", callback_data=f"set_source:{SOURCE_FONBET}")],
        [InlineKeyboardButton(f"{_mark(SOURCE_BOTH)}Оба источника 🔀", callback_data=f"set_source:{SOURCE_BOTH}")],
        [InlineKeyboardButton("« Главная", callback_data="main_menu")],
    ]
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def _handle_set_source(query, user_id: int, source: str):
    await db.set_user_setting(user_id, "data_source", source)
    label = SOURCE_LABELS.get(source, source)
    await query.answer(f"Источник изменён: {label}", show_alert=False)
    await _show_main_menu(query, edit=True, user_id=user_id)


# ═══════════════════════════════════════════════════════
#  SPORT MENU
# ═══════════════════════════════════════════════════════

async def _show_sport_menu(query, sport: str):
    user_id = query.from_user.id
    settings = await db.get_user_settings(user_id)
    source = settings.get("data_source", SOURCE_SOFASCORE)
    info = SPORTS[sport]
    source_label = SOURCE_LABELS.get(source, "")

    text = (
        f"{info['emoji']} <b>{info['label']}</b>\n"
        f"📡 {source_label}\n\n"
        f"Что хочешь сделать?"
    )
    buttons = []

    if source in (SOURCE_SOFASCORE, SOURCE_BOTH):
        buttons.append([InlineKeyboardButton(
            "📡 Live матчи (SofaScore)", callback_data=f"live:sofascore:{sport}")])
        buttons.append([InlineKeyboardButton(
            "📅 По дням (SofaScore)", callback_data=f"day:{sport}:0")])

    if source in (SOURCE_FONBET, SOURCE_BOTH):
        buttons.append([InlineKeyboardButton(
            "🎰 Live матчи (Fonbet)", callback_data=f"live:fonbet:{sport}")])
        buttons.append([InlineKeyboardButton(
            "🎰 Запланированные (Fonbet)", callback_data=f"fb_sched:{sport}")])

    buttons.append([InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts:0")])
    buttons.append([InlineKeyboardButton("« Назад", callback_data="main_menu")])
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  SOFASCORE — DAY BROWSER (без изменений)
# ═══════════════════════════════════════════════════════

async def _show_day(query, sport: str, day_offset: int):
    target_date = datetime.now() + timedelta(days=day_offset)
    date_str = target_date.strftime("%Y-%m-%d")
    day_name = DAY_NAMES.get(target_date.weekday(), "")
    date_display = target_date.strftime("%d.%m.%Y")

    if sport == "football":
        events = await api.football_by_date(date_str)
    else:
        events = await api.basketball_by_date(date_str)
    _schedule_prefetch_window(sport, day_offset)

    if not events:
        text = f"📅 <b>{day_name}, {date_display}</b>\n📊 SofaScore\n\nНет матчей на этот день."
        nav = _day_nav_buttons(sport, day_offset)
        nav.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(nav))
        return

    live_ev = [e for e in events if api.is_live(e)]
    sched_ev = [e for e in events if api.is_not_started(e)]
    fin_ev = [e for e in events if api.is_finished(e)]

    emoji = SPORTS[sport]["emoji"]
    lines = [f"📅 <b>{day_name}, {date_display}</b>", f"📊 <i>SofaScore</i>",
             f"{emoji} Всего: <b>{len(events)}</b>\n"]

    match_buttons = []

    if live_ev:
        lines.append(f"🔴 <b>Сейчас в игре: {len(live_ev)}</b>")
        for ev in live_ev[:15]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_football_list_item(ev))}")
            else:
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_basketball_list_item(ev))}")
            _add_match_btn(match_buttons, ev, sport, "sofascore")
        lines.append("")

    if sched_ev:
        lines.append(f"⏰ <b>Скоро: {len(sched_ev)}</b>")
        sched_ev.sort(key=lambda e: api.get_kickoff_timestamp(e))
        for ev in sched_ev[:20]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_football_list_item(ev))}")
            else:
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_basketball_list_item(ev))}")
            _add_match_btn(match_buttons, ev, sport, "sofascore")
        lines.append("")

    if fin_ev:
        lines.append(f"✅ <b>Завершены: {len(fin_ev)}</b>")
        for ev in fin_ev[:10]:
            eid = api.get_event_id(ev)
            if sport == "football":
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_football_list_item(ev))}")
            else:
                lines.append(f"  <code>{eid}</code> | {_esc(api.format_basketball_list_item(ev))}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3500] + "\n\n... (список обрезан)"

    buttons = []
    buttons.extend(match_buttons[:10])

    if sched_ev or live_ev:
        bulk_targets = sched_ev + live_ev
        if sport == "basketball" and bulk_targets:
            buttons.append([InlineKeyboardButton(
                f"🎲 Q1Q2 ЧЁТ на ВСЕ ({len(bulk_targets)})",
                callback_data=f"bulk:sofascore:{sport}:{date_str}:q1q2_even")])
        if bulk_targets:
            buttons.append([InlineKeyboardButton(
                f"🔔 Алерт на ВСЕ матчи ({len(bulk_targets)})",
                callback_data=f"bulk_select:sofascore:{sport}:{date_str}")])

    buttons.extend(_day_nav_buttons(sport, day_offset))
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


def _day_nav_buttons(sport, day_offset):
    nav_row = []
    if day_offset > -7:
        nav_row.append(InlineKeyboardButton("◀ Пред.", callback_data=f"day:{sport}:{day_offset - 1}"))
    if day_offset != 0:
        nav_row.append(InlineKeyboardButton("📍 Сегодня", callback_data=f"day:{sport}:0"))
    if day_offset < 7:
        nav_row.append(InlineKeyboardButton("След. ▶", callback_data=f"day:{sport}:{day_offset + 1}"))
    return [nav_row] if nav_row else []


def _add_match_btn(buttons, ev, sport, source, get_id_fn=None, get_home_fn=None, get_away_fn=None):
    if get_id_fn is None:
        get_id_fn = api.get_event_id if source == "sofascore" else fb.get_event_id
        get_home_fn = api.get_home_name if source == "sofascore" else fb.get_home_name
        get_away_fn = api.get_away_name if source == "sofascore" else fb.get_away_name
    eid = get_id_fn(ev)
    home = get_home_fn(ev)[:10]
    away = get_away_fn(ev)[:10]
    emoji = "⚽" if sport == "football" else "🏀"
    src_emoji = "📊" if source == "sofascore" else "🎰"
    btn = InlineKeyboardButton(
        f"{src_emoji}{emoji} {home}-{away}",
        callback_data=f"match:{source}:{sport}:{eid}")
    if buttons and isinstance(buttons[-1], list) and len(buttons[-1]) == 1:
        buttons[-1].append(btn)
    else:
        buttons.append([btn])


# ═══════════════════════════════════════════════════════
#  SOFASCORE — LIVE LIST
# ═══════════════════════════════════════════════════════

async def _show_live(query, source: str, sport: str):
    if source == "sofascore":
        await _show_sofascore_live(query, sport)
    else:
        await _show_fonbet_live(query, sport)


async def _show_sofascore_live(query, sport: str):
    if sport == "football":
        events = await api.football_live()
    else:
        events = await api.basketball_live()

    if not events:
        await query.edit_message_text(
            f"📊 SofaScore\nСейчас нет live-матчей ⏳",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 По дням", callback_data=f"day:{sport}:0")],
                [InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
            ]))
        return

    emoji = SPORTS[sport]["emoji"]
    lines = [f"⚡ <b>SofaScore 📊 Live {SPORTS[sport]['label']}: {len(events)}</b>\n"]
    buttons = []

    if sport == "football":
        tournaments = {}
        for ev in events:
            t_name = api.get_tournament_name(ev)
            tournaments.setdefault(t_name, []).append(ev)
        shown = 0
        for t_name in sorted(tournaments.keys()):
            if shown >= 20:
                break
            lines.append(f"\n🏆 <b>{_esc(t_name)}</b>")
            for ev in tournaments[t_name][:5]:
                lines.append(f"  <code>{api.get_event_id(ev)}</code> | {_esc(api.format_football_list_item(ev))}")
                shown += 1
    else:
        for ev in events[:15]:
            lines.append(f"  <code>{api.get_event_id(ev)}</code> | {_esc(api.format_basketball_list_item(ev))}")

    lines.append("\n👇 Выбери матч:")
    row = []
    for ev in events[:10]:
        home = api.get_home_name(ev)[:8]
        away = api.get_away_name(ev)[:8]
        row.append(InlineKeyboardButton(
            f"📊{emoji} {home}-{away}",
            callback_data=f"match:sofascore:{sport}:{api.get_event_id(ev)}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if sport == "basketball":
        buttons.append([InlineKeyboardButton(
            f"🎲 Q1Q2 ЧЁТ на ВСЕ live ({len(events)})",
            callback_data=f"bulk_live:sofascore:{sport}:q1q2_even")])

    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n..."
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  FONBET — LIVE LIST
# ═══════════════════════════════════════════════════════

async def _show_fonbet_live(query, sport: str):
    user_id = query.from_user.id
    settings = await db.get_user_settings(user_id)
    hide_esports = settings.get("hide_esports", 1)

    if sport == "football":
        events = await fb.football_live(include_esports=not hide_esports)
    else:
        events = await fb.basketball_live(include_esports=not hide_esports)

    if not events:
        await query.edit_message_text(
            f"🎰 Fonbet\nСейчас нет live-матчей ⏳\n\n"
            f"Попробуй 'Запланированные' чтобы увидеть предстоящие матчи.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎰 Запланированные", callback_data=f"fb_sched:{sport}")],
                [InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
            ]))
        return

    emoji = SPORTS[sport]["emoji"]
    lines = [f"⚡ <b>Fonbet 🎰 Live {SPORTS[sport]['label']}: {len(events)}</b>\n"]

    # Группируем по лиге
    leagues: dict[str, list] = {}
    for ev in events:
        league = fb.get_league_name(ev) or "—"
        leagues.setdefault(league, []).append(ev)

    shown = 0
    for league_name, evs in sorted(leagues.items()):
        if shown >= 25:
            break
        lines.append(f"\n🏆 <b>{_esc(league_name)}</b>")
        for ev in evs[:5]:
            lines.append(f"  <code>{fb.get_event_id(ev)}</code> | {_esc(fb.format_list_item(ev))}")
            shown += 1

    lines.append("\n👇 Выбери матч:")

    buttons = []
    row = []
    for ev in events[:10]:
        home = fb.get_home_name(ev)[:8]
        away = fb.get_away_name(ev)[:8]
        row.append(InlineKeyboardButton(
            f"🎰{emoji} {home}-{away}",
            callback_data=f"match:fonbet:{sport}:{fb.get_event_id(ev)}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if sport == "basketball":
        buttons.append([InlineKeyboardButton(
            f"🎲 Q1Q2 ЧЁТ на ВСЕ Fonbet live ({len(events)})",
            callback_data=f"bulk_live:fonbet:{sport}:q1q2_even")])

    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n..."
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  FONBET — SCHEDULED LIST
# ═══════════════════════════════════════════════════════

async def _show_fonbet_scheduled(query, sport: str):
    user_id = query.from_user.id
    settings = await db.get_user_settings(user_id)
    hide_esports = settings.get("hide_esports", 1)

    if sport == "football":
        events = await fb.football_scheduled(include_esports=not hide_esports)
    else:
        events = await fb.basketball_scheduled(include_esports=not hide_esports)

    if not events:
        await query.edit_message_text(
            f"🎰 Fonbet — Запланированные {SPORTS[sport]['label']}\n\nНет матчей.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")]]))
        return

    emoji = SPORTS[sport]["emoji"]
    lines = [f"📅 <b>Fonbet 🎰 {SPORTS[sport]['label']}: {len(events)} матчей</b>\n"]

    leagues: dict[str, list] = {}
    for ev in events:
        league = fb.get_league_name(ev) or "—"
        leagues.setdefault(league, []).append(ev)

    shown = 0
    for league_name, evs in sorted(leagues.items()):
        if shown >= 30:
            break
        lines.append(f"\n🏆 <b>{_esc(league_name)}</b>")
        for ev in evs[:6]:
            lines.append(f"  <code>{fb.get_event_id(ev)}</code> | {_esc(fb.format_list_item(ev))}")
            shown += 1

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n... (список обрезан)"

    buttons = []
    row = []
    for ev in events[:10]:
        home = fb.get_home_name(ev)[:8]
        away = fb.get_away_name(ev)[:8]
        row.append(InlineKeyboardButton(
            f"🎰{emoji} {home}-{away}",
            callback_data=f"match:fonbet:{sport}:{fb.get_event_id(ev)}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if events:
        buttons.append([InlineKeyboardButton(
            f"🔔 Алерт на ВСЕ ({len(events)})",
            callback_data=f"bulk_select:fonbet:{sport}:sched")])

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"fb_sched:{sport}")])
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  MATCH DETAIL
# ═══════════════════════════════════════════════════════

async def _show_match(query, source: str, sport: str, match_id: int):
    if source == "sofascore":
        await _show_sofascore_match(query, sport, match_id)
    else:
        await _show_fonbet_match(query, sport, match_id)


async def _show_sofascore_match(query, sport: str, match_id: int):
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

    text = text + "\n\n📊 <i>Источник: SofaScore</i>"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Создать алерт", callback_data=f"alert_type:sofascore:{sport}:{match_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"match:sofascore:{sport}:{match_id}"),
         InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
    ])
    if len(text) > 4000:
        text = text[:4000]
    await _safe_edit(query, text, keyboard)


async def _show_fonbet_match(query, sport: str, match_id: int):
    ev = await fb.get_event(match_id)
    if not ev:
        await query.edit_message_text(
            "❌ Матч не найден в Fonbet.\n\nВозможно, матч уже завершён или недоступен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")]]))
        return

    text = fb.format_detail(ev, sport)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Создать алерт", callback_data=f"alert_type:fonbet:{sport}:{match_id}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"match:fonbet:{sport}:{match_id}"),
         InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")],
    ])
    if len(text) > 4000:
        text = text[:4000]
    await _safe_edit(query, text, keyboard)


def _format_football_detail(ev, stats):
    lines = [f"⚽ <b>{_esc(api.format_football_short(ev))}</b>"]
    t_name = api.get_tournament_name(ev)
    if t_name:
        lines.append(f"🏆 {_esc(t_name)}")
    if api.is_not_started(ev):
        ts = api.get_kickoff_timestamp(ev)
        if ts:
            lines.append(f"\n⏰ <b>Начало:</b> {datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}")
        lines.append("\n💡 Можешь поставить алерт заранее!")
    if stats and (stats.get("home") or stats.get("away")):
        lines.append("\n📊 <b>Статистика:</b>")
        stat_display = [
            ("ballPossession", "Владение"), ("totalShots", "Удары"),
            ("shotsOnTarget", "В створ"), ("cornerKicks", "Угловые"),
            ("fouls", "Фолы"), ("yellowCards", "Жёлтые"),
            ("redCards", "Красные"), ("offsides", "Офсайды"),
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

async def _show_alert_type_selector(query, source: str, sport: str, match_id: int):
    stats_dict = FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS

    # Для Fonbet скрываем статистику которой там нет
    if source == "fonbet" and sport == "football":
        # У Fonbet нет детальной стат. футбола — только счёт
        stats_dict = {"goals": FOOTBALL_STATS["goals"]}

    emoji = "⚽" if sport == "football" else "🏀"
    src_emoji = SOURCE_EMOJI.get(source, "")
    buttons = []
    row = []
    for key, info in stats_dict.items():
        row.append(InlineKeyboardButton(
            f"{info['emoji']} {info['label']}",
            callback_data=f"alert_stat:{source}:{sport}:{match_id}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"match:{source}:{sport}:{match_id}")])
    await query.edit_message_text(
        f"{src_emoji}{emoji} Матч <code>{match_id}</code>\n"
        f"📡 {SOURCE_LABELS.get(source, source)}\n\n"
        f"📊 <b>Выбери тип алерта:</b>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_alert_stat_selected(query, context, source: str, sport: str, match_id: int, stat_key: str):
    stat_info = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {})
    label = stat_info.get("label", stat_key)

    if stat_key in ("q1_even", "q2_even", "q1q2_even"):
        match_meta = await _get_match_meta(source, sport, match_id)
        alert_id = await db.add_alert(
            user_id=query.from_user.id, chat_id=query.message.chat_id,
            fixture_id=match_id, stat_key=stat_key, operator="==", threshold=1,
            team="total", sport=sport, source=source,
            kickoff_at=match_meta["kickoff_at"], match_label=match_meta["match_label"])
        src_label = SOURCE_LABELS.get(source, source)
        await query.edit_message_text(
            f"✅ <b>Алерт #{alert_id} создан!</b>\n\n"
            f"📡 {src_label}\n"
            f"🏀 {_esc(match_meta['match_label'])}\n"
            f"📊 Тип: <b>{_esc(label)}</b>\n"
            f"{_scheduled_text(match_meta['kickoff_at'])}",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ещё алерт", callback_data=f"alert_type:{source}:{sport}:{match_id}")],
                [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
                [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))
        return

    context.user_data["pending_alert"] = {
        "source": source, "sport": sport, "fixture_id": match_id, "stat_key": stat_key}
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
        row.append(InlineKeyboardButton(
            p, callback_data=f"alert_quick:{source}:{sport}:{match_id}:{stat_key}:{p}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"alert_type:{source}:{sport}:{match_id}")])
    src_label = SOURCE_LABELS.get(source, source)
    await query.edit_message_text(
        f"📡 {src_label}\n📊 <b>{_esc(label)}</b> | Матч {match_id}\n\n"
        f"Выбери условие или напиши своё:\n<code>> 8</code> или <code>>= 5 home</code>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_quick_alert(query, source: str, sport: str, match_id: int, stat_key: str, condition: str):
    parts = condition.split()
    oper, threshold = parts[0], float(parts[1])
    match_meta = await _get_match_meta(source, sport, match_id)
    alert_id = await db.add_alert(
        user_id=query.from_user.id, chat_id=query.message.chat_id,
        fixture_id=match_id, stat_key=stat_key, operator=oper, threshold=threshold,
        team="total", sport=sport, source=source,
        kickoff_at=match_meta["kickoff_at"], match_label=match_meta["match_label"])
    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    src_label = SOURCE_LABELS.get(source, source)
    await query.edit_message_text(
        f"✅ <b>Алерт #{alert_id} создан!</b>\n\n"
        f"📡 {src_label}\n"
        f"{SPORTS[sport]['emoji']} {_esc(match_meta['match_label'])}\n"
        f"📊 {_esc(stat_label)} {oper} {threshold}\n"
        f"{_scheduled_text(match_meta['kickoff_at'])}",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ещё алерт", callback_data=f"alert_type:{source}:{sport}:{match_id}")],
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


# ═══════════════════════════════════════════════════════
#  BULK ALERTS
# ═══════════════════════════════════════════════════════

async def _handle_bulk_alert(query, source: str, sport: str, date_or_tag: str, stat_key: str):
    """Создать алерт на ВСЕ матчи (из расписания или дня SofaScore)."""
    if source == "sofascore":
        if sport == "football":
            events = await api.football_by_date(date_or_tag)
        else:
            events = await api.basketball_by_date(date_or_tag)
        targets = [e for e in events if api.is_not_started(e) or api.is_live(e)]
        matches = [{"fixture_id": api.get_event_id(e),
                    "kickoff_at": api.get_kickoff_timestamp(e),
                    "match_label": api.get_match_label(e)} for e in targets]
    else:  # fonbet
        user_id = query.from_user.id
        settings = await db.get_user_settings(user_id)
        hide_esports = settings.get("hide_esports", 1)
        if sport == "basketball":
            live_evs = await fb.basketball_live(include_esports=not hide_esports)
            sched_evs = await fb.basketball_scheduled(include_esports=not hide_esports)
        else:
            live_evs = await fb.football_live(include_esports=not hide_esports)
            sched_evs = await fb.football_scheduled(include_esports=not hide_esports)
        targets = live_evs + sched_evs
        matches = [{"fixture_id": fb.get_event_id(e),
                    "kickoff_at": fb.get_kickoff_timestamp(e),
                    "match_label": fb.get_match_label(e)} for e in targets]

    if not matches:
        await _safe_edit(query, "Нет доступных матчей для алертов.")
        return

    if stat_key in ("q1_even", "q2_even", "q1q2_even"):
        ids = await db.add_alerts_bulk(
            user_id=query.from_user.id, chat_id=query.message.chat_id,
            sport=sport, source=source, matches=matches,
            stat_key=stat_key, operator="==", threshold=1)
    else:
        await _show_bulk_stat_selector(query, source, sport, date_or_tag)
        return

    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    src_label = SOURCE_LABELS.get(source, source)
    await _safe_edit(
        query,
        f"✅ <b>Создано {len(ids)} алертов!</b>\n\n"
        f"📡 {src_label}\n"
        f"📊 {stat_label} на все матчи\n"
        f"🆔 #{ids[0]}—#{ids[-1]}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


async def _handle_bulk_live(query, source: str, sport: str, stat_key: str):
    """Алерт на все текущие live-матчи."""
    if source == "sofascore":
        if sport == "football":
            events = await api.football_live()
        else:
            events = await api.basketball_live()
        matches = [{"fixture_id": api.get_event_id(e), "kickoff_at": 0,
                    "match_label": api.get_match_label(e)} for e in events]
    else:
        user_id = query.from_user.id
        settings = await db.get_user_settings(user_id)
        hide = settings.get("hide_esports", 1)
        if sport == "basketball":
            events = await fb.basketball_live(include_esports=not hide)
        else:
            events = await fb.football_live(include_esports=not hide)
        matches = [{"fixture_id": fb.get_event_id(e), "kickoff_at": 0,
                    "match_label": fb.get_match_label(e)} for e in events]

    if not matches:
        await _safe_edit(query, "Нет live-матчей.")
        return

    ids = await db.add_alerts_bulk(
        user_id=query.from_user.id, chat_id=query.message.chat_id,
        sport=sport, source=source, matches=matches,
        stat_key=stat_key, operator="==", threshold=1)

    stat_label = (FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS).get(stat_key, {}).get("label", stat_key)
    src_label = SOURCE_LABELS.get(source, source)
    await _safe_edit(
        query,
        f"✅ <b>Создано {len(ids)} алертов!</b>\n\n"
        f"📡 {src_label}\n"
        f"📊 {stat_label} на все live-матчи\n"
        f"🆔 #{ids[0]}—#{ids[-1]}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои алерты", callback_data="myalerts")],
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


async def _show_bulk_stat_selector(query, source: str, sport: str, date_or_tag: str):
    stats_dict = FOOTBALL_STATS if sport == "football" else BASKETBALL_STATS
    if source == "fonbet" and sport == "football":
        stats_dict = {"goals": FOOTBALL_STATS["goals"]}
    buttons = []
    row = []
    for key, info in stats_dict.items():
        row.append(InlineKeyboardButton(
            f"{info['emoji']} {info['label']}",
            callback_data=f"bulk:{source}:{sport}:{date_or_tag}:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« Назад", callback_data=f"sport:{sport}")])
    src_label = SOURCE_LABELS.get(source, source)
    await query.edit_message_text(
        f"📡 {src_label}\n📊 <b>Выбери тип алерта для ВСЕХ матчей:</b>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


# ═══════════════════════════════════════════════════════
#  MY ALERTS
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
    if total > 1:
        buttons.append([
            InlineKeyboardButton(f"❌ Отменить ВСЕ ({total})", callback_data="cancel_all_confirm"),
            InlineKeyboardButton("🗑 Очистить", callback_data="clear_all_confirm"),
        ])

    for a in page_alerts:
        source = a.get("source", "sofascore")
        src_emoji = SOURCE_EMOJI.get(source, "📊")
        summary = format_alert_summary(a)
        if a.get("match_label"):
            summary = f"{src_emoji} {SPORTS.get(a['sport'], {}).get('emoji', '🏟')} {_esc(a['match_label'])}\n{_esc(summary)}"
        else:
            summary = f"{src_emoji} {_esc(summary)}"

        if a.get("kickoff_at", 0) > now:
            summary += f"\n   ⏰ {datetime.fromtimestamp(a['kickoff_at']).strftime('%d.%m %H:%M')}"
        else:
            summary += "\n   ⚡ Мониторинг активен"

        lines.append(summary)
        lines.append("")
        buttons.append([
            InlineKeyboardButton(f"📊 #{a['id']}", callback_data=f"alert_status:{a['id']}"),
            InlineKeyboardButton(f"❌ #{a['id']}", callback_data=f"cancel_alert:{a['id']}:{page}"),
        ])

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
        if "Message is not modified" not in str(e):
            raise


async def _show_alert_status(query, alert_id: int):
    alert = await db.get_alert_by_id(alert_id)
    if not alert or not alert["active"]:
        await query.answer("Алерт не найден или неактивен", show_alert=True)
        return

    sport = alert["sport"]
    source = alert.get("source", "sofascore")
    fid = alert["fixture_id"]
    src_label = SOURCE_LABELS.get(source, source)

    try:
        if source == "fonbet":
            ev = await fb.get_event(fid)
            if ev:
                parsed = fb.parse_basketball_scores(ev)
                from alert_engine import check_basketball_alert
                _, value, extra = check_basketball_alert(alert, parsed)
                status_line = f"{value} — {extra}" if value is not None else (extra or "нет данных")
                match_info = f"{_esc(fb.get_home_name(ev))} {parsed.get('home_total',0)}:{parsed.get('away_total',0)} {_esc(fb.get_away_name(ev))}"
            else:
                status_line = "❌ матч не найден в Fonbet"
                match_info = f"ID: {fid}"
        else:
            if sport == "football":
                ev = await api.football_event(fid)
                if ev:
                    stats = {}
                    if api.is_live(ev):
                        stats = await api.football_statistics(fid)
                    status_line = format_live_alert_status(alert, stats=stats, event=ev)
                    match_info = _esc(api.format_football_short(ev))
                else:
                    status_line = "❌ матч не найден"
                    match_info = f"ID: {fid}"
            else:
                ev = await api.basketball_event(fid)
                if ev:
                    parsed = api.parse_basketball_scores(ev)
                    status_line = format_live_alert_status(alert, parsed_bb=parsed)
                    match_info = f"{_esc(api.get_home_name(ev))} {parsed['home_total'] or 0}:{parsed['away_total'] or 0} {_esc(api.get_away_name(ev))}"
                else:
                    status_line = "❌ матч не найден"
                    match_info = f"ID: {fid}"
    except Exception as e:
        logger.error("Alert status error: %s", e)
        status_line = f"❌ ошибка: {e}"
        match_info = f"ID: {fid}"

    stat_label = _esc(format_alert_summary(alert))
    text = (
        f"📊 <b>Статус алерта #{alert_id}</b>\n"
        f"📡 {src_label}\n\n"
        f"{SPORTS.get(sport, {}).get('emoji', '')} {match_info}\n"
        f"{stat_label}\n\n"
        f"<b>Текущее значение:</b> {_esc(str(status_line))}"
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
        "<b>1.</b> В главном меню выбери <b>источник данных</b>:\n"
        "   📊 SofaScore — детальная статистика\n"
        "   🎰 Fonbet — все матчи букмекера\n"
        "   🔀 Оба — независимая работа\n\n"
        "<b>2.</b> Выбери спорт → Live или расписание\n"
        "<b>3.</b> Нажми на матч → выбери тип алерта\n"
        "<b>4.</b> Получи уведомление!\n\n"
        "<b>📊 SofaScore:</b> угловые, удары, владение, голы, очки Q1-Q4\n"
        "<b>🎰 Fonbet:</b> счёт по четвертям Q1-Q4 (баскетбол), голы (футбол)\n\n"
        "<b>🔀 При обоих источниках:</b>\n"
        "Алерты SofaScore и Fonbet работают независимо.\n"
        "В уведомлении всегда указан источник.\n\n"
        "<b>🕐 Алерты на будущие матчи:</b>\n"
        "Мониторинг включается автоматически к старту матча.\n\n"
        "Вручную: <code>/setalert football 12345 corners > 8</code>\n"
        "Fonbet: <code>/setalert basketball 63486634 points > 150 fonbet</code>"
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главная", callback_data="main_menu")]]))


# ═══════════════════════════════════════════════════════
#  WEB
# ═══════════════════════════════════════════════════════

async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("🔐 Сначала получи доступ через /start", parse_mode=ParseMode.HTML)
        return
    public_url = get_runtime_web_url()
    if not public_url:
        await update.message.reply_text("❌ Веб-панель ещё не настроена.", parse_mode=ParseMode.HTML)
        return
    token = issue_web_token(user_id)
    url = f"{public_url.rstrip('/')}/?token={token}"
    await update.message.reply_text(
        f"🌐 <b>Твоя персональная ссылка</b>\n\n<a href=\"{_esc(url)}\">Открыть веб-панель</a>\n\n"
        f"<code>{_esc(url)}</code>\n\nНикому её не передавай.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n/start — главное меню\n/web — веб-панель\n/setalert — создать алерт вручную",
        parse_mode=ParseMode.HTML)


async def _send_web_link(target, user_id: int):
    public_url = get_runtime_web_url()
    if not public_url:
        await target.reply_text("❌ Веб-панель ещё не настроена.", parse_mode=ParseMode.HTML)
        return
    token = issue_web_token(user_id)
    url = f"{public_url.rstrip('/')}/?token={token}"
    await target.reply_text(
        f"🌐 <b>Персональная ссылка</b>\n\n<a href=\"{_esc(url)}\">Открыть веб-панель</a>\n\n"
        f"<code>{_esc(url)}</code>",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════

async def _get_match_meta(source: str, sport: str, match_id: int) -> dict:
    if source == "fonbet":
        ev = await fb.get_event(match_id)
        if ev:
            return {
                "event": ev,
                "kickoff_at": fb.get_kickoff_timestamp(ev) if fb.is_scheduled(ev) else 0,
                "match_label": fb.get_match_label(ev),
            }
        return {"event": None, "kickoff_at": 0, "match_label": f"Match #{match_id}"}
    else:
        if sport == "football":
            ev = await api.football_event(match_id)
        elif sport == "basketball":
            ev = await api.basketball_event(match_id)
        else:
            ev = None
        if not ev:
            return {"event": None, "kickoff_at": 0, "match_label": f"Match #{match_id}"}
        return {
            "event": ev,
            "kickoff_at": api.get_kickoff_timestamp(ev) if api.is_not_started(ev) else 0,
            "match_label": api.get_match_label(ev),
        }


def _scheduled_text(kickoff_at: float) -> str:
    if kickoff_at <= 0 or kickoff_at <= time.time():
        return f"\n⚡ Мониторинг активен — проверяю каждые {POLL_INTERVAL_SECONDS} сек."
    dt = datetime.fromtimestamp(kickoff_at)
    return (
        f"\n⏰ Матч начнётся: <b>{dt.strftime('%d.%m %H:%M')}</b>\n"
        f"😴 Бот спит до начала — 0 запросов.\n"
        f"⚡ Мониторинг включится автоматически!"
    )


def _schedule_prefetch_window(sport: str, day_offset: int):
    for neighbor in (day_offset - 1, day_offset + 1):
        if -7 <= neighbor <= 7:
            date_str = (datetime.now() + timedelta(days=neighbor)).strftime("%Y-%m-%d")
            key = (sport, date_str)
            if key not in _schedule_prefetch_inflight:
                _schedule_prefetch_inflight.add(key)
                asyncio.create_task(_prefetch_day(sport, date_str))


async def _prefetch_day(sport: str, date_str: str):
    try:
        if sport == "football":
            await api.football_by_date(date_str)
        else:
            await api.basketball_by_date(date_str)
    except Exception as e:
        logger.debug("Prefetch failed [%s %s]: %s", sport, date_str, e)
    finally:
        _schedule_prefetch_inflight.discard((sport, date_str))


# ═══════════════════════════════════════════════════════
#  CALLBACK ROUTER
# ═══════════════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data

    if not data.startswith("auth_") and not is_authorized(query.from_user.id):
        try:
            await query.edit_message_text("🔐 Доступ ограничен. Нажми /start для запроса доступа.")
        except Exception:
            pass
        return

    try:
        if data == "main_menu":
            await _show_main_menu(query, edit=True, user_id=query.from_user.id)

        elif data == "source_menu":
            await _show_source_menu(query, query.from_user.id)

        elif data.startswith("set_source:"):
            source = data.split(":")[1]
            await _handle_set_source(query, query.from_user.id, source)

        elif data.startswith("sport:"):
            await _show_sport_menu(query, data.split(":")[1])

        elif data.startswith("day:"):
            _, s, o = data.split(":")
            await _show_day(query, s, int(o))

        elif data.startswith("live:"):
            # live:source:sport
            parts = data.split(":")
            await _show_live(query, parts[1], parts[2])

        elif data.startswith("fb_sched:"):
            sport = data.split(":")[1]
            await _show_fonbet_scheduled(query, sport)

        elif data.startswith("match:"):
            # match:source:sport:id
            parts = data.split(":")
            await _show_match(query, parts[1], parts[2], int(parts[3]))

        elif data.startswith("alert_type:"):
            # alert_type:source:sport:id
            parts = data.split(":")
            await _show_alert_type_selector(query, parts[1], parts[2], int(parts[3]))

        elif data.startswith("alert_stat:"):
            # alert_stat:source:sport:id:key
            parts = data.split(":")
            await _handle_alert_stat_selected(query, context, parts[1], parts[2], int(parts[3]), parts[4])

        elif data.startswith("alert_quick:"):
            # alert_quick:source:sport:id:stat_key:condition (condition may have spaces)
            parts = data.split(":", 6)
            await _handle_quick_alert(query, parts[1], parts[2], int(parts[3]), parts[4], parts[5])

        elif data.startswith("alert_status:"):
            await _show_alert_status(query, int(data.split(":")[1]))

        elif data.startswith("bulk:"):
            # bulk:source:sport:date_or_tag:stat_key
            parts = data.split(":")
            await _handle_bulk_alert(query, parts[1], parts[2], parts[3], parts[4])

        elif data.startswith("bulk_live:"):
            # bulk_live:source:sport:stat_key
            parts = data.split(":")
            await _handle_bulk_live(query, parts[1], parts[2], parts[3])

        elif data.startswith("bulk_select:"):
            # bulk_select:source:sport:date_or_tag
            parts = data.split(":")
            await _show_bulk_stat_selector(query, parts[1], parts[2], parts[3])

        elif data.startswith("myalerts"):
            parts = data.split(":")
            page = int(parts[1]) if len(parts) > 1 else 0
            await _show_my_alerts(query, page=page)

        elif data == "noop":
            pass

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
            count = await db.count_user_active_alerts(query.from_user.id)
            await query.edit_message_text(
                f"⚠️ <b>Отменить ВСЕ {count} алертов?</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Да, отменить все", callback_data="cancel_all_yes"),
                     InlineKeyboardButton("← Назад", callback_data="myalerts:0")]]))

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
                f"🗑 <b>Очистить все {count} алертов?</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да", callback_data="clear_all_yes"),
                     InlineKeyboardButton("← Назад", callback_data="myalerts:0")]]))

        elif data == "clear_all_yes":
            await db.clear_all_user_alerts(query.from_user.id)
            try:
                await query.answer("🗑 Все алерты удалены!", show_alert=True)
            except Exception:
                pass
            await _show_my_alerts(query)

        elif data == "help":
            await _show_help(query)

        elif data == "web_link":
            await _send_web_link(query.message, query.from_user.id)

        elif data.startswith("auth_approve:"):
            target_id = int(data.split(":")[1])
            if query.from_user.id in get_admin_ids():
                approve_user(target_id)
                pending_requests.pop(target_id, None)
                await query.edit_message_text(f"✅ Пользователь {target_id} одобрен!")
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="✅ <b>Доступ одобрен!</b>\nНажми /start для меню.",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass

        elif data.startswith("auth_reject:"):
            target_id = int(data.split(":")[1])
            if query.from_user.id in get_admin_ids():
                reject_user(target_id)
                pending_requests.pop(target_id, None)
                await query.edit_message_text(f"❌ Пользователь {target_id} отклонён.")
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="❌ <b>Заявка отклонена.</b>",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    except Exception as e:
        err_str = str(e)
        if "Message is not modified" in err_str or "Query is too old" in err_str:
            return
        if "Timed out" in err_str:
            logger.warning("Callback timeout [%s]", data)
            return
        if "NetworkError" in err_str or "ConnectError" in err_str:
            logger.warning("Callback network error [%s]: %s", data, e)
            try:
                await query.answer("Сеть нестабильна, попробуй ещё раз.", show_alert=True)
            except Exception:
                pass
            return
        logger.error("Callback error [%s]: %s", data, e, exc_info=True)
        try:
            await query.edit_message_text(f"❌ Ошибка: {e}\n\nПопробуй /start")
        except Exception:
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
        await update.message.reply_text("❌ Формат: <code>> 8</code>", parse_mode=ParseMode.HTML)
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
    source = pending.get("source", "sofascore")
    sport = pending["sport"]
    match_id = pending["fixture_id"]
    stat_key = pending["stat_key"]

    match_meta = await _get_match_meta(source, sport, match_id)
    alert_id = await db.add_alert(
        user_id=update.effective_user.id, chat_id=update.effective_chat.id,
        fixture_id=match_id, stat_key=stat_key, operator=oper,
        threshold=threshold, team=team, sport=sport, source=source,
        kickoff_at=match_meta["kickoff_at"], match_label=match_meta["match_label"])
    context.user_data.pop("pending_alert", None)
    src_label = SOURCE_LABELS.get(source, source)
    await update.message.reply_text(
        f"✅ <b>Алерт #{alert_id} создан!</b>\n"
        f"📡 {src_label}\n"
        f"📊 {stat_key} {oper} {threshold} ({team})\n"
        f"{_scheduled_text(match_meta['kickoff_at'])}",
        parse_mode=ParseMode.HTML)


async def cmd_setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setalert sport id stat_key operator threshold [home|away] [source]
    Пример: /setalert basketball 63486634 points > 150 total fonbet
    """
    args = context.args
    if not args or len(args) < 5:
        await update.message.reply_text(
            "📝 <code>/setalert football 12345 corners > 8</code>\n"
            "Fonbet: <code>/setalert basketball 63486634 points > 150 total fonbet</code>",
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

    team = "total"
    source = "sofascore"
    for extra_arg in args[5:]:
        if extra_arg.lower() in ("home", "away"):
            team = extra_arg.lower()
        elif extra_arg.lower() in (SOURCE_SOFASCORE, SOURCE_FONBET):
            source = extra_arg.lower()

    match_meta = await _get_match_meta(source, sport, fixture_id)
    alert_id = await db.add_alert(
        user_id=update.effective_user.id, chat_id=update.effective_chat.id,
        fixture_id=fixture_id, stat_key=stat_key, operator=oper,
        threshold=threshold, team=team, sport=sport, source=source,
        kickoff_at=match_meta["kickoff_at"], match_label=match_meta["match_label"])
    src_label = SOURCE_LABELS.get(source, source)
    await update.message.reply_text(
        f"✅ Алерт #{alert_id} ({src_label} | {sport} | {stat_key} {oper} {threshold})\n"
        f"{_scheduled_text(match_meta['kickoff_at'])}",
        parse_mode=ParseMode.HTML)


# ═══════════════════════════════════════════════════════
#  POLLING ENGINE
# ═══════════════════════════════════════════════════════

async def _send_html_message(bot, chat_id: int, text: str):
    if chat_id < 0:
        sent, removed = await send_user_push(chat_id, text)
        if sent == 0:
            logger.warning("No active web-push subscriptions for %s", chat_id)
        return
    if bot is None:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "Can't parse entities" in str(e):
            try:
                await bot.send_message(chat_id=chat_id, text=_plain_from_html(text))
                return
            except Exception:
                pass
        logger.error("Send failed to %s: %s", chat_id, e)


def _fixture_needs_probe(alerts: list[dict], now_ts: float) -> bool:
    kickoffs = [float(a.get("kickoff_at") or 0) for a in alerts]
    if not kickoffs or any(k <= 0 for k in kickoffs):
        return True
    earliest = min(kickoffs)
    latest = max(kickoffs)
    post_kickoff_probe_window = max(KICKOFF_LOOKAROUND_SECONDS, 3 * 3600)
    return (earliest - KICKOFF_LOOKAROUND_SECONDS) <= now_ts <= (latest + post_kickoff_probe_window)


async def poll_and_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Главный polling-цикл. Обрабатывает алерты из ОБОИХ источников независимо:
    - sofascore.football, sofascore.basketball — через SofaScore API
    - fonbet.basketball, fonbet.football — через Fonbet API
    """
    if _poll_lock.locked():
        logger.warning("Previous polling cycle still running, skipping")
        return

    async with _poll_lock:
        started_at = time.monotonic()
        try:
            snapshot = await db.get_active_alerts_snapshot()
            if not snapshot:
                return

            tasks = []

            # ── SofaScore alerts ──────────────────────────
            ss_alerts = snapshot.get("sofascore", {})
            if ss_alerts.get("football"):
                tasks.append(_poll_sofascore_football(context, ss_alerts["football"]))
            if ss_alerts.get("basketball"):
                tasks.append(_poll_sofascore_basketball(context, ss_alerts["basketball"]))

            # ── Fonbet alerts ─────────────────────────────
            fb_alerts = snapshot.get("fonbet", {})
            if fb_alerts.get("basketball"):
                tasks.append(_poll_fonbet_basketball(context, fb_alerts["basketball"]))
            if fb_alerts.get("football"):
                tasks.append(_poll_fonbet_football(context, fb_alerts["football"]))

            if tasks:
                await asyncio.gather(*tasks)

            api.cleanup_cache()
            fb.cleanup_cache()
            logger.info(
                "Polling done in %.2fs | SS football=%d basketball=%d | FB football=%d basketball=%d",
                time.monotonic() - started_at,
                len(ss_alerts.get("football", {})),
                len(ss_alerts.get("basketball", {})),
                len(fb_alerts.get("football", {})),
                len(fb_alerts.get("basketball", {})),
            )
        except Exception as e:
            logger.error("Polling error: %s", e, exc_info=True)


# ─── SofaScore polling ─────────────────────────────────

async def _poll_sofascore_football(context, alerts_by_fixture: dict):
    now_ts = time.time()
    all_live = await api.football_live()
    live_index = {api.get_event_id(ev): ev for ev in all_live}

    for fid, alerts in alerts_by_fixture.items():
        ev = live_index.get(fid)
        if ev is None and _fixture_needs_probe(alerts, now_ts):
            ev = await api.football_event(fid)
            if not ev:
                continue
            if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
                await db.deactivate_fixture_alerts(fid, "football", "sofascore")
                continue
            if api.is_not_started(ev):
                continue
        elif ev is None:
            continue

        if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
            await db.deactivate_fixture_alerts(fid, "football", "sofascore")
            continue

        needs_stats = any(a["stat_key"] not in {"goals"} for a in alerts)
        stats = await api.football_statistics(fid) if needs_stats else {}

        for alert in alerts:
            triggered, value = check_football_alert(alert, stats, ev)
            if triggered:
                msg = format_football_notification(alert, value, ev)
                await _send_html_message(context.bot, alert["chat_id"], msg)
                await db.mark_triggered(alert["id"], value, msg)


async def _poll_sofascore_basketball(context, alerts_by_fixture: dict):
    now_ts = time.time()
    all_live = await api.basketball_live()
    live_index = {api.get_event_id(ev): ev for ev in all_live}

    for gid, alerts in alerts_by_fixture.items():
        ev = live_index.get(gid)
        if ev is None and _fixture_needs_probe(alerts, now_ts):
            ev = await api.basketball_event(gid)
            if not ev:
                continue
            if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
                await db.deactivate_fixture_alerts(gid, "basketball", "sofascore")
                continue
            if api.is_not_started(ev):
                continue
        elif ev is None:
            continue

        if api.is_finished(ev) or api.is_canceled_or_postponed(ev):
            await db.deactivate_fixture_alerts(gid, "basketball", "sofascore")
            continue

        parsed = api.parse_basketball_scores(ev)
        for alert in alerts:
            triggered, value, extra = check_basketball_alert(alert, parsed)
            if extra and "expired" in extra.lower():
                await db.deactivate_alert_by_id(alert["id"])
                continue
            if triggered:
                msg = format_basketball_notification(alert, value, ev, extra)
                await _send_html_message(context.bot, alert["chat_id"], msg)
                await db.mark_triggered(alert["id"], value or 0, msg)


# ─── Fonbet polling ────────────────────────────────────

async def _poll_fonbet_basketball(context, alerts_by_fixture: dict):
    """
    Polling баскетбола Fonbet.
    Один запрос получает ВСЕ live матчи, дальше матчим по ID.
    """
    live_events = await fb.basketball_live(include_esports=True)
    sched_events = await fb.basketball_scheduled(include_esports=True)
    all_events = {fb.get_event_id(e): e for e in live_events + sched_events}
    now_ts = time.time()

    for fid, alerts in alerts_by_fixture.items():
        ev = all_events.get(fid)

        if ev is None:
            # Матч не найден — возможно завершён или удалён
            if not _fixture_needs_probe(alerts, now_ts):
                continue
            # Пробуем получить через get_event (он проверяет кеш)
            ev = await fb.get_event(fid)
            if ev is None:
                # Нет в Fonbet вообще — деактивируем если стало старым
                kickoffs = [float(a.get("kickoff_at") or 0) for a in alerts]
                max_kickoff = max(kickoffs) if kickoffs else 0
                if max_kickoff > 0 and time.time() - max_kickoff > 4 * 3600:
                    await db.deactivate_fixture_alerts(fid, "basketball", "fonbet")
                continue

        # Проверяем состояние матча
        if not fb.is_live(ev) and not fb.is_scheduled(ev):
            # place == 'notActive' или другое — завершён
            await db.deactivate_fixture_alerts(fid, "basketball", "fonbet")
            continue

        if not fb.is_live(ev):
            # Ещё не начался — ждём
            continue

        parsed = fb.parse_basketball_scores(ev)
        for alert in alerts:
            triggered, value, extra = check_basketball_alert(alert, parsed)
            if extra and "expired" in extra.lower():
                await db.deactivate_alert_by_id(alert["id"])
                continue
            if triggered:
                msg = fb.format_notification(alert, value or 0, ev, extra)
                await _send_html_message(context.bot, alert["chat_id"], msg)
                await db.mark_triggered(alert["id"], value or 0, msg)


async def _poll_fonbet_football(context, alerts_by_fixture: dict):
    """
    Polling футбола Fonbet (только голы — у Fonbet нет детальной стат.).
    """
    live_events = await fb.football_live(include_esports=True)
    all_events = {fb.get_event_id(e): e for e in live_events}
    now_ts = time.time()

    for fid, alerts in alerts_by_fixture.items():
        ev = all_events.get(fid)
        if ev is None:
            if not _fixture_needs_probe(alerts, now_ts):
                continue
            ev = await fb.get_event(fid)
            if ev is None:
                continue
        if not fb.is_live(ev):
            continue

        # Для футбола Fonbet — только счёт через liveInfo
        live_info = ev.get("_live_info")
        if not live_info:
            continue

        scores_arr = live_info.get("scores", [])
        total = scores_arr[0][0] if scores_arr and scores_arr[0] else {}
        home_goals = int(total.get("c1", 0))
        away_goals = int(total.get("c2", 0))

        # Синтетический event-объект для check_football_alert
        synthetic_ev = {
            "homeScore": {"current": home_goals},
            "awayScore": {"current": away_goals},
            "homeTeam": {"name": fb.get_home_name(ev)},
            "awayTeam": {"name": fb.get_away_name(ev)},
            "status": {"type": "inprogress", "description": live_info.get("timer", "")},
        }

        for alert in alerts:
            if alert["stat_key"] != "goals":
                continue  # Fonbet футбол — только голы
            triggered, value = check_football_alert(alert, {}, synthetic_ev)
            if triggered:
                msg = format_football_notification(alert, value, synthetic_ev)
                # Добавляем тег источника
                msg = msg.replace("АЛЕРТ СРАБОТАЛ!", "АЛЕРТ СРАБОТАЛ!\n📡 Fonbet 🎰")
                await _send_html_message(context.bot, alert["chat_id"], msg)
                await db.mark_triggered(alert["id"], value, msg)


# ═══════════════════════════════════════════════════════
#  OVERDUE CHECK
# ═══════════════════════════════════════════════════════

async def check_overdue_matches(context: ContextTypes.DEFAULT_TYPE):
    try:
        cleaned = await db.auto_cleanup_stale_alerts(max_age_hours=24)
        if cleaned:
            logger.info("Auto-cleaned %d stale alerts", cleaned)

        overdue = await db.get_overdue_alerts_not_notified(MATCH_START_TOLERANCE)
        if not overdue:
            return

        by_fixture: dict = {}
        for a in overdue:
            key = (a.get("source", "sofascore"), a.get("sport", "football"), a["fixture_id"])
            by_fixture.setdefault(key, []).append(a)

        canceled_ids, canceled_matches, overdue_ids, overdue_matches = [], [], [], []

        for (source, sport, fid), alerts in by_fixture.items():
            if source == "fonbet":
                ev = await fb.get_event(fid)
                if ev:
                    if fb.is_live(ev):
                        for a in alerts:
                            canceled_ids.append(a["id"])
                        continue
                    elif not fb.is_scheduled(ev):
                        label = alerts[0].get("match_label", str(fid))
                        canceled_matches.append(f"[Fonbet] {label}")
                        for a in alerts:
                            canceled_ids.append(a["id"])
                        await db.deactivate_fixture_alerts(fid, sport, "fonbet")
                        continue
            else:
                if sport == "football":
                    ev = await api.football_event(fid)
                else:
                    ev = await api.basketball_event(fid)

                if ev and (api.is_live(ev) or api.is_finished(ev)):
                    for a in alerts:
                        canceled_ids.append(a["id"])
                    if api.is_finished(ev):
                        await db.deactivate_fixture_alerts(fid, sport, "sofascore")
                    continue
                if ev and api.is_canceled_or_postponed(ev):
                    label = alerts[0].get("match_label", str(fid))
                    canceled_matches.append(f"[SS] {label}")
                    for a in alerts:
                        canceled_ids.append(a["id"])
                    await db.deactivate_fixture_alerts(fid, sport, "sofascore")
                    continue

            label = alerts[0].get("match_label", str(fid))
            overdue_matches.append(label)
            for a in alerts:
                overdue_ids.append(a["id"])

        await db.mark_overdue_notified(canceled_ids + overdue_ids)

        by_chat: dict = {}
        for a in overdue:
            by_chat[a["chat_id"]] = a["user_id"]

        if not canceled_matches and not overdue_matches:
            return

        for chat_id in by_chat:
            lines = []
            if canceled_matches:
                lines.append(f"❌ <b>Отменено/перенесено ({len(canceled_matches)}):</b>")
                for m in canceled_matches[:10]:
                    lines.append(f"  • {_esc(m)}")
                lines.append("Алерты деактивированы.\n")
            if overdue_matches:
                lines.append(f"⚠️ <b>Задерживаются ({len(overdue_matches)}):</b>")
                for m in overdue_matches[:10]:
                    lines.append(f"  • {_esc(m)}")
                lines.append("Алерты пока активны.")
            if lines:
                await _send_html_message(context.bot, chat_id, "\n".join(lines))

    except Exception as e:
        logger.error("Overdue check error: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════
#  STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════

async def _startup_core(bot=None):
    global _web_runner
    await db.init_db()
    logger.info("Database initialized")
    cleaned = await db.auto_cleanup_stale_alerts(max_age_hours=24)
    if cleaned:
        logger.info("Startup cleanup: %d stale alerts deactivated", cleaned)
    if bot is not None:
        await bot.set_my_commands([
            BotCommand("start", "Главное меню"),
            BotCommand("setalert", "Создать алерт вручную"),
            BotCommand("help", "Помощь"),
            BotCommand("web", "Открыть веб-панель"),
        ])
    _web_runner, _ = await start_web_app()


async def post_init(app: Application):
    await _startup_core(app.bot)


async def _shutdown_core():
    global _web_runner
    await api.close_session()
    await fb.close_session()
    await db.close_db()
    await stop_web_app(_web_runner)
    _web_runner = None


async def post_shutdown(app: Application):
    await _shutdown_core()


async def run_web_only():
    logger.warning("Starting in web-push-only mode")
    await _startup_core(bot=None)
    context = SimpleNamespace(bot=None)
    try:
        while True:
            await poll_and_check(context)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await _shutdown_core()


async def _telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает сетевые ошибки Telegram и логирует их как WARNING вместо ERROR."""
    err = context.error
    if err is None:
        return
    err_str = str(err)
    # Периодические сетевые сбои — не критично, бот сам восстановится
    if any(x in err_str for x in ("ReadError", "ConnectError", "NetworkError", "TimedOut", "Timed out")):
        logger.warning("Telegram network hiccup (auto-retry): %s", err_str[:120])
        return
    # Всё остальное — настоящая ошибка
    logger.error("Telegram error: %s", err_str, exc_info=context.error)


def main():
    if not TELEGRAM_ENABLED:
        asyncio.run(run_web_only())
        return

    if not TELEGRAM_BOT_TOKEN:
        if ALLOW_WEB_ONLY_FALLBACK:
            asyncio.run(run_web_only())
            return
        print("❌ Set TELEGRAM_BOT_TOKEN in .env!")
        return

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    telegram_httpx_kwargs = {
        "transport": httpx.AsyncHTTPTransport(
            local_address="0.0.0.0", trust_env=False, retries=1)
    }
    if TELEGRAM_PROXY:
        telegram_httpx_kwargs["proxy"] = TELEGRAM_PROXY
    telegram_request = HTTPXRequest(
        connect_timeout=15, read_timeout=20, write_timeout=20, pool_timeout=20,
        httpx_kwargs=telegram_httpx_kwargs)
    telegram_updates_request = HTTPXRequest(
        connect_timeout=15, read_timeout=60, write_timeout=20, pool_timeout=20,
        httpx_kwargs=telegram_httpx_kwargs)

    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if TELEGRAM_BASE_URL:
        builder = (builder
            .base_url(TELEGRAM_BASE_URL.rstrip("/") + "/bot")
            .base_file_url(TELEGRAM_BASE_URL.rstrip("/") + "/file/bot"))
    app = (builder
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .request(telegram_request)
        .get_updates_request(telegram_updates_request)
        .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setalert", cmd_setalert))
    app.add_handler(CommandHandler("web", cmd_web))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(_telegram_error_handler)

    app.job_queue.run_repeating(
        poll_and_check, interval=POLL_INTERVAL_SECONDS, first=5, name="poll_live_stats")
    app.job_queue.run_repeating(
        check_overdue_matches, interval=600, first=60, name="check_overdue")

    logger.info("Starting %s with SofaScore + Fonbet support...", APP_NAME)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)
    except TimedOut:
        if not ALLOW_WEB_ONLY_FALLBACK:
            raise
        asyncio.run(run_web_only())


if __name__ == "__main__":
    main()
