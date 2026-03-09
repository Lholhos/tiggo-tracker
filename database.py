"""
SQLite database layer for the DealRadar price tracker.
Stores listings and full price history per listing (keyed by URL).
"""

import sqlite3
import json
import subprocess
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "tracker.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT UNIQUE NOT NULL,
                title       TEXT,
                year        TEXT,
                location    TEXT,
                dealer      TEXT,
                image       TEXT,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                is_active   INTEGER DEFAULT 1,
                source      TEXT DEFAULT 'AutoTrader'
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id  INTEGER NOT NULL REFERENCES listings(id),
                price       INTEGER NOT NULL,
                mileage     INTEGER,
                mileage_raw TEXT,
                price_raw   TEXT,
                scraped_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                listings_found  INTEGER DEFAULT 0,
                new_listings    INTEGER DEFAULT 0,
                price_changes   INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'running',
                error           TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_listing
                ON price_history(listing_id, scraped_at DESC);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Safely add columns if migrating an existing database
        try:
            conn.execute("ALTER TABLE listings ADD COLUMN dealer TEXT;")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE listings ADD COLUMN source TEXT DEFAULT 'AutoTrader';")
        except sqlite3.OperationalError:
            pass


def upsert_listings(listings: list[dict]) -> dict:
    """
    Insert or update listings. Returns stats dict.
    """
    now = datetime.now().isoformat(timespec="seconds")
    stats = {"new": 0, "price_changes": 0, "unchanged": 0}

    with get_conn() as conn:
        # Load price alert setting
        row = conn.execute("SELECT value FROM settings WHERE key='price_alert'").fetchone()
        alert_threshold = int(row[0]) if row and row[0] and row[0].isdigit() else 0

        for item in listings:
            url = item.get("url", "").strip()
            if not url:
                continue

            price = item.get("price")
            if not price:
                continue

            # Check existing
            row = conn.execute(
                "SELECT id, last_seen FROM listings WHERE url = ?", (url,)
            ).fetchone()

            if row is None:
                # New listing
                conn.execute(
                    """INSERT INTO listings (url, title, year, location, dealer, image, first_seen, last_seen, is_active, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (url, item.get("title"), item.get("year"), item.get("location"), item.get("dealer"), item.get("image"), now, now, item.get("source", "AutoTrader")),
                )
                listing_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                stats["new"] += 1
            else:
                listing_id = row["id"]
                # Update last_seen and metadata
                conn.execute(
                    """UPDATE listings SET last_seen=?, title=?, location=?, dealer=?, image=?, is_active=1, source=COALESCE(?, source)
                       WHERE id=?""",
                    (now, item.get("title"), item.get("location"), item.get("dealer"), item.get("image"), item.get("source"), listing_id),
                )

                # Check last price
                last = conn.execute(
                    "SELECT price FROM price_history WHERE listing_id=? ORDER BY scraped_at DESC LIMIT 1",
                    (listing_id,),
                ).fetchone()

                if last and last["price"] == price:
                    stats["unchanged"] += 1
                    continue
                else:
                    stats["price_changes"] += 1
                    if alert_threshold > 0 and price <= alert_threshold:
                        _trigger_mac_notification("Price Drop Alert!", f"{item.get('title')} is now R{price:,}")

            # Record price
            conn.execute(
                """INSERT INTO price_history (listing_id, price, mileage, mileage_raw, price_raw, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (listing_id, price, item.get("mileage"), item.get("mileage_raw"), item.get("price_raw"), now),
            )

            # If new and below threshold, alert
            if row is None and alert_threshold > 0 and price <= alert_threshold:
                _trigger_mac_notification("New Deal Alert!", f"{item.get('title')} listed for R{price:,}")

        # Mark listings not seen in this run as inactive
        urls_seen = [i["url"] for i in listings if i.get("url")]
        if urls_seen:
            placeholders = ",".join("?" * len(urls_seen))
            conn.execute(
                f"UPDATE listings SET is_active=0 WHERE url NOT IN ({placeholders})",
                urls_seen,
            )

    return stats


def get_listings_with_latest_price(include_inactive: bool = False) -> list[dict]:
    """Returns listings with their latest price and price delta."""
    with get_conn() as conn:
        where_clause = "" if include_inactive else "WHERE l.is_active = 1"
        rows = conn.execute(f"""
            SELECT
                l.id, l.url, l.title, l.year, l.location, l.dealer, l.image, l.source,
                l.first_seen, l.last_seen, l.is_active,
                ph.price, ph.mileage, ph.mileage_raw, ph.price_raw, ph.scraped_at,
                prev.price AS prev_price
            FROM listings l
            JOIN price_history ph ON ph.listing_id = l.id
                AND ph.scraped_at = (
                    SELECT MAX(scraped_at) FROM price_history WHERE listing_id = l.id
                )
            LEFT JOIN price_history prev ON prev.listing_id = l.id
                AND prev.scraped_at = (
                    SELECT MAX(scraped_at) FROM price_history
                    WHERE listing_id = l.id
                    AND scraped_at < ph.scraped_at
                )
            {where_clause}
            ORDER BY ph.price ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_price_history(listing_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT price, mileage, scraped_at FROM price_history WHERE listing_id=? ORDER BY scraped_at ASC",
            (listing_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_market_snapshots() -> list[dict]:
    """Daily avg/min/max for charting."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                DATE(scraped_at) AS date,
                ROUND(AVG(price)) AS avg_price,
                MIN(price) AS min_price,
                MAX(price) AS max_price,
                COUNT(DISTINCT listing_id) AS listing_count
            FROM price_history
            GROUP BY DATE(scraped_at)
            ORDER BY date ASC
        """).fetchall()
        return [dict(r) for r in rows]


def start_run() -> int:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scrape_runs (started_at, status) VALUES (?, 'running')",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def finish_run(run_id: int, stats: dict, error: str = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE scrape_runs SET finished_at=?, listings_found=?, new_listings=?,
               price_changes=?, status=?, error=? WHERE id=?""",
            (
                datetime.now().isoformat(timespec="seconds"),
                stats.get("total", 0),
                stats.get("new", 0),
                stats.get("price_changes", 0),
                "error" if error else "done",
                error,
                run_id,
            ),
        )


def get_recent_runs(n: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_setting(key: str, default=None) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )


def _trigger_mac_notification(title: str, msg: str):
    try:
        cmd = ["osascript", "-e", f'display notification "{msg}" with title "{title}"']
        subprocess.run(cmd, check=False)
    except Exception:
        pass

# Init on import
init_db()
