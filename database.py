"""
SQLite database layer for the DealRadar price tracker.
Stores listings and full price history per listing (keyed by URL).
"""

import sqlite3
import json
import subprocess
import os
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
                variant     TEXT,
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

            CREATE TABLE IF NOT EXISTS pre_approvals (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_name           TEXT NOT NULL,
                date_applied        TEXT NOT NULL,
                amount              REAL,
                interest_rate       REAL,
                monthly_instalment  REAL,
                status              TEXT DEFAULT 'Pending',
                notes               TEXT
            );

            CREATE TABLE IF NOT EXISTS counter_offers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id      INTEGER NOT NULL,
                date            TEXT NOT NULL,
                my_offer        REAL,
                dealer_counter  REAL,
                notes           TEXT,
                status          TEXT,
                FOREIGN KEY (listing_id) REFERENCES listings (id)
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
        try:
            conn.execute("ALTER TABLE listings ADD COLUMN watchlisted INTEGER DEFAULT 0;")
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
                    """INSERT INTO listings (url, title, variant, year, location, dealer, image, first_seen, last_seen, is_active, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (url, item.get("title"), item.get("variant"), item.get("year"), item.get("location"), item.get("dealer"), item.get("image"), now, now, item.get("source", "AutoTrader")),
                )
                listing_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                stats["new"] += 1
            else:
                listing_id = row["id"]
                # Update last_seen and metadata
                conn.execute(
                    """UPDATE listings SET last_seen=?, title=?, variant=?, location=?, dealer=?, image=?, is_active=1, source=COALESCE(?, source)
                       WHERE id=?""",
                    (now, item.get("title"), item.get("variant"), item.get("location"), item.get("dealer"), item.get("image"), item.get("source"), listing_id),
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

            # Record price
            conn.execute(
                """INSERT INTO price_history (listing_id, price, mileage, mileage_raw, price_raw, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (listing_id, price, item.get("mileage"), item.get("mileage_raw"), item.get("price_raw"), now),
            )

            # --- Telegram & Mac Alerts ---
            is_new = (row is None)
            is_price_drop = (last and price < last["price"])
            
            if is_new or is_price_drop:
                title = "New Deal Alert!" if is_new else "Price Drop Alert!"
                msg = f"{item.get('title')} is now R{price:,}"
                if is_new:
                    msg = f"New: {item.get('title')} for R{price:,}"
                
                # Mac notification (if below threshold)
                if alert_threshold > 0 and price <= alert_threshold:
                    _trigger_mac_notification(title, msg)
                
                # Telegram notification (always for new/price drops if configured)
                token = get_setting("telegram_token")
                chat_id = get_setting("telegram_chat_id")
                if token and chat_id:
                    link = item.get("url", "")
                    tele_msg = f"<b>{title}</b>\n{msg}\n{item.get('year', '')} | {item.get('location', '')}\n<a href='{link}'>View Listing</a>"
                    send_telegram_msg(tele_msg)

        # Mark listings not seen in this run as inactive
        urls_seen = [i["url"] for i in listings if i.get("url")]
        if urls_seen:
            placeholders = ",".join("?" * len(urls_seen))
            conn.execute(
                f"UPDATE listings SET is_active=0 WHERE url NOT IN ({placeholders})",
                urls_seen,
            )

    return stats


def toggle_watchlist(listing_id: int) -> bool:
    """Toggle watchlisted status. Returns new state (True = watchlisted)."""
    with get_conn() as conn:
        row = conn.execute("SELECT watchlisted FROM listings WHERE id=?", (listing_id,)).fetchone()
        if row is None:
            return False
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE listings SET watchlisted=? WHERE id=?", (new_val, listing_id))
        return bool(new_val)


def get_listings_with_latest_price(include_inactive: bool = False) -> list[dict]:
    """Returns listings with their latest price and price delta."""
    with get_conn() as conn:
        where_clause = "" if include_inactive else "WHERE l.is_active = 1"
        rows = conn.execute(f"""
            SELECT
                l.id, l.url, l.title, l.variant, l.year, l.location, l.dealer, l.image, l.source,
                l.first_seen, l.last_seen, l.is_active,
                ph.price, ph.mileage, ph.mileage_raw, ph.price_raw, ph.scraped_at,
                first_ph.price AS first_price,
                l.watchlisted
            FROM listings l
            JOIN price_history ph ON ph.listing_id = l.id
                AND ph.scraped_at = (
                    SELECT MAX(scraped_at) FROM price_history WHERE listing_id = l.id
                )
            LEFT JOIN price_history first_ph ON first_ph.listing_id = l.id
                AND first_ph.scraped_at = (
                    SELECT MIN(scraped_at) FROM price_history WHERE listing_id = l.id
                )
            {where_clause}
            ORDER BY ph.price ASC
        """).fetchall()
        return [{
            "id": r[0], "url": r[1], "title": r[2], "variant": r[3], "year": r[4], "location": r[5], "dealer": r[6], "image": r[7], "source": r[8],
            "first_seen": r[9], "last_seen": r[10], "is_active": r[11],
            "price": r[12], "mileage": r[13], "mileage_raw": r[14], "price_raw": r[15], "scraped_at": r[16],
            "prev_price": r[17], "watchlisted": r[18]
        } for r in rows]


def get_price_history(listing_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT price, mileage, scraped_at FROM price_history WHERE listing_id=? ORDER BY scraped_at ASC",
            (listing_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_price_changes() -> list[dict]:
    """All price changes across all listings, newest first, from the very first scrape."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM (
                SELECT
                    l.id        AS listing_id,
                    l.title,
                    l.url,
                    l.year,
                    l.variant,
                    l.source,
                    ph.price    AS new_price,
                    ph.scraped_at,
                    LAG(ph.price) OVER (
                        PARTITION BY ph.listing_id ORDER BY ph.scraped_at
                    ) AS old_price
                FROM price_history ph
                JOIN listings l ON l.id = ph.listing_id
            )
            WHERE old_price IS NOT NULL AND new_price != old_price
            ORDER BY scraped_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


        return [dict(r) for r in rows]


def get_day_of_week_prices() -> list[dict]:
    """Avg price by day of week (0=Sun, 1=Mon, ..., 6=Sat) across all price history."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                CAST(strftime('%w', scraped_at) AS INTEGER) AS dow,
                ROUND(AVG(price)) AS avg_price,
                COUNT(*) AS count
            FROM price_history
            GROUP BY dow
            ORDER BY dow ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_week_of_month_prices() -> list[dict]:
    """Avg price by week of month (1, 2, 3, 4) based on day of month."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                CASE 
                    WHEN CAST(strftime('%d', scraped_at) AS INTEGER) <= 7 THEN 1
                    WHEN CAST(strftime('%d', scraped_at) AS INTEGER) <= 14 THEN 2
                    WHEN CAST(strftime('%d', scraped_at) AS INTEGER) <= 21 THEN 3
                    ELSE 4
                END AS wom,
                ROUND(AVG(price)) AS avg_price,
                COUNT(*) AS count
            FROM price_history
            GROUP BY wom
            ORDER BY wom ASC
        """).fetchall()
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


# Secrets are stored in .env for security, others in tracker.db
ENV_SECRETS = {"telegram_token", "telegram_chat_id", "admin_password"}
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def _read_env():
    """Simple parser for .env file."""
    if not os.path.exists(ENV_FILE):
        return {}
    secrets = {}
    try:
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    secrets[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        print(f"Error reading .env: {e}")
    return secrets

def _write_env(key, value):
    """Write or update a key in .env file."""
    secrets = _read_env()
    secrets[key] = str(value)
    try:
        with open(ENV_FILE, "w") as f:
            for k, v in secrets.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        print(f"Error writing to .env: {e}")


def get_setting(key: str, default=None) -> str:
    # Check .env first for secrets
    if key in ENV_SECRETS:
        env_val = _read_env().get(key)
        if env_val is not None:
            return env_val
            
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    # Store secrets in .env
    if key in ENV_SECRETS:
        _write_env(key, value)
        # Also remove from DB if it exists there to avoid leaking
        with get_conn() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        return

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value),
        )


def send_telegram_msg(message: str, token_override=None, chat_id_override=None):
    """Send a message to Telegram using Bot API."""
    import urllib.request
    import urllib.parse
    import json
    
    token = token_override or get_setting("telegram_token")
    chat_id = chat_id_override or get_setting("telegram_chat_id")
    if not token or not chat_id:
        return None
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            error_data = json.loads(error_body)
            print(f"Telegram API Error: {error_data.get('description')}")
            return {"error": error_data.get("description")}
        except:
            print(f"Telegram HTTP Error {e.code}: {error_body}")
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        print(f"Telegram Connection Error: {e}")
        return {"error": str(e)}


def get_telegram_updates(offset=None):
    """Fetch recent messages from Telegram Bot API."""
    import urllib.request
    import json
    
    token = get_setting("telegram_token")
    if not token:
        return []
        
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=30"
    if offset:
        url += f"&offset={offset}"
        
    try:
        with urllib.request.urlopen(url, timeout=35) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", [])
    except Exception as e:
        print(f"Telegram Polling Error: {e}")
    return []


def _trigger_mac_notification(title: str, msg: str):
    try:
        cmd = ["osascript", "-e", f'display notification "{msg}" with title "{title}"']
        subprocess.run(cmd, check=False)
    except Exception:
        pass


def get_pre_approvals() -> list[dict]:
    """Fetch all bank pre-approval applications."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pre_approvals ORDER BY date_applied DESC").fetchall()
        return [dict(r) for r in rows]


def add_pre_approval(data: dict) -> int:
    """Add a new bank pre-approval application."""
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO pre_approvals (bank_name, date_applied, amount, interest_rate, monthly_instalment, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("bank_name"),
                data.get("date_applied"),
                data.get("amount"),
                data.get("interest_rate"),
                data.get("monthly_instalment"),
                data.get("status", "Pending"),
                data.get("notes")
            )
        )
        return cursor.lastrowid


def update_pre_approval(app_id: int, data: dict) -> bool:
    """Update an existing pre-approval application."""
    with get_conn() as conn:
        cursor = conn.execute(
            """UPDATE pre_approvals 
               SET bank_name=?, date_applied=?, amount=?, interest_rate=?, monthly_instalment=?, status=?, notes=?
               WHERE id=?""",
            (
                data.get("bank_name"),
                data.get("date_applied"),
                data.get("amount"),
                data.get("interest_rate"),
                data.get("monthly_instalment"),
                data.get("status"),
                data.get("notes"),
                app_id
            )
        )
        return cursor.rowcount > 0


def get_sold_listings_with_estimates():
    """Find inactive listings and estimate their sold price."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                l.*, 
                (SELECT price FROM price_history ph WHERE ph.listing_id = l.id ORDER BY scraped_at DESC LIMIT 1) as last_price,
                (SELECT COUNT(*) FROM price_history ph WHERE ph.listing_id = l.id AND ph.price < (SELECT price FROM price_history ph2 WHERE ph2.listing_id = ph.listing_id AND ph2.scraped_at < ph.scraped_at ORDER BY ph2.scraped_at DESC LIMIT 1)) as drop_count
            FROM listings l
            WHERE l.is_active = 0
            ORDER BY l.last_seen DESC
            LIMIT 50
        """).fetchall()
        
        results = []
        for r in rows:
            d = dict(r)
            # Days on Market
            first = datetime.fromisoformat(d['first_seen'])
            last = datetime.fromisoformat(d['last_seen'])
            dom = max(1, (last - first).days)
            
            # Estimate Formula: Last Price * (1 - (Days on Market / 1000) - (Drops * 0.01))
            # Capped at 10% discount
            last_price = d['last_price'] or 0
            drop_count = d['drop_count'] or 0
            discount = min(0.10, (dom / 1000) + (drop_count * 0.01))
            d['estimated_sold_price'] = round(last_price * (1 - discount))
            d['dom'] = dom
            results.append(d)
        return results


def get_variant_stats():
    """Aggregate stats by variant."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                l.variant, 
                ROUND(AVG(ph.price)) as avg_price,
                COUNT(DISTINCT l.id) as count,
                MIN(ph.price) as min_price,
                MAX(ph.price) as max_price
            FROM listings l
            JOIN price_history ph ON l.id = ph.listing_id
            WHERE l.is_active = 1 AND l.variant IS NOT NULL AND l.variant != ''
            GROUP BY l.variant
            HAVING count > 1
            ORDER BY avg_price ASC
        """).fetchall()
        return [dict(r) for r in rows]


def delete_pre_approval(app_id: int) -> bool:
    """Delete a pre-approval application."""
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM pre_approvals WHERE id=?", (app_id,))
        return cursor.rowcount > 0


# ─── COUNTER OFFERS ────────────────────────────────────────────────────────
def get_counter_offers(listing_id: int):
    """Fetch all counter offers for a specific listing."""
    with get_conn() as conn:
        cursor = conn.execute("SELECT * FROM counter_offers WHERE listing_id = ? ORDER BY date ASC, id ASC", (listing_id,))
        return [dict(row) for row in cursor.fetchall()]


def add_counter_offer(listing_id, date, my_offer, dealer_counter, notes, status):
    """Log a new offer or counter offer."""
    with get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO counter_offers (listing_id, date, my_offer, dealer_counter, notes, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (listing_id, date, my_offer, dealer_counter, notes, status))
        return cursor.lastrowid


def delete_counter_offer(offer_id: int):
    """Delete a specific offer entry."""
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM counter_offers WHERE id = ?", (offer_id,))
        return cursor.rowcount > 0


# Init on import
init_db()
