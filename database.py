"""
Async SQLite database for storing user alerts and notification history.
v3: added source field (sofascore/fonbet), user_settings table.
"""
import asyncio
import time

import aiosqlite

from config import DATABASE_PATH


_db_conn: aiosqlite.Connection | None = None
_db_init_lock = asyncio.Lock()


async def _get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is not None:
        return _db_conn

    async with _db_init_lock:
        if _db_conn is not None:
            return _db_conn
        db = await aiosqlite.connect(DATABASE_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA foreign_keys=ON")
        _db_conn = db
        return db


async def close_db():
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


async def init_db():
    db = await _get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            sport       TEXT    NOT NULL DEFAULT 'football',
            source      TEXT    NOT NULL DEFAULT 'sofascore',
            fixture_id  INTEGER NOT NULL,
            stat_key    TEXT    NOT NULL,
            operator    TEXT    NOT NULL,
            threshold   REAL    NOT NULL,
            team        TEXT    DEFAULT 'total',
            active      INTEGER DEFAULT 1,
            triggered   INTEGER DEFAULT 0,
            created_at  REAL    NOT NULL,
            triggered_at REAL,
            kickoff_at  REAL    DEFAULT 0,
            match_label TEXT    DEFAULT '',
            overdue_notified INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id    INTEGER NOT NULL,
            fixture_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            sport       TEXT    NOT NULL DEFAULT 'football',
            source      TEXT    NOT NULL DEFAULT 'sofascore',
            stat_key    TEXT    NOT NULL,
            value       REAL,
            message     TEXT,
            created_at  REAL    NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            endpoint    TEXT    NOT NULL UNIQUE,
            subscription_json TEXT NOT NULL,
            user_agent  TEXT    DEFAULT '',
            created_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL
        )
    """)
    # Настройки пользователя: источник данных, фильтр киберспорта и т.д.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id     INTEGER PRIMARY KEY,
            data_source TEXT    NOT NULL DEFAULT 'sofascore',
            hide_esports INTEGER DEFAULT 1,
            updated_at  REAL    NOT NULL
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active, fixture_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, active)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts(active, source)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_web_push_user ON web_push_subscriptions(user_id)")
    # Миграции для существующих БД
    for sql in [
        "ALTER TABLE alerts ADD COLUMN sport TEXT NOT NULL DEFAULT 'football'",
        "ALTER TABLE alerts ADD COLUMN source TEXT NOT NULL DEFAULT 'sofascore'",
        "ALTER TABLE alerts ADD COLUMN kickoff_at REAL DEFAULT 0",
        "ALTER TABLE alerts ADD COLUMN match_label TEXT DEFAULT ''",
        "ALTER TABLE alerts ADD COLUMN overdue_notified INTEGER DEFAULT 0",
        "ALTER TABLE alert_history ADD COLUMN sport TEXT NOT NULL DEFAULT 'football'",
        "ALTER TABLE alert_history ADD COLUMN source TEXT NOT NULL DEFAULT 'sofascore'",
        "ALTER TABLE web_push_subscriptions ADD COLUMN user_agent TEXT DEFAULT ''",
    ]:
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.commit()


# ─── User Settings ──────────────────────────────────────

async def get_user_settings(user_id: int) -> dict:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
    )
    if rows:
        return dict(rows[0])
    # Дефолт
    return {"user_id": user_id, "data_source": "sofascore", "hide_esports": 1}


async def set_user_setting(user_id: int, key: str, value) -> None:
    """Обновить одну настройку пользователя. key: data_source | hide_esports"""
    db = await _get_db()
    now = time.time()
    # Upsert
    await db.execute(
        """INSERT INTO user_settings (user_id, data_source, hide_esports, updated_at)
           VALUES (?, 'sofascore', 1, ?)
           ON CONFLICT(user_id) DO NOTHING""",
        (user_id, now),
    )
    await db.execute(
        f"UPDATE user_settings SET {key} = ?, updated_at = ? WHERE user_id = ?",
        (value, now, user_id),
    )
    await db.commit()


# ─── Alerts ────────────────────────────────────────────

async def add_alert(
    user_id: int, chat_id: int, fixture_id: int,
    stat_key: str, operator: str, threshold: float,
    team: str = "total", sport: str = "football",
    source: str = "sofascore",
    kickoff_at: float = 0, match_label: str = "",
) -> int:
    db = await _get_db()
    cursor = await db.execute(
        """INSERT INTO alerts
           (user_id, chat_id, sport, source, fixture_id, stat_key, operator,
            threshold, team, created_at, kickoff_at, match_label)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, chat_id, sport, source, fixture_id, stat_key, operator,
         threshold, team, time.time(), kickoff_at, match_label),
    )
    await db.commit()
    return cursor.lastrowid


async def add_alerts_bulk(
    user_id: int, chat_id: int, sport: str,
    matches: list[dict], stat_key: str, operator: str,
    threshold: float, team: str = "total",
    source: str = "sofascore",
) -> list[int]:
    now = time.time()
    ids = []
    db = await _get_db()
    for m in matches:
        cursor = await db.execute(
            """INSERT INTO alerts
               (user_id, chat_id, sport, source, fixture_id, stat_key, operator,
                threshold, team, created_at, kickoff_at, match_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, sport, source, m["fixture_id"], stat_key, operator,
             threshold, team, now, m.get("kickoff_at", 0), m.get("match_label", "")),
        )
        ids.append(cursor.lastrowid)
    await db.commit()
    return ids


async def get_active_alerts(fixture_id: int | None = None, sport: str | None = None) -> list[dict]:
    db = await _get_db()
    query = "SELECT * FROM alerts WHERE active = 1"
    params = []
    if fixture_id:
        query += " AND fixture_id = ?"
        params.append(fixture_id)
    if sport:
        query += " AND sport = ?"
        params.append(sport)
    rows = await db.execute_fetchall(query, params)
    return [dict(r) for r in rows]


async def get_active_alerts_snapshot() -> dict:
    """
    Возвращает snapshot всех активных алертов, сгруппированных по source и sport:
    {
      "sofascore": { "football": {fixture_id: [alert, ...]}, "basketball": {...} },
      "fonbet":    { "basketball": {fixture_id: [alert, ...]}, ... },
    }
    """
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM alerts WHERE active = 1 ORDER BY source, sport, fixture_id, id"
    )
    snapshot: dict = {}
    for row in rows:
        alert = dict(row)
        source = alert.get("source", "sofascore")
        sport = alert.get("sport", "football")
        fixture_id = alert["fixture_id"]
        snapshot.setdefault(source, {}).setdefault(sport, {}).setdefault(fixture_id, []).append(alert)
    return snapshot


async def get_user_alerts(user_id: int) -> list[dict]:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM alerts WHERE user_id = ? AND active = 1 ORDER BY source, kickoff_at ASC",
        (user_id,),
    )
    return [dict(r) for r in rows]


async def get_alert_by_id(alert_id: int) -> dict | None:
    db = await _get_db()
    rows = await db.execute_fetchall("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    return dict(rows[0]) if rows else None


async def mark_triggered(alert_id: int, value: float, message: str):
    now = time.time()
    db = await _get_db()
    rows = await db.execute_fetchall("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    if not rows:
        return
    alert = dict(rows[0])
    await db.execute(
        "UPDATE alerts SET triggered=1, active=0, triggered_at=? WHERE id=?",
        (now, alert_id),
    )
    await db.execute(
        """INSERT INTO alert_history
           (alert_id, fixture_id, user_id, sport, source, stat_key, value, message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (alert_id, alert["fixture_id"], alert["user_id"],
         alert.get("sport", "football"), alert.get("source", "sofascore"),
         alert["stat_key"], value, message, now),
    )
    await db.commit()


async def deactivate_alert(alert_id: int, user_id: int) -> bool:
    db = await _get_db()
    cursor = await db.execute(
        "UPDATE alerts SET active=0 WHERE id=? AND user_id=?",
        (alert_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def deactivate_alert_by_id(alert_id: int) -> bool:
    db = await _get_db()
    cursor = await db.execute(
        "UPDATE alerts SET active=0 WHERE id=? AND active=1",
        (alert_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def deactivate_fixture_alerts(fixture_id: int, sport: str = None, source: str = None):
    db = await _get_db()
    query = "UPDATE alerts SET active=0 WHERE fixture_id=? AND active=1"
    params = [fixture_id]
    if sport:
        query += " AND sport=?"
        params.append(sport)
    if source:
        query += " AND source=?"
        params.append(source)
    await db.execute(query, params)
    await db.commit()


async def clear_all_user_alerts(user_id: int):
    db = await _get_db()
    await db.execute(
        "UPDATE alerts SET active=0 WHERE user_id=? AND active=1",
        (user_id,),
    )
    await db.commit()


async def auto_cleanup_stale_alerts(max_age_hours: int = 24) -> int:
    cutoff = time.time() - (max_age_hours * 3600)
    db = await _get_db()
    cursor = await db.execute(
        "UPDATE alerts SET active=0 WHERE active=1 AND kickoff_at > 0 AND kickoff_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def get_overdue_alerts_not_notified(tolerance_seconds: int) -> list[dict]:
    cutoff = time.time() - tolerance_seconds
    today_start = time.time() - 86400
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM alerts WHERE active=1 AND kickoff_at > ? AND kickoff_at < ? AND overdue_notified=0",
        (today_start, cutoff),
    )
    return [dict(r) for r in rows]


async def mark_overdue_notified(alert_ids: list[int]):
    if not alert_ids:
        return
    db = await _get_db()
    placeholders = ",".join("?" for _ in alert_ids)
    await db.execute(
        f"UPDATE alerts SET overdue_notified=1 WHERE id IN ({placeholders})",
        alert_ids,
    )
    await db.commit()


async def count_user_active_alerts(user_id: int) -> int:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS cnt FROM alerts WHERE user_id = ? AND active = 1",
        (user_id,),
    )
    return rows[0]["cnt"] if rows else 0


# ─── Web Push ──────────────────────────────────────────

async def save_web_push_subscription(user_id: int, endpoint: str, subscription_json: str, user_agent: str = ""):
    now = time.time()
    db = await _get_db()
    await db.execute(
        """
        INSERT INTO web_push_subscriptions (user_id, endpoint, subscription_json, user_agent, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            user_id=excluded.user_id,
            subscription_json=excluded.subscription_json,
            user_agent=excluded.user_agent,
            updated_at=excluded.updated_at
        """,
        (user_id, endpoint, subscription_json, user_agent, now, now),
    )
    await db.commit()


async def delete_web_push_subscription(endpoint: str):
    db = await _get_db()
    await db.execute("DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,))
    await db.commit()


async def get_web_push_subscriptions(user_id: int) -> list[dict]:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM web_push_subscriptions WHERE user_id = ? ORDER BY updated_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]
