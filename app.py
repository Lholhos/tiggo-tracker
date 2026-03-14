"""
Flask backend for the DealRadar price tracker.
Runs on http://localhost:5000
"""

import threading
import queue
import time
import schedule
from flask import Flask, jsonify, render_template_string, request
from scraper import scrape, scrape_single_url
from database import (
    upsert_listings,
    get_listings_with_latest_price,
    get_price_history,
    get_market_snapshots,
    get_day_of_week_prices,
    get_recent_runs,
    start_run,
    finish_run,
    get_setting,
    set_setting,
    toggle_watchlist,
)

app = Flask(__name__)

# Global scrape state
_scrape_lock = threading.Lock()
_scrape_status = {"running": False, "log": [], "run_id": None}


def scheduled_job():
    with _scrape_lock:
        if _scrape_status["running"]:
            return
        _scrape_status["running"] = True
        _scrape_status["log"] = ["Starting scheduled daily scrape..."]

    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()


def start_scheduler():
    schedule.every().day.at("09:00").do(scheduled_job)
    
    def run_loop():
        while True:
            schedule.run_pending()
            time.sleep(60)
            
    threading.Thread(target=run_loop, daemon=True).start()

start_scheduler()


def _do_scrape():
    run_id = start_run()
    _scrape_status["run_id"] = run_id
    _scrape_status["log"] = []
    error = None
    listings = []

    def log(msg):
        _scrape_status["log"].append(msg)

    try:
        wbc_url = get_setting("wbc_url", "") or None
        listings = scrape(max_pages=10, headless=True, status_callback=log, wbc_url=wbc_url)
        stats = upsert_listings(listings)
        stats["total"] = len(listings)
        finish_run(run_id, stats)
        log(f"✓ Done — {len(listings)} listings, {stats['new']} new, {stats['price_changes']} price changes")
    except Exception as e:
        error = str(e)
        log(f"✗ Error: {error}")
        finish_run(run_id, {"total": len(listings), "new": 0, "price_changes": 0}, error=error)
    finally:
        _scrape_status["running"] = False


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/scrape", methods=["POST"])
def trigger_scrape():
    with _scrape_lock:
        if _scrape_status["running"]:
            return jsonify({"error": "Scrape already in progress"}), 409
        _scrape_status["running"] = True
        _scrape_status["log"] = ["Starting scrape..."]

    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/scrape/url", methods=["POST"])
def trigger_scrape_url():
    data = request.json or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
        
    with _scrape_lock:
        if _scrape_status["running"]:
            return jsonify({"error": "Scrape already in progress"}), 409
        _scrape_status["running"] = True
        _scrape_status["log"] = [f"Scraping single URL: {url}"]

    def _do_single():
        run_id = start_run()
        _scrape_status["run_id"] = run_id
        error = None
        listings = []
        def log(msg): _scrape_status["log"].append(msg)
        
        try:
            listings = scrape_single_url(url, headless=True, status_callback=log)
            if not listings:
                raise Exception("Failed to extract data from URL")
            stats = upsert_listings(listings)
            stats["total"] = len(listings)
            finish_run(run_id, stats)
            log(f"✓ Done — 1 listing processed")
        except Exception as e:
            error = str(e)
            log(f"✗ Error: {error}")
            finish_run(run_id, {"total": 0, "new": 0, "price_changes": 0}, error=error)
        finally:
            _scrape_status["running"] = False

    t = threading.Thread(target=_do_single, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/scrape/status")
def scrape_status():
    return jsonify({
        "running": _scrape_status["running"],
        "log": _scrape_status["log"][-50:],  # last 50 lines
    })


@app.route("/api/listings")
def listings():
    include_inactive = request.args.get("include_inactive", "0") == "1"
    data = get_listings_with_latest_price(include_inactive=include_inactive)
    return jsonify(data)

@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "POST":
        data = request.json or {}
        if "price_alert" in data:
            set_setting("price_alert", str(data["price_alert"]))
        if "wbc_url" in data:
            set_setting("wbc_url", str(data["wbc_url"]))
        return jsonify({"ok": True})
    return jsonify({"price_alert": get_setting("price_alert", ""), "wbc_url": get_setting("wbc_url", "")})


@app.route("/api/listings/<int:listing_id>/history")
def listing_history(listing_id):
    data = get_price_history(listing_id)
    return jsonify(data)


@app.route("/api/market")
def market():
    snapshots = get_market_snapshots()
    # Calculate 30-day velocity (avg price change per month)
    velocity = None
    if len(snapshots) >= 2:
        oldest = snapshots[0]["avg_price"]
        newest = snapshots[-1]["avg_price"]
        days = len(snapshots)
        velocity = round((newest - oldest) / days * 30) if days else 0
    return jsonify({"chart": snapshots, "velocity_30d": velocity})


@app.route("/api/analytics")
def analytics():
    from datetime import datetime, timedelta
    snapshots = get_market_snapshots()
    dow_prices = get_day_of_week_prices()

    # Linear regression on daily avg prices → 30-day forecast
    forecast = []
    slope = None
    if len(snapshots) >= 3:
        n = len(snapshots)
        xs = list(range(n))
        ys = [s["avg_price"] for s in snapshots]
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)
        denom = n * sum_xx - sum_x ** 2
        if denom:
            slope = (n * sum_xy - sum_x * sum_y) / denom
            intercept = (sum_y - slope * sum_x) / n
            last_date = datetime.fromisoformat(snapshots[-1]["date"])
            for i in range(1, 31):
                fx = n - 1 + i
                forecast.append({
                    "date": (last_date + timedelta(days=i)).strftime("%Y-%m-%d"),
                    "projected_price": round(intercept + slope * fx),
                })

    return jsonify({
        "forecast": forecast,
        "dow": dow_prices,
        "slope": round(slope, 2) if slope is not None else None,
    })


@app.route("/api/runs")
def runs():
    return jsonify(get_recent_runs())


@app.route("/api/listings/<int:listing_id>/watchlist", methods=["POST"])
def toggle_watchlist_route(listing_id):
    new_state = toggle_watchlist(listing_id)
    return jsonify({"watchlisted": new_state})


# ---------------------------------------------------------------------------
# Frontend HTML (single-file, self-contained)
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealRadar</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

  :root {
    --bg: #080b10;
    --surface: #0d1117;
    --border: #1c2333;
    --border2: #2a3347;
    --gold: #d4a843;
    --gold2: #f0c060;
    --green: #3fb950;
    --red: #f85149;
    --text: #e6edf3;
    --muted: #7d8590;
    --dim: #3d444d;
  }

  [data-theme="light"] {
    --bg: #f6f8fa;
    --surface: #ffffff;
    --border: #d0d7de;
    --border2: #b0bac4;
    --gold: #9a6f00;
    --gold2: #c98a00;
    --green: #1a7f37;
    --red: #cf222e;
    --text: #1f2328;
    --muted: #57606a;
    --dim: #8c959f;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
    min-height: 100vh;
  }

  /* HEADER */
  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 64px;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header-brand {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .header-brand .wordmark {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 15px;
    font-weight: 700;
    color: var(--text);
    letter-spacing: 1px;
  }
  .header-brand .subtitle {
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .header-badge {
    background: rgba(212,168,67,0.12);
    border: 1px solid rgba(212,168,67,0.3);
    color: var(--gold);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    padding: 3px 10px;
    letter-spacing: 2px;
  }

  /* STATS BAR */
  .statsbar {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .stat {
    padding: 16px 24px;
    border-right: 1px solid var(--border);
  }
  .stat:last-child { border-right: none; }
  .stat-label {
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 6px;
    font-family: 'IBM Plex Mono', monospace;
  }
  .stat-value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 20px;
    font-weight: 700;
    color: var(--text);
  }
  .stat-value.gold { color: var(--gold); }
  .stat-value.green { color: var(--green); }
  .stat-value.red { color: var(--red); }

  /* MAIN LAYOUT */
  .main { padding: 28px 32px; max-width: 1400px; margin: 0 auto; }

  /* SCRAPE PANEL */
  .scrape-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 20px 24px;
    margin-bottom: 24px;
    display: flex;
    align-items: flex-start;
    gap: 24px;
  }
  .scrape-btn {
    background: var(--gold);
    color: #080b10;
    border: none;
    padding: 10px 24px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
    transition: background 0.2s;
  }
  .scrape-btn:hover { background: var(--gold2); }
  .scrape-btn:disabled { background: var(--dim); color: var(--muted); cursor: not-allowed; }
  .scrape-log {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.8;
    flex: 1;
    max-height: 80px;
    overflow-y: auto;
  }
  .scrape-log .line-ok { color: var(--green); }
  .scrape-log .line-err { color: var(--red); }
  .scrape-log .line-info { color: var(--gold); }

  /* TABS */
  .tabs {
    display: flex;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
    gap: 0;
  }
  .tab-btn {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    padding: 10px 20px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    cursor: pointer;
    transition: all 0.15s;
  }
  .tab-btn.active {
    color: var(--gold);
    border-bottom-color: var(--gold);
  }
  .tab-btn:hover:not(.active) { color: var(--text); }

  /* FILTERS */
  .filters {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
    align-items: center;
  }
  .filter-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .filter-btn {
    background: none;
    border: 1px solid var(--border2);
    color: var(--muted);
    padding: 4px 12px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .filter-btn.active {
    border-color: var(--gold);
    color: var(--gold);
    background: rgba(212,168,67,0.08);
  }

  /* TABLE */
  .table-wrap { overflow-x: auto; }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  thead th {
    text-align: left;
    padding: 10px 14px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 2px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }
  thead th:hover { color: var(--text); }
  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background 0.1s;
  }
  tbody tr:hover { background: rgba(212,168,67,0.04); }
  tbody td { padding: 12px 14px; vertical-align: middle; }

  .price-cell {
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 700;
    font-size: 14px;
  }
  .price-delta {
    font-size: 10px;
    font-family: 'IBM Plex Mono', monospace;
    margin-top: 2px;
  }
  .price-delta.down { color: var(--green); }
  .price-delta.up { color: var(--red); }

  .vs-avg {
    font-size: 10px;
    font-family: 'IBM Plex Mono', monospace;
    padding: 2px 6px;
    border-radius: 2px;
  }
  .vs-avg.below { background: rgba(63,185,80,0.12); color: var(--green); }
  .vs-avg.above { background: rgba(248,81,73,0.12); color: var(--red); }
  .vs-avg.at { background: rgba(125,133,144,0.12); color: var(--muted); }

  .listing-title a {
    color: var(--text);
    text-decoration: none;
    font-weight: 500;
  }
  .listing-title a:hover { color: var(--gold); }
  .listing-meta {
    font-size: 11px;
    color: var(--muted);
    margin-top: 3px;
    display: flex;
    gap: 12px;
  }

  .rank-num {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--dim);
    text-align: center;
  }

  .history-btn {
    background: none;
    border: 1px solid var(--border2);
    color: var(--muted);
    padding: 3px 8px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    cursor: pointer;
    letter-spacing: 1px;
  }
  .history-btn:hover { border-color: var(--gold); color: var(--gold); }

  /* CHART SECTION */
  .chart-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 20px;
  }
  .chart-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-bottom: 16px;
  }

  /* COMPARE BUTTON */
  .cmp-btn {
    background: none;
    border: 1px solid var(--border2);
    color: var(--dim);
    padding: 2px 6px;
    font-size: 14px;
    cursor: pointer;
    border-radius: 3px;
    line-height: 1;
    transition: all 0.15s;
    font-family: 'IBM Plex Mono', monospace;
  }
  .cmp-btn:hover { border-color: var(--gold); color: var(--gold); }
  .cmp-btn.in-compare { background: rgba(212,168,67,0.12); border-color: var(--gold); color: var(--gold); }

  /* COMPARE BAR */
  .compare-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: var(--surface);
    border-top: 1px solid var(--border2);
    padding: 12px 32px;
    display: flex;
    align-items: center;
    gap: 16px;
    z-index: 150;
    transform: translateY(100%);
    transition: transform 0.2s ease;
  }
  .compare-bar.visible { transform: translateY(0); }
  .cmp-thumb-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
  }
  .cmp-chip {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--bg);
    border: 1px solid var(--border2);
    padding: 6px 10px;
    max-width: 240px;
  }
  .cmp-chip img { width: 48px; height: 32px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .cmp-chip-title { font-size: 11px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cmp-chip-price { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--gold); white-space: nowrap; }
  .cmp-chip-remove { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 14px; padding: 0 2px; line-height: 1; }
  .cmp-chip-remove:hover { color: var(--red); }
  .cmp-slot {
    width: 180px; height: 44px;
    border: 1px dashed var(--border2);
    display: flex; align-items: center; justify-content: center;
    color: var(--dim); font-size: 11px; font-family: 'IBM Plex Mono', monospace;
    letter-spacing: 1px;
  }

  /* COMPARE MODAL */
  .cmp-modal-overlay {
    position: fixed; inset: 0;
    background: rgba(8,11,16,0.9);
    display: none; align-items: flex-start; justify-content: center;
    z-index: 300;
    overflow-y: auto;
    padding: 32px 16px;
  }
  .cmp-modal-overlay.open { display: flex; }
  .cmp-modal {
    background: var(--surface);
    border: 1px solid var(--border2);
    padding: 28px;
    width: 100%;
    max-width: 960px;
  }
  .cmp-grid {
    display: grid;
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    margin-top: 20px;
  }
  .cmp-row {
    display: grid;
    background: var(--surface);
    min-height: 40px;
  }
  .cmp-row.header-row { background: var(--bg); }
  .cmp-cell {
    padding: 10px 14px;
    font-size: 12px;
    border-right: 1px solid var(--border);
    display: flex;
    align-items: center;
  }
  .cmp-cell:last-child { border-right: none; }
  .cmp-cell.row-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 1px;
    text-transform: uppercase;
    background: var(--bg);
  }
  .cmp-cell.best {
    color: var(--green);
    font-weight: 700;
  }
  .cmp-cell.worst { color: var(--red); }
  .cmp-header-cell {
    padding: 12px 14px;
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .cmp-header-cell:last-child { border-right: none; }

  /* MODAL */
  .modal-overlay {
    position: fixed; inset: 0;
    background: rgba(8,11,16,0.85);
    display: flex; align-items: center; justify-content: center;
    z-index: 200;
    display: none;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface);
    border: 1px solid var(--border2);
    padding: 28px;
    width: 560px;
    max-width: 95vw;
    max-height: 80vh;
    overflow-y: auto;
  }
  .modal-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    color: var(--gold);
    margin-bottom: 4px;
  }
  .modal-subtitle {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 20px;
  }
  .modal-close {
    background: none;
    border: 1px solid var(--border2);
    color: var(--muted);
    padding: 6px 14px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    cursor: pointer;
    margin-top: 20px;
  }
  .sortable {
    cursor: pointer;
    user-select: none;
  }
  .sortable:hover {
    color: var(--text);
  }
  .sort-icon {
    font-size: 10px;
    margin-left: 4px;
    color: var(--gold);
  }

  /* RUNS */
  .run-row {
    display: grid;
    grid-template-columns: 1fr 80px 80px 80px 80px 80px;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    align-items: center;
  }
  .run-row:last-child { border-bottom: none; }
  .status-dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    margin-right: 6px;
  }
  .status-dot.done { background: var(--green); }
  .status-dot.error { background: var(--red); }
  .status-dot.running { background: var(--gold); animation: pulse 1s infinite; }

  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .empty-state {
    text-align: center;
    padding: 80px 0;
    color: var(--dim);
  }
  .empty-state .icon { font-size: 40px; margin-bottom: 16px; }
  .empty-state .msg { font-family: 'IBM Plex Mono', monospace; font-size: 12px; letter-spacing: 2px; text-transform: uppercase; }
  .empty-state .sub { font-size: 12px; color: var(--muted); margin-top: 8px; }
  
  .age-old { font-weight: bold; color: var(--gold); }

  /* STAR / WATCHLIST */
  .star-btn {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 16px;
    color: var(--dim);
    padding: 2px 4px;
    line-height: 1;
    transition: color 0.15s, transform 0.1s;
  }
  .star-btn:hover { color: var(--gold); transform: scale(1.2); }
  .star-btn.starred { color: var(--gold); }

  /* COMPACT MODE */
  body.compact tbody td { padding: 5px 14px; }
  body.compact .listing-thumb { display: none !important; }
  body.compact .listing-meta { display: none; }
  body.compact .scrape-log { max-height: 40px; }

  /* THEME TOGGLE & COMPACT TOGGLE */
  .icon-btn {
    background: none;
    border: 1px solid var(--border2);
    color: var(--muted);
    padding: 5px 10px;
    font-size: 14px;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.15s;
    line-height: 1;
  }
  .icon-btn:hover { border-color: var(--gold); color: var(--gold); }

  /* SEARCH PROFILES */
  .profiles-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
  }
  .profiles-row select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border2);
    padding: 3px 8px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    outline: none;
  }
  .profiles-row .prof-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 1px;
    text-transform: uppercase;
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-brand">
    <span class="wordmark">DEALRADAR</span>
    <span class="subtitle">Price Intelligence · 2022–2024</span>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <span id="velocity-badge" class="header-badge" style="display:none;font-weight:bold"></span>
    <button class="icon-btn" id="compact-btn" onclick="toggleCompact()" title="Toggle compact view">⊟</button>
    <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle light/dark mode">☀</button>
  </div>
</div>

<!-- Stats Bar -->
<div class="statsbar" id="statsbar">
  <div class="stat"><div class="stat-label">Listings</div><div class="stat-value" id="stat-count">—</div></div>
  <div class="stat"><div class="stat-label">Lowest</div><div class="stat-value green" id="stat-min">—</div></div>
  <div class="stat"><div class="stat-label">Average</div><div class="stat-value gold" id="stat-avg">—</div></div>
  <div class="stat"><div class="stat-label">Highest</div><div class="stat-value red" id="stat-max">—</div></div>
  <div class="stat"><div class="stat-label">Best Day (New)</div><div class="stat-value" style="font-size:13px;color:var(--text)" id="stat-best-new">—</div></div>
  <div class="stat"><div class="stat-label">Best Day (Drop)</div><div class="stat-value" style="font-size:13px;color:var(--green)" id="stat-best-drop">—</div></div>
  <div class="stat" style="display:none"><div class="stat-label">Last Scraped</div><div class="stat-value" style="font-size:13px;color:var(--muted)" id="stat-last">Never</div></div>
</div>

<div id="market-insights" style="margin:0 auto 16px auto; max-width:1200px; padding:0 24px; font-size:12px; color:var(--muted); display:flex; justify-content:flex-end; gap:8px; flex-wrap:wrap;">
  <span id="dup-insight" style="background:var(--surface); padding:4px 10px; border-radius:4px; border:1px solid var(--border); display:none; cursor:pointer" onclick="setStatus('duplicates');document.getElementById('filter-status').value='duplicates'">
    <span style="color:var(--gold)">⚡</span> <span id="dup-insight-text"></span>
  </span>
  <span id="mileage-sweet-spot" style="background:var(--surface); padding:4px 10px; border-radius:4px; border:1px solid var(--border); display:none">
    <i class="icon" style="color:var(--gold)">⚡</i> <span id="mss-text"></span>
  </span>
</div>

<div class="main">

  <!-- Scrape Panel -->
  <div class="scrape-panel">
    <div style="display:flex;gap:8px">
      <button class="scrape-btn" id="scrape-btn" onclick="triggerScrape()">▶ Scrape Now</button>
      <input type="text" id="manual-url" placeholder="Paste single AutoTrader URL..." style="padding:8px 12px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:6px;width:320px;font-family:inherit;font-size:13px;outline:none">
      <button class="scrape-btn" id="scrape-url-btn" onclick="triggerScrapeUrl()" style="background:var(--surface);border:1px solid var(--border);color:var(--text)">Add URL</button>
      
      <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
        <span style="font-size:12px;color:var(--muted)">WBC URL:</span>
        <input type="text" id="wbc-url" placeholder="WeBuyCars search URL..." style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:220px;font-family:inherit;font-size:12px;outline:none">
        <span style="font-size:12px;color:var(--muted)">Alert:</span>
        <input type="number" id="alert-price" placeholder="R320000" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:90px;font-family:inherit;font-size:12px;outline:none">
        <button onclick="saveSettings()" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer;font-size:12px">Save</button>
      </div>
    </div>
    <div class="scrape-log" id="scrape-log">
      <span style="color:var(--dim)">No scrape running. Click to fetch latest listings from AutoTrader.</span>
    </div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab-btn active" onclick="setTab('listings', this)">Listings</button>
    <button class="tab-btn" onclick="setTab('dealers', this)">Dealers</button>
    <button class="tab-btn" onclick="setTab('charts', this)">Charts</button>
    <button class="tab-btn" onclick="setTab('runs', this)">Scrape History</button>
  </div>

  <!-- Listings Tab -->
  <div id="tab-listings">
    <div class="filters">
      <span class="filter-label">Year:</span>
      <button class="filter-btn active" onclick="setYear('all', this)">All</button>
      <button class="filter-btn" onclick="setYear('2022', this)">2022</button>
      <button class="filter-btn" onclick="setYear('2023', this)">2023</button>
      <button class="filter-btn" onclick="setYear('2024', this)">2024</button>
      <span class="filter-label" style="margin-left:16px">Location:</span>
      <select id="filter-location" onchange="setLocation(this.value)" style="background:var(--surface);color:var(--text);border:1px solid var(--border);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:12px;outline:none">
        <option value="all">All</option>
      </select>
      <span class="filter-label" style="margin-left:16px">Status:</span>
      <select id="filter-status" onchange="setStatus(this.value)" style="background:var(--surface);color:var(--text);border:1px solid var(--border);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:12px;outline:none">
        <option value="active">Active</option>
        <option value="gone">Gone (Sold)</option>
        <option value="watchlisted">Watchlisted ★</option>
        <option value="duplicates">Duplicates ⚡</option>
        <option value="all">Any</option>
      </select>
    </div>
    <!-- Search Profiles -->
    <div class="profiles-row">
      <span class="prof-label">Profiles:</span>
      <select id="profiles-select" onchange="loadProfile(this.value)" style="min-width:160px">
        <option value="">-- saved profiles --</option>
      </select>
      <button class="filter-btn" onclick="saveProfile()">+ Save Current</button>
      <button class="filter-btn" onclick="deleteProfile()" style="color:var(--red);border-color:var(--red)">✕ Delete</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:28px">★</th>
            <th style="width:32px" title="Add to compare">⊕</th>
            <th style="width:36px">#</th>
            <th class="sortable" onclick="setSort('title')">Listing <span class="sort-icon" id="sort-icon-title"></span></th>
            <th class="sortable" onclick="setSort('model')">Model <span class="sort-icon" id="sort-icon-model"></span></th>
            <th class="sortable" onclick="setSort('source')">Source <span class="sort-icon" id="sort-icon-source"></span></th>
            <th class="sortable" onclick="setSort('price')">Price <span class="sort-icon" id="sort-icon-price"></span></th>
            <th class="sortable" onclick="setSort('change')">Drop <span class="sort-icon" id="sort-icon-change"></span></th>
            <th class="sortable" onclick="setSort('score')">Score <span class="sort-icon" id="sort-icon-score"></span></th>
            <th style="width:85px">Neg. Gap</th>
            <th class="sortable" onclick="setSort('mileage')">Mileage <span class="sort-icon" id="sort-icon-mileage"></span></th>
            <th class="sortable" onclick="setSort('year')">Year <span class="sort-icon" id="sort-icon-year"></span></th>
            <th class="sortable" onclick="setSort('location')">Location & Dealer <span class="sort-icon" id="sort-icon-location"></span></th>
            <th class="sortable" onclick="setSort('status')">Status / Time to Sell <span class="sort-icon" id="sort-icon-status"></span></th>
            <th></th>
          </tr>
        </thead>
        <tbody id="listings-tbody">
          <tr><td colspan="15" class="empty-state" style="padding:60px">
            <div class="icon">🚗</div>
            <div class="msg">No data yet</div>
            <div class="sub">Run a scrape to fetch listings</div>
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Charts Tab -->
  <div id="tab-charts" style="display:none">
    <div class="chart-grid">
      <div class="chart-card" style="grid-column:1/-1">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Market Average Price · Historical + 30-Day Forecast</div>
          <div id="forecast-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:3px 10px;border-radius:3px;border:1px solid"></div>
        </div>
        <canvas id="chart-forecast" height="120"></canvas>
      </div>
      <div class="chart-card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Best Time to Buy · Avg Price by Day</div>
          <div id="best-day-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:3px 10px;border-radius:3px;border:1px solid var(--border2);color:var(--green)"></div>
        </div>
        <canvas id="chart-dow" height="200"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-title">Price vs Mileage · Current Listings</div>
        <canvas id="chart-scatter" height="200"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-title">Price Distribution</div>
        <canvas id="chart-dist" height="200"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-title">Listings by Year</div>
        <canvas id="chart-year" height="200"></canvas>
      </div>
      <div class="chart-card" style="grid-column:1/-1">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Market Heat Map · Avg Price by Location</div>
          <div id="heatmap-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:3px 10px;border-radius:3px;border:1px solid var(--border2);color:var(--green)"></div>
        </div>
        <canvas id="chart-heatmap" height="80"></canvas>
      </div>
    </div>
  </div>

  <!-- Runs Tab -->
  <div id="tab-runs" style="display:none">
    <div style="background:var(--surface);border:1px solid var(--border);padding:20px">
      <div class="run-row" style="font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600">
        <div>STARTED</div><div>STATUS</div><div>FOUND</div><div>NEW</div><div>CHANGES</div><div>DURATION</div>
      </div>
      <div id="runs-list"><div style="color:var(--dim);font-family:monospace;font-size:12px;padding:20px 0">No runs yet</div></div>
    </div>
  </div>

  <!-- Dealers Tab -->
  <div id="tab-dealers" style="display:none">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th style="width:36px">#</th>
            <th>Dealer Name</th>
            <th style="text-align:right">Active Inv.</th>
            <th style="text-align:right">Price Drops</th>
            <th style="text-align:right">Avg. Deviation</th>
            <th style="text-align:center">Reputation Score</th>
          </tr>
        </thead>
        <tbody id="dealers-tbody"></tbody>
      </table>
    </div>
  </div>

</div>

<!-- Compare Bar -->
<div class="compare-bar" id="compare-bar">
  <div class="cmp-thumb-wrap" id="cmp-chips"></div>
  <button onclick="openCompare()" style="background:var(--gold);color:#080b10;border:none;padding:8px 20px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700;letter-spacing:2px;cursor:pointer;white-space:nowrap" id="cmp-go-btn">COMPARE (0)</button>
  <button onclick="clearCompare()" style="background:none;border:1px solid var(--border2);color:var(--muted);padding:8px 14px;font-family:'IBM Plex Mono',monospace;font-size:11px;cursor:pointer">Clear</button>
</div>

<!-- Compare Modal -->
<div class="cmp-modal-overlay" id="cmp-modal">
  <div class="cmp-modal">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div style="font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--gold)">Side-by-Side Comparison</div>
      <button onclick="closeCompare()" style="background:none;border:1px solid var(--border2);color:var(--muted);padding:4px 12px;font-family:'IBM Plex Mono',monospace;font-size:11px;cursor:pointer">✕ Close</button>
    </div>
    <div id="cmp-content"></div>
  </div>
</div>

<!-- History Modal -->
<div class="modal-overlay" id="modal">
  <div class="modal" style="width:600px">
    <div class="modal-title" id="modal-title">Price History</div>
    <div class="modal-subtitle" id="modal-subtitle"></div>
    <canvas id="chart-history" height="180"></canvas>
    
    <!-- Similar Deals -->
    <div id="similar-listings-wrap" style="margin-top:24px;display:none;">
      <div style="font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px">Similar Deals</div>
      <div id="similar-listings" style="display:flex;flex-direction:column;gap:8px"></div>
    </div>
    
    <!-- Negotiation Helper -->
    <div id="neg-helper-wrap" style="margin-top:24px;display:none">
      <div style="font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px">Negotiation Helper</div>
      <div id="neg-helper-content"></div>
    </div>

    <!-- Ownership Calculator -->
    <div id="ownership-wrap" style="margin-top:24px;display:none">
      <div style="font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px">Total Ownership Cost</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px">
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">DEPOSIT (R)</div>
          <input id="oc-deposit" type="number" value="50000" oninput="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">LOAN TERM</div>
          <select id="oc-term" onchange="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
            <option value="48">48 months</option>
            <option value="60" selected>60 months</option>
            <option value="72">72 months</option>
            <option value="84">84 months</option>
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">INTEREST RATE (%)</div>
          <input id="oc-rate" type="number" value="12.5" step="0.25" oninput="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">INSURANCE / MO (R)</div>
          <input id="oc-insurance" type="number" value="1200" oninput="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">FUEL / MO (R)</div>
          <input id="oc-fuel" type="number" value="2500" oninput="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:4px;font-family:monospace">MAINTENANCE / YR (R)</div>
          <input id="oc-maint" type="number" value="6000" oninput="calcOwnership()" style="width:100%;padding:6px 8px;background:var(--bg);border:1px solid var(--border2);color:var(--text);font-family:monospace;font-size:12px;outline:none">
        </div>
      </div>
      <div id="oc-results"></div>
    </div>

    <button class="modal-close" style="margin-top:24px" onclick="closeModal()">Close</button>
  </div>
</div>

<script>
let allListings = [];
let filterYear = 'all';
let filterLocation = 'all';
let filterStatus = 'active';
let sortKey = 'change';
let sortAsc = false;

function setLocation(val) {
  filterLocation = val;
  renderTable();
}
function setStatus(val) {
  filterStatus = val;
  renderTable();
}

async function loadSettings() {
  const res = await fetch('/api/settings');
  const d = await res.json();
  if (d.price_alert) document.getElementById('alert-price').value = d.price_alert;
  if (d.wbc_url) document.getElementById('wbc-url').value = d.wbc_url;
}
async function saveSettings() {
  const price_alert = document.getElementById('alert-price').value;
  const wbc_url = document.getElementById('wbc-url').value.trim();
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({price_alert, wbc_url})
  });
  alert('Settings saved!');
}
loadSettings();

let charts = {};

const fmt = (n) => n ? 'R\u00a0' + Number(n).toLocaleString('en-ZA') : '—';
const fmtDate = (s) => s ? s.slice(0,16).replace('T',' ') : '—';

// ─── TABS ──────────────────────────────────────────────────────────────────
function setTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-listings').style.display = name === 'listings' ? 'block' : 'none';
  document.getElementById('tab-charts').style.display = name === 'charts' ? 'block' : 'none';
  document.getElementById('tab-runs').style.display = name === 'runs' ? 'block' : 'none';
  document.getElementById('tab-dealers').style.display = name === 'dealers' ? 'block' : 'none';
  if (name === 'charts') setTimeout(renderCharts, 0);
  if (name === 'runs') loadRuns();
  if (name === 'dealers') renderDealers();
}

// ─── FILTERS ───────────────────────────────────────────────────────────────
function setYear(y, btn) {
  filterYear = y;
  document.querySelectorAll('.filter-btn').forEach(b => {
    if (['all','2022','2023','2024'].includes(b.textContent)) b.classList.remove('active');
  });
  btn.classList.add('active');
  renderTable();
}
function setSort(key) {
  if (sortKey === key) {
    sortAsc = !sortAsc;
  } else {
    sortKey = key;
    sortAsc = true;
  }
  renderTable();
}

// ─── SCRAPE ────────────────────────────────────────────────────────────────
let scrapePoller = null;

async function triggerScrape() {
  const btn = document.getElementById('scrape-btn');
  btn.disabled = true;
  btn.textContent = '⟳ Running...';
  document.getElementById('scrape-log').innerHTML = '';

  const res = await fetch('/api/scrape', { method: 'POST' });
  if (!res.ok) {
    const d = await res.json();
    btn.disabled = false;
    btn.textContent = '▶ Scrape Now';
    document.getElementById('scrape-log').innerHTML = `<div class="line-err">✗ ${d.error || 'Failed'}</div>`;
    return;
  }

  scrapePoller = setInterval(pollScrapeStatus, 1500);
}

async function triggerScrapeUrl() {
  const url = document.getElementById('manual-url').value.trim();
  if(!url) return;
  
  const btn = document.getElementById('scrape-url-btn');
  btn.disabled = true;
  btn.textContent = '...';
  document.getElementById('scrape-log').innerHTML = '';

  const res = await fetch('/api/scrape/url', { 
    method: 'POST', 
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url})
  });
  
  if (!res.ok) {
    const d = await res.json();
    btn.disabled = false;
    btn.textContent = 'Add URL';
    document.getElementById('scrape-log').innerHTML = `<div class="line-err">✗ ${d.error || 'Failed'}</div>`;
    return;
  }

  document.getElementById('manual-url').value = '';
  scrapePoller = setInterval(pollScrapeStatus, 1500);
}

async function pollScrapeStatus() {
  const res = await fetch('/api/scrape/status');
  const d = await res.json();
  const logEl = document.getElementById('scrape-log');

  logEl.innerHTML = d.log.map(line => {
    let cls = 'line-info';
    if (line.startsWith('✓')) cls = 'line-ok';
    if (line.startsWith('✗') || line.includes('Error')) cls = 'line-err';
    return `<div class="${cls}">${line}</div>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;

  if (!d.running) {
    clearInterval(scrapePoller);
    document.getElementById('scrape-btn').disabled = false;
    document.getElementById('scrape-btn').textContent = '▶ Scrape Now';
    const urlBtn = document.getElementById('scrape-url-btn');
    if(urlBtn) {
      urlBtn.disabled = false;
      urlBtn.textContent = 'Add URL';
    }
    loadListings();
  }
}

// ─── LISTINGS ──────────────────────────────────────────────────────────────
async function loadListings() {
  // Pass include_inactive if Status is Any or Gone, or just always and handle locally. Let's always fetch all.
  const res = await fetch('/api/listings?include_inactive=1');
  allListings = await res.json();
  detectDuplicates();

  const locs = [...new Set(allListings.map(l => l.location).filter(Boolean))].sort();
  const locSelect = document.getElementById('filter-location');
  if (locSelect) {
    const prevVal = locSelect.value;
    locSelect.innerHTML = '<option value="all">All Locations</option>' + locs.map(loc => `<option value="${loc}">${loc}</option>`).join('');
    if (locs.includes(prevVal)) {
      locSelect.value = prevVal;
    } else {
      filterLocation = 'all';
      locSelect.value = 'all';
    }
  }

  updateStats();
  renderTable();
  if (document.getElementById('tab-dealers').style.display === 'block') renderDealers();
}

function updateStats() {
  const prices = allListings.map(l => l.price).filter(Boolean);
  document.getElementById('stat-count').textContent = allListings.length;
  document.getElementById('stat-min').textContent = prices.length ? fmt(Math.min(...prices)) : '—';
  document.getElementById('stat-max').textContent = prices.length ? fmt(Math.max(...prices)) : '—';
  const avg = prices.length ? Math.round(prices.reduce((a,b) => a+b, 0) / prices.length) : 0;
  document.getElementById('stat-avg').textContent = avg ? fmt(avg) : '—';

  const dates = allListings.map(l => l.last_seen).filter(Boolean).sort();
  document.getElementById('stat-last').textContent = dates.length ? fmtDate(dates[dates.length-1]) : 'Never';

  // Best Day to Buy logic
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const newCounts = {0:0,1:0,2:0,3:0,4:0,5:0,6:0};
  const dropCounts = {0:0,1:0,2:0,3:0,4:0,5:0,6:0};

  allListings.forEach(l => {
    if (l.first_seen) {
      newCounts[new Date(l.first_seen).getDay()]++;
    }
    if (l.prev_price && l.price < l.prev_price && l.last_seen) {
      dropCounts[new Date(l.last_seen).getDay()]++;
    }
  });

  const bestNewDayNum = Object.keys(newCounts).reduce((a, b) => newCounts[a] > newCounts[b] ? a : b);
  const bestDropDayNum = Object.keys(dropCounts).reduce((a, b) => dropCounts[a] > dropCounts[b] ? a : b);

  document.getElementById('stat-best-new').textContent = newCounts[bestNewDayNum] > 0 ? days[bestNewDayNum] : '—';
  document.getElementById('stat-best-drop').textContent = dropCounts[bestDropDayNum] > 0 ? days[bestDropDayNum] : '—';

  // Mileage Sweet Spot
  const brackets = {};
  allListings.filter(l => l.is_active && l.price && l.mileage).forEach(l => {
    const b = Math.floor(l.mileage / 20000) * 20;
    if (!brackets[b]) brackets[b] = [];
    brackets[b].push(l.price);
  });
  
  const bKeys = Object.keys(brackets).map(Number).sort((a,b)=>a-b);
  let maxDrop = 0;
  let sweetSpot = null;
  
  if (bKeys.length > 1) {
    for (let i = 1; i < bKeys.length; i++) {
        const prevAvg = brackets[bKeys[i-1]].reduce((a,b)=>a+b,0) / brackets[bKeys[i-1]].length;
        const curAvg = brackets[bKeys[i]].reduce((a,b)=>a+b,0) / brackets[bKeys[i]].length;
        const drop = prevAvg - curAvg;
        // Avoid anomalies where higher mileage costs more
        if (drop > maxDrop && bKeys[i] <= 100) { // Cap at 100k to avoid 120k+ outliers ruining it
            maxDrop = drop;
            sweetSpot = `${bKeys[i-1]}k - ${bKeys[i]}k km (-R${Math.round(drop).toLocaleString()})`;
        }
    }
  }

  const mssEl = document.getElementById('mileage-sweet-spot');
  if (sweetSpot) {
    mssEl.style.display = 'inline-block';
    document.getElementById('mss-text').textContent = `Sweet Spot: ${sweetSpot}`;
  } else {
    mssEl.style.display = 'none';
  }
}

function getDealScore(l, avgPrice) {
  if (!l.price || !avgPrice || !l.year || !l.mileage) return 0;
  // Base factors
  let diffPct = (avgPrice - l.price) / avgPrice * 100;
  let milScore = (50000 - l.mileage) / 10000;
  let yearScore = (l.year - 2022) * 5;
  let score = 50 + (diffPct * 2) + milScore + yearScore;
  // Days-on-market bonus (longer = more negotiation room)
  if (l.first_seen) {
    const end = (l.is_active || !l.last_seen) ? new Date() : new Date(l.last_seen);
    const dom = Math.floor((end - new Date(l.first_seen)) / 86400000);
    if (dom >= 60) score += 8;
    else if (dom >= 30) score += 5;
    else if (dom >= 15) score += 2;
  }
  // Price drop bonus: 1 pt per 1% total drop, capped at +10
  if (l.prev_price && l.prev_price > l.price) {
    const dropPct = (l.prev_price - l.price) / l.prev_price * 100;
    score += Math.min(10, Math.round(dropPct));
  }
  return Math.max(0, Math.min(100, Math.round(score)));
}

// ─── DUPLICATE DETECTOR ────────────────────────────────────────────────────
function detectDuplicates() {
  allListings.forEach(l => { delete l._dupGroupId; });

  let nextGroupId = 1;
  const urlToGroup = new Map();

  for (let i = 0; i < allListings.length; i++) {
    for (let j = i + 1; j < allListings.length; j++) {
      const a = allListings[i], b = allListings[j];
      if (!a.price || !b.price || !a.year || !b.year) continue;
      if (a.source === b.source) continue;
      if (a.year !== b.year) continue;
      const priceDiff = Math.abs(a.price - b.price) / Math.max(a.price, b.price);
      if (priceDiff > 0.05) continue; // within 5%

      // Optional: mileage within 15% adds confidence but don't require it
      const aGroup = urlToGroup.get(a.url);
      const bGroup = urlToGroup.get(b.url);
      const gid = aGroup || bGroup || nextGroupId++;
      urlToGroup.set(a.url, gid);
      urlToGroup.set(b.url, gid);
    }
  }

  allListings.forEach(l => {
    if (urlToGroup.has(l.url)) l._dupGroupId = urlToGroup.get(l.url);
  });

  const dupCount = new Set(urlToGroup.values()).size;
  const insightEl = document.getElementById('dup-insight');
  const insightText = document.getElementById('dup-insight-text');
  if (dupCount > 0) {
    const listingCount = urlToGroup.size;
    insightEl.style.display = 'inline-block';
    insightText.textContent = `${listingCount} listings are duplicates across sources (${dupCount} matches) — click to filter`;
  } else {
    insightEl.style.display = 'none';
  }
}

function renderTable() {
  let activeData = allListings.filter(l => l.is_active === 1);
  const prices = activeData.map(l => l.price).filter(Boolean);
  const avg = prices.length ? prices.reduce((a,b)=>a+b,0)/prices.length : 0;

  let data = allListings;
  if (filterStatus === 'active') data = data.filter(l => l.is_active === 1);
  if (filterStatus === 'gone') data = data.filter(l => l.is_active === 0);
  if (filterStatus === 'watchlisted') data = data.filter(l => l.watchlisted);
  if (filterStatus === 'duplicates') data = data.filter(l => l._dupGroupId);
  
  if (filterYear !== 'all') data = data.filter(l => l.year === filterYear);
  if (filterLocation !== 'all') data = data.filter(l => l.location === filterLocation);

  // Compute deal score before sorting
  data.forEach(l => {
    l._dealScore = getDealScore(l, avg);
    l._priceDrop = (l.prev_price && l.price < l.prev_price) ? (l.prev_price - l.price) : 0;
  });

  data.sort((a, b) => {
    let valA, valB;
    if (sortKey === 'price') { valA = a.price||9e9; valB = b.price||9e9; }
    else if (sortKey === 'change') { valA = a._priceDrop; valB = b._priceDrop; }
    else if (sortKey === 'mileage') { valA = a.mileage||9e9; valB = b.mileage||9e9; }
    else if (sortKey === 'year') { valA = a.year||0; valB = b.year||0; }
    else if (sortKey === 'score') { valA = a._dealScore; valB = b._dealScore; }
    else if (sortKey === 'source') { valA = a.source||''; valB = b.source||''; }
    else if (sortKey === 'model') { valA = a.variant||''; valB = b.variant||''; }
    else if (sortKey === 'title') { valA = a.title||''; valB = b.title||''; }
    else if (sortKey === 'location') { valA = a.location||''; valB = b.location||''; }
    else if (sortKey === 'status') { valA = a.is_active||0; valB = b.is_active||0; }
    else { valA = a.price; valB = b.price; }
    
    if (valA < valB) return sortAsc ? -1 : 1;
    if (valA > valB) return sortAsc ? 1 : -1;

    // Fallback sort: if primary fields are equal (e.g. both drops are 0), sort ascending by actual price
    if (sortKey === 'change') {
      return (a.price || 9e9) - (b.price || 9e9);
    }
    return 0;
  });

  document.querySelectorAll('.sort-icon').forEach(el => el.innerHTML = '');
  const icon = document.getElementById('sort-icon-' + sortKey);
  if (icon) icon.innerHTML = sortAsc ? '↑' : '↓';

  const tbody = document.getElementById('listings-tbody');
  if (!data.length) {
    tbody.innerHTML = `<tr><td colspan="15"><div class="empty-state">
      <div class="icon">🔍</div>
      <div class="msg">No listings</div>
      <div class="sub">Run a scrape or adjust filters</div>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = '';
  data.forEach((l, i) => {
    const diff = avg && l.price ? ((l.price - avg) / avg * 100).toFixed(1) : null;
    const vsClass = diff === null ? '' : diff < -5 ? 'below' : diff > 5 ? 'above' : 'at';
    const vsLabel = diff === null ? '' : diff < 0 ? `▼ ${Math.abs(diff)}%` : `▲ ${diff}%`;

    const hasDelta = l.prev_price && l.price !== l.prev_price;
    const isDown = l.price < l.prev_price;
    const deltaVal = hasDelta ? Math.abs(l.price - l.prev_price) : 0;

    let scoreColor = 'var(--muted)';
    if (l._dealScore >= 75) scoreColor = 'var(--green)';
    else if (l._dealScore >= 50) scoreColor = 'var(--gold)';
    else if (l._dealScore >= 0) scoreColor = 'var(--red)';

    let timeToSellLabel = '';
    if (l.is_active === 0 && l.first_seen && l.last_seen) {
      const ms = new Date(l.last_seen) - new Date(l.first_seen);
      const days = Math.round(ms / (1000 * 60 * 60 * 24));
      timeToSellLabel = `Gone in ${Math.max(1, days)}d`;
    }

    const tr = document.createElement('tr');
    if (l.is_active === 0) tr.style.opacity = '0.5';

    // 0. Star (watchlist)
    const tdStar = document.createElement('td');
    tdStar.style.cssText = 'text-align:center;padding:4px 6px';
    const btnStar = document.createElement('button');
    btnStar.className = 'star-btn' + (l.watchlisted ? ' starred' : '');
    btnStar.textContent = l.watchlisted ? '★' : '☆';
    btnStar.title = l.watchlisted ? 'Remove from watchlist' : 'Add to watchlist';
    btnStar.onclick = async (e) => {
      e.stopPropagation();
      const res = await fetch(`/api/listings/${l.id}/watchlist`, { method: 'POST' });
      const d = await res.json();
      l.watchlisted = d.watchlisted ? 1 : 0;
      btnStar.textContent = l.watchlisted ? '★' : '☆';
      btnStar.className = 'star-btn' + (l.watchlisted ? ' starred' : '');
      btnStar.title = l.watchlisted ? 'Remove from watchlist' : 'Add to watchlist';
    };
    tdStar.appendChild(btnStar);
    tr.appendChild(tdStar);

    // 1b. Compare toggle
    const tdCmp = document.createElement('td');
    tdCmp.style.cssText = 'text-align:center;padding:4px 4px';
    const btnCmp = document.createElement('button');
    btnCmp.className = 'cmp-btn' + (compareList.includes(l.id) ? ' in-compare' : '');
    btnCmp.textContent = compareList.includes(l.id) ? '✓' : '⊕';
    btnCmp.title = compareList.includes(l.id) ? 'Remove from compare' : 'Add to compare (max 3)';
    btnCmp.dataset.id = l.id;
    btnCmp.onclick = (e) => { e.stopPropagation(); toggleCompare(l.id); };
    tdCmp.appendChild(btnCmp);
    tr.appendChild(tdCmp);

    // 1. #
    const tdRank = document.createElement('td');
    tdRank.className = 'rank-num';
    tdRank.textContent = i + 1;
    tr.appendChild(tdRank);

    // 2. Listing
    const tdListing = document.createElement('td');
    tdListing.style.cssText = 'display:flex;align-items:center;gap:10px;';

    // Thumbnail
    if (l.image) {
      const img = document.createElement('img');
      img.src = l.image;
      img.alt = '';
      img.className = 'listing-thumb';
      img.style.cssText = 'width:72px;height:48px;object-fit:cover;border-radius:4px;flex-shrink:0;background:var(--surface);border:1px solid var(--border);';
      img.onerror = function() { this.style.display='none'; };
      tdListing.appendChild(img);
    }

    const divListingText = document.createElement('div');
    const divTitle = document.createElement('div');
    divTitle.className = 'listing-title';
    const aTitle = document.createElement('a');
    aTitle.href = (l.url && l.url.startsWith('http')) ? l.url : '#';
    aTitle.target = '_blank';
    aTitle.textContent = (l.title || 'DealRadar') + ' ↗';
    divTitle.appendChild(aTitle);
    
    const divMeta = document.createElement('div');
    divMeta.className = 'listing-meta';
    const spanSeen = document.createElement('span');
    let ageHtml = '';
    if (l.first_seen) {
      const end = (l.is_active || !l.last_seen) ? new Date() : new Date(l.last_seen);
      const start = new Date(l.first_seen);
      const diffDays = Math.max(0, Math.floor(Math.abs(end - start) / (1000 * 60 * 60 * 24)));
      const ageStyle = diffDays > 30 ? 'class="age-old"' : '';
      ageHtml = ` &middot; Age: <span ${ageStyle}>${diffDays}d</span>`;
    }
    spanSeen.innerHTML = 'Seen: ' + (l.first_seen ? l.first_seen.slice(0,10) : '—') + ageHtml;
    divMeta.appendChild(spanSeen);
    
    divListingText.appendChild(divTitle);
    divListingText.appendChild(divMeta);
    tdListing.appendChild(divListingText);
    tr.appendChild(tdListing);

    // 2b. Variant
    const tdMod = document.createElement('td');
    tdMod.style.fontFamily = 'monospace';
    tdMod.textContent = l.variant || '—';
    tr.appendChild(tdMod);

    // 3. Source
    const tdSrc = document.createElement('td');
    const spanSrc = document.createElement('span');
    spanSrc.style.cssText = 'background:var(--tertiary); color:var(--text); padding:4px 8px; border-radius:4px; font-size:11px; white-space:nowrap';
    spanSrc.textContent = l.source || 'AutoTrader';
    tdSrc.appendChild(spanSrc);
    if (l._dupGroupId) {
      const dupBadge = document.createElement('div');
      dupBadge.title = `Likely same car on multiple platforms (group ${l._dupGroupId})`;
      dupBadge.style.cssText = 'font-size:10px;color:var(--gold);margin-top:3px;font-family:monospace;cursor:default';
      dupBadge.textContent = '⚡ dup #' + l._dupGroupId;
      tdSrc.appendChild(dupBadge);
    }
    tr.appendChild(tdSrc);

    // 4. Price
    const tdPrice = document.createElement('td');
    const divPrice = document.createElement('div');
    divPrice.className = 'price-cell';
    divPrice.textContent = fmt(l.price);
    tdPrice.appendChild(divPrice);
    tr.appendChild(tdPrice);

    // 4b. Drop
    const tdDrop = document.createElement('td');
    if (hasDelta) {
      const divDelta = document.createElement('div');
      divDelta.className = `price-delta ${isDown ? 'down' : 'up'}`;
      divDelta.textContent = `${isDown ? '▼' : '▲'} ${fmt(deltaVal)}`;
      tdDrop.appendChild(divDelta);
    } else {
      tdDrop.textContent = '—';
      tdDrop.style.color = 'var(--muted)';
      tdDrop.style.fontSize = '12px';
    }
    tr.appendChild(tdDrop);

    // 5. Score
    const tdScore = document.createElement('td');
    tdScore.style.cssText = `color:${scoreColor};font-weight:bold`;
    tdScore.textContent = l._dealScore || '—';
    tr.appendChild(tdScore);

    // 6. Neg. Gap
    const tdVs = document.createElement('td');
    if (l.price && avg) {
      const gap = avg - l.price;
      const spanVs = document.createElement('span');
      if (gap > 0) {
        spanVs.className = 'vs-avg below';
        spanVs.textContent = `+ ${fmt(gap)}`;
      } else {
        spanVs.className = 'vs-avg above';
        spanVs.textContent = `- ${fmt(Math.abs(gap))}`;
      }
      tdVs.appendChild(spanVs);
    } else {
      tdVs.textContent = '—';
    }
    tr.appendChild(tdVs);

    // 7. Mileage
    const tdMil = document.createElement('td');
    tdMil.style.fontFamily = 'monospace';
    tdMil.textContent = l.mileage ? Number(l.mileage).toLocaleString() + ' km' : (l.mileage_raw || '—');
    tr.appendChild(tdMil);

    // 8. Year
    const tdYear = document.createElement('td');
    tdYear.style.fontFamily = 'monospace';
    tdYear.textContent = l.year || '—';
    tr.appendChild(tdYear);

    // 9. Location & Dealer
    const tdLoc = document.createElement('td');
    const divLoc = document.createElement('div');
    divLoc.style.cssText = 'color:var(--text);font-size:12px';
    divLoc.textContent = l.location || '—';
    const divDealer = document.createElement('div');
    divDealer.style.cssText = 'font-size:11px;color:rgba(255,255,255,0.4)';
    divDealer.textContent = l.dealer || '';
    tdLoc.appendChild(divLoc);
    tdLoc.appendChild(divDealer);
    tr.appendChild(tdLoc);

    // 10. Status
    const tdStatus = document.createElement('td');
    tdStatus.style.fontSize = '11px';
    const spanStat = document.createElement('span');
    if (l.is_active) {
      spanStat.style.color = 'var(--green)';
      spanStat.textContent = 'Active';
    } else {
      spanStat.style.color = 'var(--red)';
      spanStat.textContent = timeToSellLabel;
    }
    tdStatus.appendChild(spanStat);
    tr.appendChild(tdStatus);

    // 11. History
    const tdHist = document.createElement('td');
    const btnHist = document.createElement('button');
    btnHist.className = 'history-btn';
    btnHist.textContent = 'History';
    btnHist.onclick = () => showHistory(l.id, l.title || '');
    tdHist.appendChild(btnHist);
    tr.appendChild(tdHist);

    tbody.appendChild(tr);
  });
}

// ─── CHARTS ────────────────────────────────────────────────────────────────
const chartDefaults = {
  color: '#7d8590',
  borderColor: '#1c2333',
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { color: '#1c2333' }, ticks: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 } } },
    y: { grid: { color: '#1c2333' }, ticks: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 } } },
  }
};

async function renderCharts() {
  const [marketRes, analyticsRes] = await Promise.all([
    fetch('/api/market').then(r => r.json()),
    fetch('/api/analytics').then(r => r.json()),
  ]);
  const market = marketRes.chart;
  const vel = marketRes.velocity_30d;
  const forecast = analyticsRes.forecast || [];
  const dowData = analyticsRes.dow || [];
  const slope = analyticsRes.slope;
  const listings = allListings;

  // Update Velocity Badge
  const velBadge = document.getElementById('velocity-badge');
  if (vel !== undefined && vel !== 0) {
    velBadge.style.display = 'inline-block';
    if (vel < 0) {
      velBadge.style.color = 'var(--green)';
      velBadge.style.borderColor = 'rgba(63,185,80,0.4)';
      velBadge.textContent = '▼ R ' + Math.abs(vel).toLocaleString() + ' / mo';
    } else {
      velBadge.style.color = 'var(--red)';
      velBadge.style.borderColor = 'rgba(248,81,73,0.4)';
      velBadge.textContent = '▲ R ' + vel.toLocaleString() + ' / mo';
    }
  }

  // ── Forecast chart (historical + projected) ──────────────────────────────
  const forecastBadge = document.getElementById('forecast-badge');
  if (slope !== null && slope !== undefined) {
    forecastBadge.style.display = 'inline-block';
    if (slope < 0) {
      forecastBadge.style.color = 'var(--green)';
      forecastBadge.style.borderColor = 'rgba(63,185,80,0.4)';
      forecastBadge.textContent = '▼ Trending down R' + Math.abs(slope).toFixed(0) + '/day';
    } else {
      forecastBadge.style.color = 'var(--red)';
      forecastBadge.style.borderColor = 'rgba(248,81,73,0.4)';
      forecastBadge.textContent = '▲ Trending up R' + slope.toFixed(0) + '/day';
    }
  }

  const histLabels = market.map(d => d.date);
  const histPrices = market.map(d => d.avg_price);
  // Pad historical arrays so forecast starts right after
  const forecastLabels = forecast.map(d => d.date);
  const forecastPrices = forecast.map(d => d.projected_price);
  const allLabels = [...histLabels, ...forecastLabels];
  // Historical dataset: null for forecast positions
  const histPadded = [...histPrices, ...Array(forecastLabels.length).fill(null)];
  // Forecast dataset: null for historical positions, then projected (overlap 1 point for continuity)
  const forecastPadded = [...Array(histPrices.length - 1).fill(null), histPrices[histPrices.length - 1] ?? null, ...forecastPrices];

  if (charts.forecast) charts.forecast.destroy();
  charts.forecast = new Chart(document.getElementById('chart-forecast'), {
    type: 'line',
    data: {
      labels: allLabels,
      datasets: [
        {
          label: 'Historical',
          data: histPadded,
          borderColor: '#d4a843',
          backgroundColor: 'rgba(212,168,67,0.07)',
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointBackgroundColor: '#d4a843',
          spanGaps: false,
        },
        {
          label: 'Forecast',
          data: forecastPadded,
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,0.06)',
          fill: true,
          tension: 0.3,
          borderDash: [6, 4],
          pointRadius: 2,
          pointBackgroundColor: '#58a6ff',
          spanGaps: false,
        },
      ]
    },
    options: {
      ...chartDefaults,
      plugins: {
        legend: { display: true, labels: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 }, boxWidth: 20 } },
      },
      scales: {
        ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
      }
    }
  });

  // ── Best time to buy — day of week ────────────────────────────────────────
  const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  if (dowData.length > 0) {
    const minPrice = Math.min(...dowData.map(d => d.avg_price));
    const bestDayBadge = document.getElementById('best-day-badge');
    const bestRow = dowData.find(d => d.avg_price === minPrice);
    if (bestRow) {
      bestDayBadge.style.display = 'inline-block';
      bestDayBadge.textContent = 'Best: ' + DAY_NAMES[bestRow.dow] + ' · R' + Math.round(minPrice).toLocaleString();
    }
    const dowColors = dowData.map(d =>
      d.avg_price === minPrice ? 'rgba(63,185,80,0.75)' : 'rgba(212,168,67,0.45)'
    );
    const dowBorders = dowData.map(d =>
      d.avg_price === minPrice ? '#3fb950' : '#d4a843'
    );
    if (charts.dow) charts.dow.destroy();
    charts.dow = new Chart(document.getElementById('chart-dow'), {
      type: 'bar',
      data: {
        labels: dowData.map(d => DAY_NAMES[d.dow]),
        datasets: [{
          data: dowData.map(d => d.avg_price),
          backgroundColor: dowColors,
          borderColor: dowBorders,
          borderWidth: 1,
        }]
      },
      options: {
        ...chartDefaults,
        plugins: { legend: { display: false } },
        scales: {
          ...chartDefaults.scales,
          y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
        }
      }
    });
  }

  // (Legacy market chart removed — forecast chart replaces it)
  if (charts.market) { charts.market.destroy(); charts.market = null; }

  // Scatter: price vs mileage
  const scatterData = listings.filter(l => l.price && l.mileage).map(l => ({ x: l.mileage, y: l.price }));
  if (charts.scatter) charts.scatter.destroy();
  charts.scatter = new Chart(document.getElementById('chart-scatter'), {
    type: 'scatter',
    data: { datasets: [{ data: scatterData, backgroundColor: 'rgba(212,168,67,0.6)', pointRadius: 5 }] },
    options: {
      ...chartDefaults,
      scales: {
        x: { ...chartDefaults.scales.x, title: { display: true, text: 'Mileage (km)', color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 } } },
        y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
      }
    }
  });

  // Price distribution histogram
  const prices = listings.map(l => l.price).filter(Boolean);
  const min = Math.min(...prices), max = Math.max(...prices);
  const buckets = 8;
  const bucketSize = (max - min) / buckets || 50000;
  const counts = Array(buckets).fill(0);
  const labels = [];
  for (let i = 0; i < buckets; i++) {
    labels.push('R' + Math.round((min + i * bucketSize) / 1000) + 'k');
  }
  prices.forEach(p => {
    const idx = Math.min(Math.floor((p - min) / bucketSize), buckets - 1);
    counts[idx]++;
  });
  if (charts.dist) charts.dist.destroy();
  charts.dist = new Chart(document.getElementById('chart-dist'), {
    type: 'bar',
    data: { labels, datasets: [{ data: counts, backgroundColor: 'rgba(212,168,67,0.5)', borderColor: '#d4a843', borderWidth: 1 }] },
    options: { ...chartDefaults }
  });

  // Year breakdown
  const yearCounts = {};
  listings.forEach(l => { if (l.year) yearCounts[l.year] = (yearCounts[l.year]||0) + 1; });
  if (charts.year) charts.year.destroy();
  charts.year = new Chart(document.getElementById('chart-year'), {
    type: 'doughnut',
    data: {
      labels: Object.keys(yearCounts),
      datasets: [{ data: Object.values(yearCounts), backgroundColor: ['#d4a843','#3fb950','#58a6ff'], borderColor: '#0d1117', borderWidth: 2 }]
    },
    options: {
      plugins: { legend: { display: true, labels: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 11 } } } }
    }
  });

  // ── Market Heat Map — avg price by location ───────────────────────────────
  const locMap = {};
  listings.filter(l => l.is_active && l.price && l.location).forEach(l => {
    if (!locMap[l.location]) locMap[l.location] = { prices: [] };
    locMap[l.location].prices.push(l.price);
  });
  const locStats = Object.entries(locMap)
    .map(([name, d]) => ({
      name,
      count: d.prices.length,
      avg: Math.round(d.prices.reduce((a,b) => a+b, 0) / d.prices.length),
      min: Math.min(...d.prices),
    }))
    .sort((a, b) => a.avg - b.avg);

  if (locStats.length > 0) {
    const cheapestAvg = locStats[0].avg;
    const hmBadge = document.getElementById('heatmap-badge');
    hmBadge.style.display = 'inline-block';
    hmBadge.textContent = 'Cheapest: ' + locStats[0].name + ' · R' + cheapestAvg.toLocaleString();

    const hmColors = locStats.map(d =>
      d.avg === cheapestAvg ? 'rgba(63,185,80,0.75)' : 'rgba(212,168,67,0.45)'
    );
    const hmBorders = locStats.map(d =>
      d.avg === cheapestAvg ? '#3fb950' : '#d4a843'
    );
    if (charts.heatmap) charts.heatmap.destroy();
    charts.heatmap = new Chart(document.getElementById('chart-heatmap'), {
      type: 'bar',
      data: {
        labels: locStats.map(d => d.name),
        datasets: [{
          data: locStats.map(d => d.avg),
          backgroundColor: hmColors,
          borderColor: hmBorders,
          borderWidth: 1,
        }]
      },
      options: {
        indexAxis: 'y',
        ...chartDefaults,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const d = locStats[ctx.dataIndex];
                return ` Avg R${d.avg.toLocaleString()}  ·  Lowest R${d.min.toLocaleString()}  ·  ${d.count} listing${d.count !== 1 ? 's' : ''}`;
              }
            }
          }
        },
        scales: {
          x: { ...chartDefaults.scales.x, ticks: { ...chartDefaults.scales.x.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } },
          y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, font: { family: 'IBM Plex Mono', size: 11 } } },
        }
      }
    });
  }
}

// ─── NEGOTIATION HELPER ─────────────────────────────────────────────────────
function renderNegHelper(l, avgPrice) {
  const wrap = document.getElementById('neg-helper-wrap');
  if (!l || !l.price) { wrap.style.display = 'none'; return; }

  let discount = 0;
  const reasons = [];

  // Days on market
  if (l.first_seen) {
    const end = (l.is_active || !l.last_seen) ? new Date() : new Date(l.last_seen);
    const dom = Math.floor((end - new Date(l.first_seen)) / 86400000);
    if (dom >= 60)      { discount += 6; reasons.push(`Listed ${dom} days — stale stock, sellers are motivated`); }
    else if (dom >= 30) { discount += 4; reasons.push(`Listed ${dom} days — been sitting a while`); }
    else if (dom >= 15) { discount += 2; reasons.push(`Listed ${dom} days — some room to negotiate`); }
    else                { reasons.push(`Only listed ${dom} days — seller unlikely to budge much yet`); }
  }

  // vs market average
  if (avgPrice && l.price) {
    const diff = ((l.price - avgPrice) / avgPrice * 100);
    if (diff > 10)      { discount += 4; reasons.push(`${diff.toFixed(0)}% above market average — overpriced`); }
    else if (diff > 5)  { discount += 2; reasons.push(`${diff.toFixed(0)}% above market average`); }
    else if (diff < -5) { reasons.push(`${Math.abs(diff).toFixed(0)}% below market average — already a good price`); }
  }

  // Prior price drops
  if (l.prev_price && l.price < l.prev_price) {
    const dropPct = ((l.prev_price - l.price) / l.prev_price * 100).toFixed(1);
    discount += 2;
    reasons.push(`Seller already dropped price ${dropPct}% (R${(l.prev_price - l.price).toLocaleString()})`);
  }

  // Compute offer range (round to nearest R1k)
  const round1k = v => Math.round(v / 1000) * 1000;
  const openingOffer = round1k(l.price * (1 - (discount + 3) / 100));
  const targetOffer  = round1k(l.price * (1 - discount / 100));
  const savings = l.price - openingOffer;

  const negEl = document.getElementById('neg-helper-content');

  // Offer range strip
  const rangeDiv = document.createElement('div');
  rangeDiv.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px';
  rangeDiv.innerHTML = `
    <div style="background:var(--bg);border:1px solid var(--border);padding:12px">
      <div style="font-size:10px;color:var(--muted);letter-spacing:1px;font-family:monospace;margin-bottom:4px">OPENING OFFER</div>
      <div style="font-size:20px;font-weight:700;font-family:monospace;color:var(--green)">${fmt(openingOffer)}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:3px">Save up to ${fmt(savings)} off asking</div>
    </div>
    <div style="background:var(--bg);border:1px solid var(--border);padding:12px">
      <div style="font-size:10px;color:var(--muted);letter-spacing:1px;font-family:monospace;margin-bottom:4px">TARGET / WALK-AWAY</div>
      <div style="font-size:20px;font-weight:700;font-family:monospace;color:var(--gold)">${fmt(targetOffer)}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:3px">Based on ${discount}% leverage found</div>
    </div>`;
  negEl.innerHTML = '';
  negEl.appendChild(rangeDiv);

  // Script suggestion
  const script = document.createElement('div');
  script.style.cssText = 'background:rgba(212,168,67,0.06);border:1px solid rgba(212,168,67,0.2);padding:12px;margin-bottom:12px;font-size:12px;color:var(--text);line-height:1.6';
  script.textContent = `"I've done my research and similar vehicles are going for around ${fmt(Math.round((avgPrice || l.price) / 1000) * 1000)}. Would you consider ${fmt(openingOffer)}?"`;
  negEl.appendChild(script);

  // Reasons list
  if (reasons.length) {
    const ul = document.createElement('ul');
    ul.style.cssText = 'list-style:none;display:flex;flex-direction:column;gap:5px';
    reasons.forEach(r => {
      const li = document.createElement('li');
      li.style.cssText = 'font-size:11px;color:var(--muted);padding-left:12px;position:relative';
      li.textContent = r;
      li.style.setProperty('--dot', '"·"');
      const dot = document.createElement('span');
      dot.style.cssText = 'position:absolute;left:0;color:var(--gold)';
      dot.textContent = '·';
      li.prepend(dot);
      ul.appendChild(li);
    });
    negEl.appendChild(ul);
  }

  wrap.style.display = 'block';
}

// ─── OWNERSHIP CALCULATOR ───────────────────────────────────────────────────
let _ocPrice = 0;
function calcOwnership() {
  if (!_ocPrice) return;
  const deposit   = Math.max(0, parseFloat(document.getElementById('oc-deposit').value)   || 0);
  const term      = parseInt(document.getElementById('oc-term').value)    || 60;
  const annualRate= parseFloat(document.getElementById('oc-rate').value)  || 12.5;
  const insurance = parseFloat(document.getElementById('oc-insurance').value) || 0;
  const fuel      = parseFloat(document.getElementById('oc-fuel').value)      || 0;
  const maint     = parseFloat(document.getElementById('oc-maint').value)     || 0;

  const principal = Math.max(0, _ocPrice - deposit);
  const r = annualRate / 100 / 12;
  const installment = (principal === 0 || r === 0)
    ? principal / term
    : principal * r * Math.pow(1 + r, term) / (Math.pow(1 + r, term) - 1);

  const monthlyAll = installment + insurance + fuel + maint / 12;
  const totalCost  = deposit + monthlyAll * term;
  const totalInterest = Math.max(0, installment * term - principal);
  const years = term / 12;

  const el = document.getElementById('oc-results');
  el.innerHTML = '';

  const grid = document.createElement('div');
  grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px';
  [
    ['MONTHLY INSTALL.', fmt(Math.round(installment)), 'var(--gold)'],
    ['MONTHLY ALL-IN',   fmt(Math.round(monthlyAll)),  'var(--text)'],
    [`TOTAL ${years}YR COST`, fmt(Math.round(totalCost)), 'var(--red)'],
  ].forEach(([label, value, color]) => {
    const card = document.createElement('div');
    card.style.cssText = 'background:var(--bg);border:1px solid var(--border);padding:12px';
    const lEl = document.createElement('div');
    lEl.style.cssText = 'font-size:10px;color:var(--muted);letter-spacing:1px;font-family:monospace;margin-bottom:4px';
    lEl.textContent = label;
    const vEl = document.createElement('div');
    vEl.style.cssText = `font-size:18px;font-weight:700;font-family:monospace;color:${color}`;
    vEl.textContent = value;
    card.appendChild(lEl);
    card.appendChild(vEl);
    grid.appendChild(card);
  });
  el.appendChild(grid);

  const note = document.createElement('div');
  note.style.cssText = 'font-size:11px;color:var(--muted)';
  note.textContent = `Interest paid over loan: ${fmt(Math.round(totalInterest))}  ·  `
    + `Monthly: ${fmt(Math.round(installment))} loan + ${fmt(insurance)} insure + ${fmt(fuel)} fuel + ${fmt(Math.round(maint/12))} service`;
  el.appendChild(note);
}

// ─── COMPARE MODE ──────────────────────────────────────────────────────────
let compareList = []; // array of listing ids

function toggleCompare(id) {
  const idx = compareList.indexOf(id);
  if (idx > -1) {
    compareList.splice(idx, 1);
  } else {
    if (compareList.length >= 3) {
      // Replace oldest
      compareList.shift();
    }
    compareList.push(id);
  }
  renderCompareBar();
  // Update all compare buttons in the table
  document.querySelectorAll('.cmp-btn').forEach(btn => {
    const bid = parseInt(btn.dataset.id);
    const active = compareList.includes(bid);
    btn.className = 'cmp-btn' + (active ? ' in-compare' : '');
    btn.textContent = active ? '✓' : '⊕';
    btn.title = active ? 'Remove from compare' : 'Add to compare (max 3)';
  });
}

function renderCompareBar() {
  const bar = document.getElementById('compare-bar');
  const chipsEl = document.getElementById('cmp-chips');
  const goBtn = document.getElementById('cmp-go-btn');

  if (compareList.length === 0) {
    bar.classList.remove('visible');
    return;
  }
  bar.classList.add('visible');
  goBtn.textContent = `COMPARE (${compareList.length})`;
  goBtn.disabled = compareList.length < 2;
  goBtn.style.opacity = compareList.length < 2 ? '0.5' : '1';

  chipsEl.innerHTML = '';
  // Show chips for selected listings
  [0, 1, 2].forEach(slot => {
    const id = compareList[slot];
    if (id !== undefined) {
      const l = allListings.find(x => x.id === id);
      if (!l) return;
      const chip = document.createElement('div');
      chip.className = 'cmp-chip';
      if (l.image) {
        const img = document.createElement('img');
        img.src = l.image;
        img.onerror = function() { this.style.display = 'none'; };
        chip.appendChild(img);
      }
      const info = document.createElement('div');
      info.style.cssText = 'overflow:hidden;flex:1';
      const title = document.createElement('div');
      title.className = 'cmp-chip-title';
      title.textContent = l.title || 'Unknown';
      const price = document.createElement('div');
      price.className = 'cmp-chip-price';
      price.textContent = fmt(l.price);
      info.appendChild(title);
      info.appendChild(price);
      chip.appendChild(info);
      const rm = document.createElement('button');
      rm.className = 'cmp-chip-remove';
      rm.textContent = '✕';
      rm.onclick = () => toggleCompare(id);
      chip.appendChild(rm);
      chipsEl.appendChild(chip);
    } else {
      const slot_el = document.createElement('div');
      slot_el.className = 'cmp-slot';
      slot_el.textContent = '+ Add car';
      chipsEl.appendChild(slot_el);
    }
  });
}

function clearCompare() {
  compareList = [];
  renderCompareBar();
  document.querySelectorAll('.cmp-btn').forEach(btn => {
    btn.className = 'cmp-btn';
    btn.textContent = '⊕';
    btn.title = 'Add to compare (max 3)';
  });
}

function openCompare() {
  if (compareList.length < 2) return;
  const cars = compareList.map(id => allListings.find(x => x.id === id)).filter(Boolean);
  const n = cars.length;

  // Pre-compute deal scores
  const activePrices = allListings.filter(l => l.is_active && l.price).map(l => l.price);
  const marketAvg = activePrices.length ? activePrices.reduce((a,b)=>a+b,0)/activePrices.length : 0;
  cars.forEach(c => { c._cmpScore = getDealScore(c, marketAvg); });

  const content = document.getElementById('cmp-content');
  content.innerHTML = '';

  // Grid column template: label col + N car cols
  const colTemplate = `160px ${Array(n).fill('1fr').join(' ')}`;

  const grid = document.createElement('div');
  grid.className = 'cmp-grid';
  grid.style.gridTemplateColumns = '1px'; // handled per row

  // Helper to create a row
  function makeRow(labelText, values, opts = {}) {
    const row = document.createElement('div');
    row.className = 'cmp-row';
    row.style.gridTemplateColumns = colTemplate;

    const label = document.createElement('div');
    label.className = 'cmp-cell row-label';
    label.textContent = labelText;
    row.appendChild(label);

    // Find best/worst for highlighting
    const nums = values.map(v => typeof v === 'object' ? v._num : null);
    const validNums = nums.filter(v => v !== null && !isNaN(v));
    let bestNum = null, worstNum = null;
    if (validNums.length > 1 && opts.highlight) {
      bestNum = opts.highlight === 'low' ? Math.min(...validNums) : Math.max(...validNums);
      worstNum = opts.highlight === 'low' ? Math.max(...validNums) : Math.min(...validNums);
    }

    values.forEach((v, i) => {
      const cell = document.createElement('div');
      cell.className = 'cmp-cell';
      const num = typeof v === 'object' ? v._num : null;
      const display = typeof v === 'object' ? v.text : v;
      if (num !== null && bestNum !== null && num === bestNum) cell.classList.add('best');
      else if (num !== null && worstNum !== null && num === worstNum) cell.classList.add('worst');
      cell.textContent = display || '—';
      row.appendChild(cell);
    });
    return row;
  }

  // ── Header row (images + titles) ────────────────────────────────────────
  const headerRow = document.createElement('div');
  headerRow.className = 'cmp-row header-row';
  headerRow.style.gridTemplateColumns = colTemplate;
  // Empty label cell
  const emptyLabel = document.createElement('div');
  emptyLabel.className = 'cmp-cell row-label';
  headerRow.appendChild(emptyLabel);
  cars.forEach(c => {
    const cell = document.createElement('div');
    cell.className = 'cmp-header-cell';
    if (c.image) {
      const img = document.createElement('img');
      img.src = c.image;
      img.style.cssText = 'width:100%;height:80px;object-fit:cover;border-radius:3px;border:1px solid var(--border)';
      img.onerror = function() { this.style.display='none'; };
      cell.appendChild(img);
    }
    const titleEl = document.createElement('a');
    titleEl.href = c.url || '#';
    titleEl.target = '_blank';
    titleEl.style.cssText = 'font-size:12px;font-weight:600;color:var(--text);text-decoration:none';
    titleEl.textContent = (c.title || 'Unknown') + ' ↗';
    cell.appendChild(titleEl);
    const src = document.createElement('div');
    src.style.cssText = 'font-size:10px;color:var(--muted);font-family:monospace';
    src.textContent = c.source || 'AutoTrader';
    cell.appendChild(src);
    headerRow.appendChild(cell);
  });
  grid.appendChild(headerRow);

  // ── Data rows ────────────────────────────────────────────────────────────
  const rows = [
    ['ASKING PRICE',  cars.map(c => ({ _num: c.price, text: fmt(c.price) })), { highlight: 'low' }],
    ['YEAR',          cars.map(c => ({ _num: parseInt(c.year), text: c.year || '—' })), { highlight: 'high' }],
    ['MILEAGE',       cars.map(c => ({ _num: c.mileage, text: c.mileage ? Number(c.mileage).toLocaleString() + ' km' : '—' })), { highlight: 'low' }],
    ['VARIANT',       cars.map(c => c.variant || '—'), {}],
    ['LOCATION',      cars.map(c => c.location || '—'), {}],
    ['DEALER',        cars.map(c => c.dealer || '—'), {}],
    ['DEAL SCORE',    cars.map(c => ({ _num: c._cmpScore, text: c._cmpScore ? String(c._cmpScore) : '—' })), { highlight: 'high' }],
    ['PRICE DROP',    cars.map(c => {
      if (!c.prev_price || c.price >= c.prev_price) return { _num: 0, text: 'None' };
      return { _num: c.prev_price - c.price, text: `▼ ${fmt(c.prev_price - c.price)}` };
    }), { highlight: 'high' }],
    ['DAYS LISTED',   cars.map(c => {
      if (!c.first_seen) return '—';
      const end = (c.is_active || !c.last_seen) ? new Date() : new Date(c.last_seen);
      const d = Math.floor((end - new Date(c.first_seen)) / 86400000);
      return { _num: d, text: d + 'd' };
    }), {}],
    ['EST. MONTHLY',  cars.map(c => {
      if (!c.price) return '—';
      const P = Math.max(0, c.price - 50000);
      const r = 0.125 / 12;
      const n = 60;
      const m = P * r * Math.pow(1+r,n) / (Math.pow(1+r,n) - 1);
      return { _num: Math.round(m), text: fmt(Math.round(m)) + '/mo' };
    }), { highlight: 'low' }],
    ['VS MARKET AVG', cars.map(c => {
      if (!c.price || !marketAvg) return '—';
      const diff = ((c.price - marketAvg) / marketAvg * 100).toFixed(1);
      return { _num: parseFloat(diff), text: (diff > 0 ? '▲ +' : '▼ ') + diff + '%' };
    }), { highlight: 'low' }],
  ];

  rows.forEach(([label, values, opts]) => {
    grid.appendChild(makeRow(label, values, opts));
  });

  content.appendChild(grid);

  const note = document.createElement('div');
  note.style.cssText = 'font-size:11px;color:var(--dim);margin-top:12px';
  note.textContent = 'Green = best value · Red = worst value · Est. Monthly assumes R50k deposit, 60 months at 12.5%';
  content.appendChild(note);

  document.getElementById('cmp-modal').classList.add('open');
}

function closeCompare() {
  document.getElementById('cmp-modal').classList.remove('open');
}
document.getElementById('cmp-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('cmp-modal')) closeCompare();
});

// ─── HISTORY MODAL ─────────────────────────────────────────────────────────
async function showHistory(id, title) {
  document.getElementById('modal-title').textContent = title || 'Price History';
  document.getElementById('modal-subtitle').textContent = 'Price recorded on each scrape run';
  document.getElementById('modal').classList.add('open');

  const hist = await fetch(`/api/listings/${id}/history`).then(r => r.json());

  if (charts.history) charts.history.destroy();
  charts.history = new Chart(document.getElementById('chart-history'), {
    type: 'line',
    data: {
      labels: hist.map(h => h.scraped_at.slice(0,16)),
      datasets: [{
        data: hist.map(h => h.price),
        borderColor: '#d4a843',
        backgroundColor: 'rgba(212,168,67,0.1)',
        fill: true,
        tension: 0.2,
        pointRadius: 5,
        pointBackgroundColor: '#d4a843',
      }]
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
      }
    }
  });

  // Calculate Similar Listings
  const sdWrap = document.getElementById('similar-listings-wrap');
  if (sdWrap) sdWrap.style.display = 'none';
  
  const me = allListings.find(x => x.id === id);
  if (me && me.price && me.year && me.mileage) {
    const scored = allListings
      .filter(x => x.id !== id && x.price && x.year && x.mileage && x.is_active)
      .map(x => {
        // Distance weight: Price diff (low), Year diff (high), Mileage diff (medium)
        const dPrice = Math.abs(x.price - me.price) / 10000; // 1 pt per 10k
        const dYear = Math.abs(x.year - me.year) * 5; // 5 pts per year diff
        const dMil = Math.abs(x.mileage - me.mileage) / 10000; // 1 pt per 10k km
        return { ...x, _dist: dPrice + dYear + dMil };
      })
      .sort((a,b) => a._dist - b._dist)
      .slice(0, 3);
      
    if (sdWrap && scored.length > 0) {
      sdWrap.style.display = 'block';
      let html = '';
      scored.forEach(s => {
        html += `
          <div style="display:flex;justify-content:space-between;align-items:center;background:var(--bg);padding:8px 12px;border-radius:4px;border:1px solid var(--border)">
            <div style="flex:1">
              <a href="${s.url}" target="_blank" style="color:var(--text);text-decoration:none;font-weight:600;font-size:13px">${s.title || 'Unknown Vehicle'}</a>
              <div style="font-size:11px;color:var(--muted);margin-top:4px">${s.year} &middot; ${Number(s.mileage).toLocaleString()} km &middot; ${s.location||'Anywhere'}</div>
            </div>
            <div style="font-weight:bold;color:var(--gold)">R ${Number(s.price).toLocaleString()}</div>
          </div>
        `;
      });
      document.getElementById('similar-listings').innerHTML = html;
    }
  }

  // Negotiation helper + Ownership calculator
  if (me && me.price) {
    const activePrices = allListings.filter(l => l.is_active && l.price).map(l => l.price);
    const marketAvg = activePrices.length ? activePrices.reduce((a,b)=>a+b,0)/activePrices.length : 0;
    renderNegHelper(me, marketAvg);

    _ocPrice = me.price;
    document.getElementById('ownership-wrap').style.display = 'block';
    calcOwnership();
  } else {
    document.getElementById('neg-helper-wrap').style.display = 'none';
    document.getElementById('ownership-wrap').style.display = 'none';
  }
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}
document.getElementById('modal').addEventListener('click', e => {
  if (e.target === document.getElementById('modal')) closeModal();
});

// ─── RUNS ──────────────────────────────────────────────────────────────────
async function loadRuns() {
  const runs = await fetch('/api/runs').then(r => r.json());
  const el = document.getElementById('runs-list');
  if (!runs.length) { el.innerHTML = '<div style="color:var(--dim);font-family:monospace;font-size:12px;padding:20px 0">No runs yet</div>'; return; }
  el.innerHTML = runs.map(r => {
    let dur = '—';
    if (r.started_at && r.finished_at) {
      const secs = Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000);
      dur = secs >= 60 ? `${Math.floor(secs/60)}m ${secs%60}s` : `${secs}s`;
    } else if (r.status === 'running') {
      dur = '⟳';
    }
    return `
    <div class="run-row">
      <div>${fmtDate(r.started_at)}</div>
      <div><span class="status-dot ${r.status}"></span>${r.status}</div>
      <div>${r.listings_found ?? '—'}</div>
      <div style="color:var(--green)">${r.new_listings ?? '—'}</div>
      <div style="color:var(--gold)">${r.price_changes ?? '—'}</div>
      <div style="color:var(--muted)">${dur}</div>
    </div>`;
  }).join('');
}

// ─── DEALERS ───────────────────────────────────────────────────────────────
function renderDealers() {
  const activeData = allListings.filter(l => l.is_active === 1);
  const avgMktPrice = activeData.reduce((a, b) => a + (b.price || 0), 0) / (activeData.length || 1);
  
  const dMap = {};
  // Aggregate dealer stats
  activeData.forEach(l => {
    if (!l.dealer) return;
    const key = l.dealer;
    if (!dMap[key]) dMap[key] = { name: key, inv: 0, drops: 0, devSum: 0 };
    
    dMap[key].inv++;
    if (l.prev_price && l.price < l.prev_price) dMap[key].drops++;
    
    if (l.price) {
        // Deviation percentage from market average (negative means cheaper than market)
        const devPct = ((l.price - avgMktPrice) / avgMktPrice) * 100;
        dMap[key].devSum += devPct;
    }
  });
  
  let dealers = Object.values(dMap).map(d => {
      d.avgDev = d.devSum / d.inv;
      // Grade assignment
      if (d.avgDev < -5) d.grade = 'A';
      else if (d.avgDev <= 0) d.grade = 'B';
      else if (d.avgDev <= 5) d.grade = 'C';
      else d.grade = 'F';
      return d;
  }).filter(d => d.inv > 1).sort((a,b) => a.avgDev - b.avgDev); // Only show dealers with > 1 car, sorted by best deals

  const tbody = document.getElementById('dealers-tbody');
  tbody.innerHTML = '';
  
  if (dealers.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:30px">Not enough dealer data yet.</td></tr>';
    return;
  }
  
  dealers.forEach((d, i) => {
    let gradeColor = 'var(--text)';
    if (d.grade === 'A') gradeColor = '#58a6ff';
    if (d.grade === 'B') gradeColor = 'var(--green)';
    if (d.grade === 'C') gradeColor = 'var(--gold)';
    if (d.grade === 'F') gradeColor = 'var(--red)';
    
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="color:var(--dim)">${i+1}</td>
      <td style="font-weight:600;color:var(--text)">${d.name}</td>
      <td style="text-align:right;color:var(--text)">${d.inv}</td>
      <td style="text-align:right;color:var(--green)">${d.drops}</td>
      <td style="text-align:right;color:${d.avgDev > 0 ? 'var(--red)' : 'var(--green)'}">${d.avgDev > 0 ? '+' : ''}${d.avgDev.toFixed(1)}%</td>
      <td style="text-align:center;font-weight:900;font-size:18px;color:${gradeColor}">${d.grade}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ─── THEME ─────────────────────────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next === 'dark' ? '' : 'light');
  document.getElementById('theme-btn').textContent = next === 'light' ? '🌙' : '☀';
  localStorage.setItem('dr_theme', next);
}
(function initTheme() {
  const saved = localStorage.getItem('dr_theme');
  if (saved === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.getElementById('theme-btn').textContent = '🌙';
  }
})();

// ─── COMPACT VIEW ──────────────────────────────────────────────────────────
function toggleCompact() {
  const isCompact = document.body.classList.toggle('compact');
  document.getElementById('compact-btn').textContent = isCompact ? '⊞' : '⊟';
  localStorage.setItem('dr_compact', isCompact ? '1' : '0');
}
(function initCompact() {
  if (localStorage.getItem('dr_compact') === '1') {
    document.body.classList.add('compact');
    document.getElementById('compact-btn').textContent = '⊞';
  }
})();

// ─── SEARCH PROFILES ───────────────────────────────────────────────────────
function _getProfiles() {
  try { return JSON.parse(localStorage.getItem('dr_profiles') || '[]'); } catch(e) { return []; }
}
function _saveProfiles(arr) {
  localStorage.setItem('dr_profiles', JSON.stringify(arr));
}
function _renderProfileSelect() {
  const sel = document.getElementById('profiles-select');
  const profiles = _getProfiles();
  const cur = sel.value;
  sel.innerHTML = '<option value="">-- saved profiles --</option>' +
    profiles.map(p => `<option value="${p.name}">${p.name}</option>`).join('');
  if (profiles.find(p => p.name === cur)) sel.value = cur;
}
function saveProfile() {
  const name = prompt('Profile name:', '');
  if (!name) return;
  const profiles = _getProfiles().filter(p => p.name !== name);
  profiles.push({ name, year: filterYear, location: filterLocation, status: filterStatus, sortKey, sortAsc });
  _saveProfiles(profiles);
  _renderProfileSelect();
  document.getElementById('profiles-select').value = name;
}
function loadProfile(name) {
  if (!name) return;
  const p = _getProfiles().find(x => x.name === name);
  if (!p) return;
  filterYear = p.year || 'all';
  filterLocation = p.location || 'all';
  filterStatus = p.status || 'active';
  sortKey = p.sortKey || 'change';
  sortAsc = !!p.sortAsc;
  // Sync UI
  document.getElementById('filter-status').value = filterStatus;
  document.getElementById('filter-location').value = filterLocation;
  document.querySelectorAll('.filter-btn').forEach(b => {
    if (['All','2022','2023','2024'].includes(b.textContent)) {
      b.classList.toggle('active', b.textContent === (filterYear === 'all' ? 'All' : filterYear));
    }
  });
  renderTable();
}
function deleteProfile() {
  const sel = document.getElementById('profiles-select');
  const name = sel.value;
  if (!name) return;
  if (!confirm(`Delete profile "${name}"?`)) return;
  _saveProfiles(_getProfiles().filter(p => p.name !== name));
  _renderProfileSelect();
}
_renderProfileSelect();

// ─── INIT ──────────────────────────────────────────────────────────────────
loadListings();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("\n  DealRadar Price Tracker")
    print("  Open → http://localhost:5001\n")
    app.run(debug=False, port=5001, host="0.0.0.0")
