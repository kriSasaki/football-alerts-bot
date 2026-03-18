"""
Async SQLite database for storing user alerts and notification history.
v2: added bulk alert support and alert stats query.
"""
import aiosqlite
import time
from config import DATABASE_PATH


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                sport       TEXT    NOT NULL DEFAULT 'football',
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active, fixture_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_web_push_user ON web_push_subscriptions(user_id)")
        # Migrations for older DBs
        for sql in [
            "ALTER TABLE alerts ADD COLUMN sport TEXT NOT NULL DEFAULT 'football'",
            "ALTER TABLE alerts ADD COLUMN kickoff_at REAL DEFAULT 0",
            "ALTER TABLE alerts ADD COLUMN match_label TEXT DEFAULT ''",
            "ALTER TABLE alerts ADD COLUMN overdue_notified INTEGER DEFAULT 0",
            "ALTER TABLE alert_history ADD COLUMN sport TEXT NOT NULL DEFAULT 'football'",
            "ALTER TABLE web_push_subscriptions ADD COLUMN user_agent TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(sql)
            except Exception:
                pass
        await db.commit()


async def add_alert(
    user_id: int, chat_id: int, fixture_id: int,
    stat_key: str, operator: str, threshold: float,
    team: str = "total", sport: str = "football",
    kickoff_at: float = 0, match_label: str = "",
) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO alerts
               (user_id, chat_id, sport, fixture_id, stat_key, operator,
                threshold, team, created_at, kickoff_at, match_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, sport, fixture_id, stat_key, operator,
             threshold, team, time.time(), kickoff_at, match_label),
        )
        await db.commit()
        return cursor.lastrowid


async def add_alerts_bulk(
    user_id: int, chat_id: int, sport: str,
    matches: list[dict], stat_key: str, operator: str,
    threshold: float, team: str = "total",
) -> list[int]:
    """Add alerts for multiple matches at once. Returns list of alert IDs."""
    now = time.time()
    ids = []
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for m in matches:
            cursor = await db.execute(
                """INSERT INTO alerts
                   (user_id, chat_id, sport, fixture_id, stat_key, operator,
                    threshold, team, created_at, kickoff_at, match_label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, chat_id, sport, m["fixture_id"], stat_key, operator,
                 threshold, team, now, m.get("kickoff_at", 0), m.get("match_label", "")),
            )
            ids.append(cursor.lastrowid)
        await db.commit()
    return ids


async def get_active_alerts(fixture_id: int | None = None, sport: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM alerts WHERE active = 1"
        p = []
        if fixture_id:
            q += " AND fixture_id = ?"; p.append(fixture_id)
        if sport:
            q += " AND sport = ?"; p.append(sport)
        return [dict(r) for r in await db.execute_fetchall(q, p)]


async def get_active_alerts_snapshot() -> dict[str, dict[int, list[dict]]]:
    """Return all active alerts grouped as sport -> fixture_id -> alerts."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM alerts WHERE active = 1 ORDER BY sport, fixture_id, id"
        )

    snapshot: dict[str, dict[int, list[dict]]] = {}
    for row in rows:
        alert = dict(row)
        sport = alert.get("sport", "football")
        fixture_id = alert["fixture_id"]
        snapshot.setdefault(sport, {}).setdefault(fixture_id, []).append(alert)
    return snapshot


async def get_user_alerts(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM alerts WHERE user_id = ? AND active = 1 ORDER BY kickoff_at ASC",
            (user_id,),
        )
        return [dict(r) for r in rows]


async def get_alert_by_id(alert_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        return dict(rows[0]) if rows else None


async def mark_triggered(alert_id: int, value: float, message: str):
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if not rows:
            return
        a = dict(rows[0])
        await db.execute(
            "UPDATE alerts SET triggered=1, active=0, triggered_at=? WHERE id=?",
            (now, alert_id))
        await db.execute(
            """INSERT INTO alert_history
               (alert_id, fixture_id, user_id, sport, stat_key, value, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (alert_id, a["fixture_id"], a["user_id"], a.get("sport", "football"),
             a["stat_key"], value, message, now))
        await db.commit()


async def deactivate_alert(alert_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        c = await db.execute(
            "UPDATE alerts SET active=0 WHERE id=? AND user_id=?", (alert_id, user_id))
        await db.commit()
        return c.rowcount > 0


async def deactivate_fixture_alerts(fixture_id: int, sport: str = None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if sport:
            await db.execute(
                "UPDATE alerts SET active=0 WHERE fixture_id=? AND sport=? AND active=1",
                (fixture_id, sport))
        else:
            await db.execute(
                "UPDATE alerts SET active=0 WHERE fixture_id=? AND active=1", (fixture_id,))
        await db.commit()


async def get_watched_by_sport() -> dict[str, set[int]]:
    """Only return fixtures whose kickoff_at has already passed (or is 0 = live)."""
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT DISTINCT sport, fixture_id FROM alerts "
            "WHERE active=1 AND kickoff_at <= ?", (now,))
        result: dict[str, set[int]] = {}
        for sport, fid in rows:
            result.setdefault(sport, set()).add(fid)
        return result


async def get_overdue_alerts(tolerance_seconds: int) -> list[dict]:
    """Alerts where kickoff_at is in the past but match hasn't started.
    Returns alerts where kickoff_at > 0 AND kickoff_at < (now - tolerance)."""
    cutoff = time.time() - tolerance_seconds
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM alerts WHERE active=1 AND kickoff_at > 0 AND kickoff_at < ?",
            (cutoff,))
        return [dict(r) for r in rows]


async def clear_all_user_alerts(user_id: int):
    """Deactivate ALL alerts for a user (active + triggered)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE alerts SET active=0 WHERE user_id=? AND active=1", (user_id,))
        await db.commit()


async def auto_cleanup_stale_alerts(max_age_hours: int = 24) -> int:
    """Silently deactivate alerts whose kickoff was more than max_age_hours ago.
    Returns count of cleaned alerts."""
    cutoff = time.time() - (max_age_hours * 3600)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        c = await db.execute(
            "UPDATE alerts SET active=0 WHERE active=1 AND kickoff_at > 0 AND kickoff_at < ?",
            (cutoff,))
        await db.commit()
        return c.rowcount


async def get_overdue_alerts_not_notified(tolerance_seconds: int) -> list[dict]:
    """Get overdue alerts that haven't been notified yet.
    Only returns alerts where kickoff was TODAY and overdue by tolerance."""
    cutoff = time.time() - tolerance_seconds
    # Only today's alerts: kickoff_at must be within last 24 hours
    today_start = time.time() - 86400
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM alerts WHERE active=1 AND kickoff_at > ? AND kickoff_at < ? AND overdue_notified=0",
            (today_start, cutoff))
        return [dict(r) for r in rows]


async def mark_overdue_notified(alert_ids: list[int]):
    """Mark alerts as overdue-notified so we don't spam again."""
    if not alert_ids:
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        placeholders = ",".join("?" for _ in alert_ids)
        await db.execute(
            f"UPDATE alerts SET overdue_notified=1 WHERE id IN ({placeholders})",
            alert_ids)
        await db.commit()


async def count_user_active_alerts(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM alerts WHERE user_id = ? AND active = 1", (user_id,))
        return rows[0][0] if rows else 0


async def save_web_push_subscription(user_id: int, endpoint: str, subscription_json: str, user_agent: str = ""):
    now = time.time()
    async with aiosqlite.connect(DATABASE_PATH) as db:
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
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,))
        await db.commit()


async def get_web_push_subscriptions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM web_push_subscriptions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]
