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
    get_recent_runs,
    start_run,
    finish_run,
    get_setting,
    set_setting,
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
        listings = scrape(max_pages=10, headless=True, status_callback=log)
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
        return jsonify({"ok": True})
    return jsonify({"price_alert": get_setting("price_alert", "")})


@app.route("/api/listings/<int:listing_id>/history")
def listing_history(listing_id):
    data = get_price_history(listing_id)
    return jsonify(data)


@app.route("/api/market")
def market():
    return jsonify(get_market_snapshots())


@app.route("/api/runs")
def runs():
    return jsonify(get_recent_runs())


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
    grid-template-columns: 1fr 80px 80px 80px 80px;
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
</style>
</head>
<body>

<div class="header">
  <div class="header-brand">
    <span class="wordmark">DEALRADAR</span>
    <span class="subtitle">Price Intelligence · 2022–2024</span>
  </div>
  <div style="display:flex;gap:12px;align-items:center">
    <span id="velocity-badge" class="header-badge" style="display:none;font-weight:bold"></span>
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

<div id="market-insights" style="margin:0 auto 16px auto; max-width:1200px; padding:0 24px; font-size:12px; color:var(--muted); display:flex; justify-content:flex-end;">
  <span id="mileage-sweet-spot" style="background:var(--card); padding:4px 10px; border-radius:4px; border:1px solid var(--border); display:none">
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
        <span style="font-size:12px;color:var(--muted)">Price Alert:</span>
        <input type="number" id="alert-price" placeholder="R320000" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:90px;font-family:inherit;font-size:12px;outline:none">
        <button onclick="saveAlert()" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer;font-size:12px">Save</button>
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
        <option value="all">Any</option>
      </select>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
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
          <tr><td colspan="9" class="empty-state" style="padding:60px">
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
      <div class="chart-card">
        <div class="chart-title">Market Average Price · Over Time</div>
        <canvas id="chart-market" height="200"></canvas>
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
    </div>
  </div>

  <!-- Runs Tab -->
  <div id="tab-runs" style="display:none">
    <div style="background:var(--surface);border:1px solid var(--border);padding:20px">
      <div class="run-row" style="font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600">
        <div>STARTED</div><div>STATUS</div><div>FOUND</div><div>NEW</div><div>CHANGES</div>
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
}
async function saveAlert() {
  const val = document.getElementById('alert-price').value;
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({price_alert: val})
  });
  alert('Price alert updated!');
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
  let diffPct = (avgPrice - l.price) / avgPrice * 100;
  let milScore = (50000 - l.mileage) / 10000;
  let yearScore = (l.year - 2022) * 5;
  let score = Math.round(50 + (diffPct * 2) + milScore + yearScore);
  return Math.max(0, Math.min(100, score));
}

function renderTable() {
  let activeData = allListings.filter(l => l.is_active === 1);
  const prices = activeData.map(l => l.price).filter(Boolean);
  const avg = prices.length ? prices.reduce((a,b)=>a+b,0)/prices.length : 0;

  let data = allListings;
  if (filterStatus === 'active') data = data.filter(l => l.is_active === 1);
  if (filterStatus === 'gone') data = data.filter(l => l.is_active === 0);
  
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
    else if (sortKey === 'model') { valA = a.model||''; valB = b.model||''; }
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
    tbody.innerHTML = `<tr><td colspan="12"><div class="empty-state">
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

    // 1. #
    const tdRank = document.createElement('td');
    tdRank.className = 'rank-num';
    tdRank.textContent = i + 1;
    tr.appendChild(tdRank);

    // 2. Listing
    const tdListing = document.createElement('td');
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
    
    tdListing.appendChild(divTitle);
    tdListing.appendChild(divMeta);
    tr.appendChild(tdListing);

    // 2b. Model
    const tdMod = document.createElement('td');
    tdMod.style.fontFamily = 'monospace';
    tdMod.textContent = l.model || '—';
    tr.appendChild(tdMod);

    // 3. Source
    const tdSrc = document.createElement('td');
    const spanSrc = document.createElement('span');
    spanSrc.style.cssText = 'background:var(--tertiary); color:var(--text); padding:4px 8px; border-radius:4px; font-size:11px; white-space:nowrap';
    spanSrc.textContent = l.source || 'AutoTrader';
    tdSrc.appendChild(spanSrc);
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
  const marketRes = await fetch('/api/market').then(r => r.json());
  const market = marketRes.chart;
  const vel = marketRes.velocity_30d;
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

  // Market avg chart
  if (charts.market) charts.market.destroy();
  charts.market = new Chart(document.getElementById('chart-market'), {
    type: 'line',
    data: {
      labels: market.map(d => d.date),
      datasets: [{
        data: market.map(d => d.avg_price),
        borderColor: '#d4a843',
        backgroundColor: 'rgba(212,168,67,0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: 4,
        pointBackgroundColor: '#d4a843',
      }]
    },
    options: {
      ...chartDefaults,
      plugins: { ...chartDefaults.plugins },
      scales: {
        ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
      }
    }
  });

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
}

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
  el.innerHTML = runs.map(r => `
    <div class="run-row">
      <div>${fmtDate(r.started_at)}</div>
      <div><span class="status-dot ${r.status}"></span>${r.status}</div>
      <div>${r.listings_found ?? '—'}</div>
      <div style="color:var(--green)">${r.new_listings ?? '—'}</div>
      <div style="color:var(--gold)">${r.price_changes ?? '—'}</div>
    </div>
  `).join('');
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
