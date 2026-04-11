"""
Flask backend for the DealRadar price tracker.
Runs on http://localhost:5001
"""

import os
import sys
import threading
import queue
import time
import schedule
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request, Response, session, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from scraper import scrape, scrape_single_url
from database import (
    upsert_listings,
    get_listings_with_latest_price,
    get_price_history,
    get_price_changes,
    get_market_snapshots,
    get_day_of_week_prices,
    get_recent_runs,
    start_run,
    finish_run,
    get_setting,
    set_setting,
    toggle_watchlist,
    send_telegram_msg,
    get_telegram_updates,
    get_pre_approvals,
    add_pre_approval,
    update_pre_approval,
    delete_pre_approval,
    get_counter_offers,
    add_counter_offer,
    delete_counter_offer,
    get_sold_listings_with_estimates,
    get_week_of_month_prices,
    get_variant_stats,
)
from playwright.sync_api import sync_playwright

load_dotenv()

# ── Startup validation ────────────────────────────────────────────────────────
_REQUIRED_ENV = [
    "DEALRADAR_PASSWORD",
    "SESSION_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "FIREBASE_API_KEY",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_APP_ID",
]
_missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print("\n[DealRadar] ERROR — Missing required environment variables:")
    for v in _missing:
        print(f"  ✗ {v}")
    print("\nCopy .env.example → .env and fill in all values.\n")
    sys.exit(1)

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]

# -- Security Hardening --
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,  # Set to True if using HTTPS
    SESSION_COOKIE_SAMESITE='Lax',
)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

def require_auth(f):
    """Session-based auth. Redirects to /login for browser, 403 for API."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("authenticated"):
            return f(*args, **kwargs)
        # API requests get 403; browser requests get a redirect
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 403
        return redirect(url_for("login"))
    return decorated

def require_csrf(f):
    """Simple CSRF protection for API state changes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check for X-Requested-With header (standard for fetch/XHR)
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            # Also allow if the Origin or Referer matches our own host
            origin = request.headers.get("Origin")
            if origin and request.host_url.strip('/') in origin:
                 return f(*args, **kwargs)
            return jsonify({"error": "CSRF suspect - Missing X-Requested-With header"}), 403
        return f(*args, **kwargs)
    return decorated


# ── Login / Logout ────────────────────────────────────────────────────────────
_LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DealRadar · Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #080b10;
    color: #e6edf3;
    font-family: 'IBM Plex Mono', monospace;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .card {
    background: #0d1117;
    border: 1px solid #30363d;
    border-top: 3px solid #d4a843;
    padding: 48px 40px;
    width: 100%;
    max-width: 400px;
  }
  .logo { font-size: 22px; font-weight: 700; color: #d4a843; letter-spacing: 3px; margin-bottom: 8px; }
  .subtitle { font-size: 11px; color: #7d8590; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 36px; }
  label { font-size: 11px; color: #7d8590; text-transform: uppercase; letter-spacing: 1px; display: block; margin-bottom: 8px; }
  input {
    width: 100%;
    background: #080b10;
    border: 1px solid #30363d;
    color: #e6edf3;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 14px;
    padding: 12px 16px;
    margin-bottom: 24px;
    outline: none;
    transition: border-color 0.2s;
  }
  input:focus { border-color: #d4a843; }
  button {
    width: 100%;
    background: #d4a843;
    color: #080b10;
    border: none;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    padding: 14px;
    cursor: pointer;
    transition: opacity 0.2s;
  }
  button:hover { opacity: 0.85; }
  .error {
    background: rgba(248,81,73,0.1);
    border: 1px solid #f85149;
    color: #f85149;
    padding: 10px 14px;
    font-size: 12px;
    margin-bottom: 20px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="logo">DEALRADAR</div>
  <div class="subtitle">Price Intelligence Dashboard</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autofocus autocomplete="current-password">
    <button type="submit">Enter Dashboard</button>
  </form>
</div>
</body>
</html>
'''

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == os.environ.get("DEALRADAR_PASSWORD", ""):
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password. Try again."
    return render_template_string(_LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


REPORT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DealRadar Deal Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg: #080b10;
    --surface: #0d1117;
    --border: #30363d;
    --gold: #d4a843;
    --text: #e6edf3;
    --muted: #7d8590;
    --green: #3fb950;
    --red: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM+Plex+Mono', monospace;
    margin: 0;
    padding: 40px;
    width: 210mm; /* A4 width */
    min-height: 297mm;
  }
  .report-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    border-bottom: 2px solid var(--gold);
    padding-bottom: 20px;
    margin-bottom: 30px;
  }
  .logo { font-size: 24px; font-weight: 700; color: var(--gold); letter-spacing: 2px; }
  .report-info { text-align: right; color: var(--muted); font-size: 12px; }
  
  .listing-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 30px;
    margin-bottom: 30px;
  }
  .listing-title { font-size: 20px; font-weight: 700; margin-bottom: 10px; }
  .listing-meta { font-size: 13px; color: var(--muted); margin-bottom: 20px; }
  
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 15px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .stat-value { font-size: 18px; font-weight: 600; color: var(--gold); }
  
  .market-analysis {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 20px;
    margin-bottom: 30px;
  }
  .section-title {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--gold);
    margin-bottom: 15px;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .chart-container { height: 200px; margin-bottom: 30px; }
  
  .neg-box {
    background: rgba(212, 168, 67, 0.05);
    border: 1px solid var(--gold);
    padding: 20px;
    margin-bottom: 30px;
  }
  .neg-script {
    font-style: italic;
    color: var(--text);
    margin-top: 15px;
    padding-left: 15px;
    border-left: 3px solid var(--gold);
  }

  .similar-deals-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 15px;
  }
  .similar-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 12px;
    font-size: 11px;
  }

  .footer {
    margin-top: 50px;
    font-size: 10px;
    color: var(--muted);
    text-align: center;
    border-top: 1px solid var(--border);
    padding-top: 20px;
  }
</style>
</head>
<body>
  <div class="report-header">
    <div class="logo">DEALRADAR</div>
    <div class="report-info">
      VALUATION REPORT · {{ listing_id }}<br>
      GENERATED: <span id="gen-date"></span>
    </div>
  </div>

  <div id="loading" style="text-align:center; padding:50px; color:var(--muted)">Generating Deal Intel...</div>

  <div id="content" style="display:none">
    <div class="listing-title" id="l-title">...</div>
    <div class="listing-meta" id="l-meta">...</div>

    <div class="listing-grid">
      <div class="stat-card">
        <div class="stat-label">Current Asking Price</div>
        <div class="stat-value" id="l-price">...</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Market Context</div>
        <div class="stat-value" id="l-context">...</div>
      </div>
    </div>

    <div class="market-analysis">
      <div class="section-title">📉 Price History Trends</div>
      <div class="chart-container">
        <canvas id="historyChart"></canvas>
      </div>
    </div>

    <div class="neg-box">
      <div class="section-title">🤝 Negotiation Strategy</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px">
        <div>
          <div class="stat-label">Opening Offer</div>
          <div class="stat-value" id="l-opening" style="color:var(--green)">...</div>
        </div>
        <div>
          <div class="stat-label">Target / Walk-away</div>
          <div class="stat-value" id="l-target">...</div>
        </div>
      </div>
      <div class="stat-label">Suggested Script</div>
      <div class="neg-script" id="l-script">...</div>
    </div>

    <div>
      <div class="section-title">🚗 Similar Market Listings</div>
      <div class="similar-deals-grid" id="similar-list"></div>
    </div>

    <div class="footer">
      DealRadar Price Intelligence · Confidential Dealer Report · Professional Use Only
    </div>
  </div>

<script>
  const listingId = {{ listing_id }};
  const fmt = (n) => 'R ' + Number(n).toLocaleString('en-ZA');

  async function init() {
    document.getElementById('gen-date').textContent = new Date().toLocaleDateString('en-ZA', { year:'numeric', month:'long', day:'numeric' });
    
    // Fetch Data
    const listings = await fetch('/api/listings?include_inactive=1').then(r => r.json());
    const me = listings.find(x => x.id === listingId);
    if (!me) return;

    const history = await fetch(`/api/listings/${listingId}/history`).then(r => r.json());
    
    // Market Avg
    const activePrices = listings.filter(l => l.is_active && l.price).map(l => l.price);
    const marketAvg = activePrices.length ? activePrices.reduce((a,b)=>a+b,0)/activePrices.length : 0;

    // Fill UI
    document.getElementById('l-title').textContent = [me.year, me.title, me.variant].filter(Boolean).join(' ');
    document.getElementById('l-meta').textContent = `${me.location || 'Anywhere'} · ${me.mileage ? me.mileage.toLocaleString() : '—'} km · ${me.dealer || 'Private Seller'} · ${me.source || 'AutoTrader'}`;
    document.getElementById('l-price').textContent = fmt(me.price);
    
    const diff = marketAvg ? ((me.price - marketAvg) / marketAvg * 100) : 0;
    document.getElementById('l-context').textContent = `${Math.abs(diff).toFixed(1)}% ${diff > 0 ? 'Above' : 'Below'} Average`;
    document.getElementById('l-context').style.color = diff > 0 ? 'var(--red)' : 'var(--green)';

    // Negotiation Logic (Clone from app.js)
    let discount = 0;
    if (me.first_seen) {
        const end = (me.is_active || !me.last_seen) ? new Date() : new Date(me.last_seen);
        const dom = Math.floor((end - new Date(me.first_seen)) / 86400000);
        if (dom >= 60) discount += 6;
        else if (dom >= 30) discount += 4;
        else if (dom >= 15) discount += 2;
    }
    if (marketAvg && me.price) {
        const d = ((me.price - marketAvg) / marketAvg * 100);
        if (d > 10) discount += 4;
        else if (d > 5) discount += 2;
    }
    const opening = Math.round((me.price * (1 - (discount + 3) / 100)) / 1000) * 1000;
    const target = Math.round((me.price * (1 - discount / 100)) / 1000) * 1000;
    
    document.getElementById('l-opening').textContent = fmt(opening);
    document.getElementById('l-target').textContent = fmt(target);
    document.getElementById('l-script').textContent = `"I've done my research and similar vehicles are going for around ${fmt(Math.round(marketAvg/1000)*1000)}. Would you consider ${fmt(opening)}?"`;

    // History Chart
    new Chart(document.getElementById('historyChart'), {
      type: 'line',
      data: {
        labels: history.map(h => h.scraped_at.slice(0, 10)),
        datasets: [{
          data: history.map(h => h.price),
          borderColor: '#d4a843',
          backgroundColor: 'rgba(212, 168, 67, 0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 4,
          pointBackgroundColor: '#d4a843',
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: '#30363d' }, ticks: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 } } },
          y: { grid: { color: '#30363d' }, ticks: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 }, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
        }
      }
    });

    // Similar Listings
    const similar = listings
        .filter(x => x.id !== listingId && x.price && x.year && x.is_active)
        .map(x => ({ ...x, _dist: Math.abs(x.price - me.price)/10000 + Math.abs(x.year - me.year)*5 + Math.abs((x.mileage||0) - (me.mileage||0))/10000 }))
        .sort((a,b) => a._dist - b._dist)
        .slice(0, 3);
    
    const simEl = document.getElementById('similar-list');
    similar.forEach(s => {
      const card = document.createElement('div');
      card.className = 'similar-card';
      card.innerHTML = `<strong>${s.year} ${s.title}</strong><br><span style="color:var(--muted)">${s.location||'Anywhere'} &middot; ${s.mileage?s.mileage.toLocaleString():'0'}km</span><br><span style="color:var(--gold);font-weight:700">${fmt(s.price)}</span>`;
      simEl.appendChild(card);
    });

    document.getElementById('loading').style.display = 'none';
    document.getElementById('content').style.display = 'block';
  }

  init();
</script>
</body>
</html>
"""

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
        max_price_str = get_setting("max_price", "")
        max_price = int(max_price_str) if max_price_str and max_price_str.isdigit() else None
        
        listings = scrape(max_pages=10, headless=True, status_callback=log, wbc_url=wbc_url, max_price=max_price)
        stats = upsert_listings(listings)
        stats["total"] = len(listings)
        finish_run(run_id, stats)
        log(f"✓ Done — {len(listings)} listings, {stats['new']} new, {stats['price_changes']} price changes")

        # ── Firestore sync ────────────────────────────────────────────────────
        try:
            from sync_service import sync_to_firestore
            log("Syncing to Firebase...")
            sync_to_firestore(status_callback=log)
            log("✓ Sync complete")
        except Exception as sync_err:
            log(f"⚠ Sync error (non-fatal): {sync_err}")
        # ─────────────────────────────────────────────────────────────────────

    except Exception as e:
        error = str(e)
        log(f"✗ Error: {error}")
        finish_run(run_id, {"total": len(listings), "new": 0, "price_changes": 0}, error=error)
    finally:
        _scrape_status["running"] = False



@app.route("/")
@require_auth
def index():
    return render_template_string(HTML)


@app.route("/api/scrape", methods=["POST"])
@require_auth
@require_csrf
@limiter.limit("1 per minute")
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
@require_auth
@require_csrf
def trigger_scrape_url():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
        
    # Security: URL Validation
    allowed_domains = ["autotrader.co.za", "webuycars.co.za"]
    if not any(domain in url.lower() for domain in allowed_domains):
        return jsonify({"error": "Invalid domain. Only AutoTrader and WeBuyCars URLs are allowed."}), 400
        
    with _scrape_lock:
        if _scrape_status["running"]:
            return jsonify({"error": "Scrape already in progress"}), 409
        _scrape_status["running"] = True
        _scrape_status["log"] = ["Starting single URL scrape..."]
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
@require_auth
def scrape_status():
    return jsonify({
        "running": _scrape_status["running"],
        "log": _scrape_status["log"][-50:],  # last 50 lines
    })


@app.route("/api/listings")
@require_auth
def listings():
    include_inactive = request.args.get("include_inactive", "0") == "1"
    data = get_listings_with_latest_price(include_inactive=include_inactive)
    return jsonify(data)

@app.route("/api/settings", methods=["GET", "POST"])
@require_auth
@require_csrf
def settings_api():
    if request.method == "POST":
        data = request.json or {}
        if "price_alert" in data:
            set_setting("price_alert", str(data["price_alert"]))
        if "wbc_url" in data:
            set_setting("wbc_url", str(data["wbc_url"]))
        if "max_price" in data:
            set_setting("max_price", str(data["max_price"]))
        if "telegram_token" in data:
            set_setting("telegram_token", str(data["telegram_token"]).strip())
        if "telegram_chat_id" in data:
            set_setting("telegram_chat_id", str(data["telegram_chat_id"]).strip())
        if "admin_password" in data:
            set_setting("admin_password", str(data["admin_password"]).strip())
        return jsonify({"ok": True})
    return jsonify({
        "price_alert": get_setting("price_alert", ""),
        "wbc_url": get_setting("wbc_url", ""),
        "max_price": get_setting("max_price", ""),
        "telegram_token": get_setting("telegram_token", ""),
        "telegram_chat_id": get_setting("telegram_chat_id", ""),
        "admin_password": get_setting("admin_password", "")
    })

@app.route("/api/test-telegram", methods=["POST"])
@require_auth
def test_telegram():
    data = request.json or {}
    token = data.get("telegram_token")
    chat_id = data.get("telegram_chat_id")
    
    res = send_telegram_msg(
        "<b>DealRadar</b>\nThis is a test notification! Your bot is configured correctly. 🚀",
        token_override=token,
        chat_id_override=chat_id
    )
    
    if res and isinstance(res, (bytes, str)):
        return jsonify({"ok": True})
    
    error_msg = "Unknown error"
    if isinstance(res, dict) and "error" in res:
        error_msg = res["error"]
        
    return jsonify({"error": f"Failed to send message: {error_msg}"}), 400


@app.route("/api/listings/<int:listing_id>/history")
@require_auth
def listing_history(listing_id):
    data = get_price_history(listing_id)
    return jsonify(data)


@app.route("/api/market")
@require_auth
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
@require_auth
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
@require_auth
def runs():
    return jsonify(get_recent_runs())


@app.route("/api/price-changes")
@require_auth
def price_changes():
    return jsonify(get_price_changes())


@app.route("/api/listings/<int:listing_id>/watchlist", methods=["POST"])
@require_auth
def toggle_watchlist_route(listing_id):
    new_state = toggle_watchlist(listing_id)
    return jsonify({"watchlisted": new_state})


@app.route("/api/finance/pre-approvals", methods=["GET"])
@require_auth
def pre_approvals_get():
    return jsonify(get_pre_approvals())


@app.route("/api/finance/pre-approvals", methods=["POST"])
@require_auth
def pre_approvals_add():
    data = request.json or {}
    app_id = add_pre_approval(data)
    return jsonify({"ok": True, "id": app_id})


@app.route("/api/finance/pre-approvals/<int:app_id>", methods=["PUT"])
@require_auth
def pre_approvals_update(app_id):
    data = request.json or {}
    success = update_pre_approval(app_id, data)
    return jsonify({"ok": success})


@app.route("/api/finance/pre-approvals/<int:pre_id>", methods=["DELETE"])
@require_auth
def delete_pre_app(pre_id):
    delete_pre_approval(pre_id)
    return jsonify({"ok": True})

# ─── COUNTER OFFERS ────────────────────────────────────────────────────────
@app.route("/api/listings/<int:id>/counter-offers", methods=["GET"])
@require_auth
def get_offers(id):
    return jsonify(get_counter_offers(id))

@app.route("/api/listings/<int:id>/counter-offers", methods=["POST"])
@require_auth
def add_offer(id):
    d = request.json
    add_counter_offer(
        id, 
        d.get("date"), 
        d.get("my_offer"), 
        d.get("dealer_counter"), 
        d.get("notes"), 
        d.get("status")
    )
    return jsonify({"ok": True})

@app.route("/api/counter-offers/<int:offer_id>", methods=["DELETE"])
@require_auth
def delete_offer_item(offer_id):
    delete_counter_offer(offer_id)
    return jsonify({"ok": True})

# ─── PDF REPORT ────────────────────────────────────────────────────────────
@app.route("/api/listings/<int:id>/report")
@require_auth
def generate_report(id):
    """
    Generates a professional PDF Deal Report for a specific listing using Playwright.
    """
    from playwright.sync_api import sync_playwright
    import time
    
    host = request.host_url.rstrip("/")
    report_url = f"{host}/report/{id}"
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1600})
        
        page.goto(report_url, wait_until="networkidle")
        time.sleep(1) 
        
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "20px", "right": "20px", "bottom": "20px", "left": "20px"}
        )
        browser.close()
    
    from flask import make_response
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=DealRadar_Report_{id}.pdf'
    return response

@app.route("/report/<int:id>")
@require_auth
def report_view(id):
    return render_template_string(REPORT_HTML, listing_id=id)


# ─── INTELLIGENCE ─────────────────────────────────────────────────────────
@app.route("/api/intelligence/sold")
@require_auth
def intel_sold():
    return jsonify(get_sold_listings_with_estimates())

@app.route("/api/intelligence/seasonal")
@require_auth
def intel_seasonal():
    return jsonify({
        "dow": get_day_of_week_prices(),
        "wom": get_week_of_month_prices()
    })

@app.route("/api/intelligence/variants")
@require_auth
def intel_variants():
    return jsonify(get_variant_stats())


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
    --yellow: #f1e05a;
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
  .main { padding: 28px 32px; max-width: 1800px; margin: 0 auto; }

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
  /* COMPARE TABLE */
  .cmp-table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    margin-top: 20px;
    font-size: 12px;
  }
  .cmp-table th, .cmp-table td {
    padding: 10px 14px;
    border: 1px solid var(--border);
    vertical-align: middle;
    text-align: left;
    overflow-wrap: break-word;
    word-break: break-word;
  }
  .cmp-table .row-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 1px;
    text-transform: uppercase;
    background: var(--bg);
    white-space: nowrap;
    width: 130px;
  }
  .cmp-table thead th {
    background: var(--bg);
    vertical-align: top;
    white-space: normal;
    word-break: break-word;
  }
  .cmp-table tbody tr:nth-child(even) { background: rgba(255,255,255,0.02); }
  .cmp-table tbody tr:hover { background: rgba(212,168,67,0.04); }
  .cmp-table td.best { color: var(--green); font-weight: 700; }
  .cmp-table td.worst { color: var(--red); }

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

  /* FINANCE TAB */
  .finance-grid {
    display: grid;
    grid-template-columns: 350px 1fr;
    gap: 24px;
    align-items: start;
  }
  .finance-card {
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 24px;
    margin-bottom: 24px;
  }
  .finance-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--gold);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .finance-input-group {
    margin-bottom: 16px;
  }
  .finance-input-group label {
    display: block;
    font-size: 10px;
    color: var(--muted);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .finance-input-group input, .finance-input-group select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border2);
    color: var(--text);
    padding: 10px 12px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    outline: none;
  }
  .finance-result-row {
    display: flex;
    justify-content: space-between;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
  }
  .finance-result-row:last-child { border-bottom: none; }
  .finance-result-label { font-size: 12px; color: var(--muted); }
  .finance-result-value { font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 700; color: var(--text); }
  .finance-result-value.gold { color: var(--gold); }

  .status-badge {
    padding: 2px 8px;
    font-size: 10px;
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 700;
    text-transform: uppercase;
    border-radius: 2px;
  }
  .status-badge.approved { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid rgba(63,185,80,0.3); }
  .status-badge.declined { background: rgba(248,81,73,0.15); color: var(--red); border: 1px solid rgba(248,81,73,0.3); }
  .status-badge.pending { background: rgba(241,224,90,0.15); color: var(--yellow); border: 1px solid rgba(241,224,90,0.3); }

  .finance-table th { font-size: 9px; }
  .finance-table td { font-size: 12px; font-family: 'IBM Plex Mono', monospace; }
  
  .action-btn {
    background: none;
    border: 1px solid var(--border2);
    color: var(--muted);
    padding: 4px 8px;
    font-size: 10px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .action-btn:hover { border-color: var(--gold); color: var(--gold); }

  /* COUNTER OFFERS */
  .offer-row {
    border-bottom: 1px solid var(--border);
    padding: 12px 0;
  }
  .offer-row:last-child { border-bottom: none; }
  .offer-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .offer-status {
    padding: 2px 8px;
    font-size: 10px;
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 700;
    text-transform: uppercase;
    border-radius: 2px;
  }
  .status-ongoing { background: rgba(212,168,67,0.15); color: var(--gold); border: 1px solid rgba(212,168,67,0.3); }
  .status-accepted { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid rgba(63,185,80,0.3); }
  .status-walked { background: rgba(248,81,73,0.15); color: var(--red); border: 1px solid rgba(248,113,113,0.3); }
  
  .print-btn {
    background: var(--gold);
    color: #080b10;
    border: none;
    padding: 10px 16px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    cursor: pointer;
    border-radius: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .print-btn:disabled { opacity: 0.5; cursor: not-allowed; }
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
    <a href="/logout" style="background:#1c1f26;border:1px solid #30363d;color:#7d8590;font-family:inherit;font-size:11px;letter-spacing:1px;text-transform:uppercase;padding:6px 14px;text-decoration:none;transition:color 0.2s,border-color 0.2s" onmouseover="this.style.color='#d4a843';this.style.borderColor='#d4a843'" onmouseout="this.style.color='#7d8590';this.style.borderColor='#30363d'">Logout</a>
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

<div id="market-insights" style="margin:0 auto 16px auto; max-width:1800px; padding:0 32px; font-size:12px; color:var(--muted); display:flex; justify-content:flex-end; gap:8px; flex-wrap:wrap;">
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
        <input type="text" id="wbc-url" placeholder="WeBuyCars search URL..." style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:180px;font-family:inherit;font-size:12px;outline:none">
        <span style="font-size:12px;color:var(--muted)">Max Price:</span>
        <input type="number" id="max-price" placeholder="R450000" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:90px;font-family:inherit;font-size:12px;outline:none">
        <span style="font-size:12px;color:var(--muted)">Alert:</span>
        <input type="number" id="alert-price" placeholder="R320000" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:90px;font-family:inherit;font-size:12px;outline:none">
        <button onclick="saveSettings()" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer;font-size:12px">Save</button>
      </div>
    </div>
    
    <!-- Telegram Settings Row -->
    <div style="display:flex; gap:12px; align-items:center; margin-top:12px; padding-top:12px; border-top:1px solid var(--border)">
      <div style="display:flex; align-items:center; gap:8px">
        <i class="icon" style="color:#229ED9; font-style:normal; font-size:14px">✈</i>
        <span style="font-size:12px; color:var(--text); font-weight:600">Telegram Bot Notifications:</span>
      </div>
      <input type="password" id="tele-token" placeholder="Bot Token" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:150px;font-family:inherit;font-size:12px;outline:none">
      <input type="text" id="tele-chat" placeholder="Chat ID" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:80px;font-family:inherit;font-size:12px;outline:none">
      <input type="password" id="admin-pass" placeholder="Admin Password" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:4px;width:120px;font-family:inherit;font-size:12px;outline:none">
      <button onclick="testTelegram()" id="test-tele-btn" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;cursor:pointer;font-size:12px">Test</button>
      <span style="font-size:11px; color:var(--muted)">Password protects this page.</span>
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
    <button class="tab-btn" onclick="setTab('price-changes', this)">Price Changes</button>
    <button class="tab-btn" onclick="setTab('finance', this)">Finance</button>
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
        <option value="all">Any</option>
      </select>
      <button id="loc-btn" onclick="openLocModal()" style="margin-left:16px;background:none;border:1px solid var(--border);color:var(--muted);padding:4px 10px;font-family:inherit;font-size:12px;cursor:pointer;border-radius:4px">📍 Set my location</button>
    </div>
    <div id="chart-filter-badge" onclick="clearChartFilter()" style="display:none;background:rgba(212,168,67,0.1);border:1px solid var(--gold);color:var(--gold);font-size:11px;font-family:'IBM Plex Mono',monospace;padding:6px 14px;border-radius:4px;cursor:pointer;margin-bottom:8px"></div>
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
            <th class="sortable" onclick="setSort('distance')" id="th-distance" style="display:none">Dist. <span class="sort-icon" id="sort-icon-distance"></span></th>
            <th class="sortable" onclick="setSort('status')">Status / Time to Sell <span class="sort-icon" id="sort-icon-status"></span></th>
            <th></th>
          </tr>
        </thead>
        <tbody id="listings-tbody">
          <tr><td colspan="16" class="empty-state" style="padding:60px">
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
      <!-- Seasonal Intelligence -->
      <div class="chart-card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Seasonal Buy-Signals · Day of Week</div>
          <div id="best-dow-badge" style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--green)"></div>
        </div>
        <canvas id="chart-dow-new" height="200"></canvas>
      </div>
      <div class="chart-card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Seasonal Buy-Signals · Week of Month</div>
          <div id="best-wom-badge" style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--gold)"></div>
        </div>
        <canvas id="chart-wom" height="200"></canvas>
      </div>

      <!-- Variant Breakdown -->
      <div class="chart-card" style="grid-column:1/-1">
        <div class="chart-title">Variant Value Analysis · Avg Price by Trim/Spec</div>
        <canvas id="chart-variant-analysis" height="100"></canvas>
      </div>

      <!-- Existing Charts -->
      <div class="chart-card" style="grid-column:1/-1">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
          <div class="chart-title" style="margin-bottom:0">Historical Price Trend + 30-Day Forecast</div>
          <div id="forecast-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:3px 10px;border-radius:3px;border:1px solid"></div>
        </div>
        <canvas id="chart-forecast" height="120"></canvas>
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
          <div id="heatmap-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--gold)"></div>
        </div>
        <canvas id="chart-heatmap" height="80"></canvas>
      </div>
    </div>

    <!-- Sold Listings Intelligence -->
    <div style="background:var(--surface); border:1px solid var(--border); padding:24px; margin-top:24px">
      <div class="chart-title" style="margin-bottom:16px; display:flex; align-items:center; gap:10px">
        <span style="font-size:16px">🏷️</span> Sold Price Estimator
        <span style="font-size:10px; color:var(--muted); font-weight:normal; text-transform:none; margin-left:auto">Likely sold based on inactivity. Estimates include DOM & drop weighted factors.</span>
      </div>
      <div class="table-wrap">
        <table class="finance-table" style="font-size:11px">
          <thead>
            <tr>
              <th>Vehicle / Source</th>
              <th>First Seen</th>
              <th>Last Seen</th>
              <th>Days</th>
              <th>Last Listed</th>
              <th>Drops</th>
              <th style="color:var(--gold)">Est. Sold Price</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="sold-tbody">
            <tr><td colspan="8" style="text-align:center; padding:30px; color:var(--dim)">Calculating market exit signals...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Price Changes Tab -->
  <div id="tab-price-changes" style="display:none">
    <div style="background:var(--surface);border:1px solid var(--border);padding:20px;margin-bottom:12px">
      <div style="font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600;margin-bottom:12px">DAILY SUMMARY</div>
      <div id="pc-daily" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px"></div>
    </div>
    <div style="background:var(--surface);border:1px solid var(--border);padding:20px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <div style="font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600">ALL PRICE CHANGES — FULL HISTORY</div>
        <div style="display:flex;align-items:center;gap:12px">
          <div id="pc-filter-badge" style="font-size:11px;color:var(--gold)"></div>
          <div id="pc-summary" style="font-size:12px;color:var(--muted)"></div>
        </div>
      </div>
      <div id="pc-list"><div style="color:var(--muted);font-size:12px;padding:20px 0">Loading…</div></div>
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

  <!-- Finance Tab -->
  <div id="tab-finance" style="display:none">
    <div class="finance-grid">
      <!-- Calculators Column -->
      <div class="finance-left">
        <!-- Affordability Calculator -->
        <div class="finance-card">
          <div class="finance-title">
            <span style="font-size:16px">🧮</span> Affordability Calculator
          </div>
          <div class="finance-input-group">
            <label>Gross Monthly Salary (R)</label>
            <input type="number" id="calc-gross" value="45000" oninput="calcAffordability()">
          </div>
          <div class="finance-input-group">
            <label>Net Monthly Income (R)</label>
            <input type="number" id="calc-net" value="32000" oninput="calcAffordability()">
          </div>
          <div class="finance-input-group">
            <label>Suggested Deposit (R)</label>
            <input type="number" id="calc-deposit" value="50000" oninput="calcAffordability()">
          </div>
          <div class="finance-input-group">
            <label>Preferred Term (Months)</label>
            <select id="calc-term" onchange="calcAffordability()">
              <option value="48">48 months</option>
              <option value="60" selected>60 months</option>
              <option value="72">72 months</option>
              <option value="84">84 months</option>
            </select>
          </div>
          
          <div style="margin-top:24px; padding-top:16px; border-top:1px dashed var(--border2)">
            <div class="finance-result-row">
              <div class="finance-result-label">Recommended Max Price</div>
              <div class="finance-result-value gold" id="res-max-price">R0</div>
            </div>
            <div class="finance-result-row">
              <div class="finance-result-label">Max Monthly Instalment (20%)</div>
              <div class="finance-result-value" id="res-max-instalment">R0</div>
            </div>
            <div class="finance-result-row">
              <div class="finance-result-label">Total Repayment Amount</div>
              <div class="finance-result-value" id="res-total-repay">R0</div>
            </div>
          </div>
          <div style="margin-top:16px; font-size:11px; color:var(--muted); font-style:italic">
            *Based on 20% of net income rule and average market rates.
          </div>
        </div>

        <!-- Bank Rate Comparsion Inputs (Syncs with the table) -->
        <div class="finance-card">
          <div class="finance-title">
            <span style="font-size:16px">⚖️</span> Loan Comparison Settings
          </div>
          <div class="finance-input-group">
            <label>Vehicle Price (R)</label>
            <input type="number" id="comp-price" value="350000" oninput="renderBankComparison()">
          </div>
          <div class="finance-input-group">
            <label>Deposit (R)</label>
            <input type="number" id="comp-deposit" value="50000" oninput="renderBankComparison()">
          </div>
          <div class="finance-input-group">
            <label>Loan Term (Months)</label>
            <select id="comp-term" onchange="renderBankComparison()">
              <option value="48">48 months</option>
              <option value="60" selected>60 months</option>
              <option value="72">72 months</option>
              <option value="84">84 months</option>
            </select>
          </div>
        </div>
      </div>

      <!-- Tables Column -->
      <div class="finance-right">
        <!-- Pre-approval Tracker -->
        <div class="finance-card">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
            <div class="finance-title" style="margin-bottom:0">
              <span style="font-size:16px">🏦</span> Pre-approval Tracker
            </div>
            <button class="action-btn" onclick="openPreApprovalModal()" style="padding:6px 12px; border-radius:4px; background:var(--gold); color:#080b10; font-weight:700">Add Application</button>
          </div>
          <div class="table-wrap">
            <table class="finance-table">
              <thead>
                <tr>
                  <th>Bank</th>
                  <th>Date</th>
                  <th>Amount</th>
                  <th>Rate</th>
                  <th>Instalment</th>
                  <th>Status</th>
                  <th>Notes</th>
                  <th></th>
                </tr>
              </thead>
              <tbody id="pre-approval-tbody">
                <tr><td colspan="8" style="text-align:center; padding:40px; color:var(--dim)">No applications logged yet.</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Bank Rate Comparison -->
        <div class="finance-card">
          <div class="finance-title">
            <span style="font-size:16px">📊</span> Bank Rate Comparison
          </div>
          <div class="table-wrap">
            <table class="finance-table">
              <thead>
                <tr>
                  <th>Bank</th>
                  <th>Interest Rate</th>
                  <th>Monthly Instalment</th>
                  <th>Total Repayment</th>
                  <th>Total Interest</th>
                </tr>
              </thead>
              <tbody id="bank-comp-tbody">
                <!-- Dynamically Rendered -->
              </tbody>
            </table>
          </div>
          <div style="margin-top:16px; font-size:11px; color:var(--muted)">
             Average market rates pre-filled. You can edit rates in the Pre-approval tracker above for accuracy.
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Pre-approval Modal -->
  <div class="modal-overlay" id="pre-approval-modal">
    <div class="modal" style="width:450px">
      <div class="modal-title" id="pre-title">Add Pre-approval</div>
      <div class="modal-subtitle">Log your bank application results.</div>
      
      <div class="finance-input-group">
        <label>Bank Name</label>
        <input type="text" id="pre-bank" placeholder="e.g. WesBank">
      </div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
        <div class="finance-input-group">
          <label>Date Applied</label>
          <input type="date" id="pre-date">
        </div>
        <div class="finance-input-group">
          <label>Status</label>
          <select id="pre-status">
            <option value="Pending">Pending</option>
            <option value="Approved">Approved</option>
            <option value="Declined">Declined</option>
          </select>
        </div>
      </div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
        <div class="finance-input-group">
          <label>Amount (R)</label>
          <input type="number" id="pre-amount">
        </div>
        <div class="finance-input-group">
          <label>Interest Rate (%)</label>
          <input type="number" id="pre-rate" step="0.1" value="11.75">
        </div>
      </div>
      <div class="finance-input-group">
        <label>Monthly Instalment (Estimated/Actual)</label>
        <input type="number" id="pre-instalment">
      </div>
      <div class="finance-input-group">
        <label>Notes</label>
        <input type="text" id="pre-notes" placeholder="e.g. Includes balloon payment">
      </div>

      <div style="display:flex; gap:12px; margin-top:20px">
        <button class="action-btn" onclick="savePreApproval()" style="flex:1; background:var(--gold); color:#080b10; font-weight:700; padding:10px">Save Application</button>
        <button class="action-btn" onclick="closePreApprovalModal()" style="flex:1; padding:10px">Cancel</button>
      </div>
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

    <div style="display:flex; justify-content:flex-end; margin-bottom:16px">
      <button id="report-btn" class="print-btn" onclick="downloadReport()">
        <span>📄</span> Download Deal Report
      </button>
    </div>

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

    <!-- Negotiation Log (Counter Offer Tracker) -->
    <div id="counter-offer-wrap" style="margin-top:24px;">
      <div style="font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px">Negotiation Log</div>
      
      <!-- New Offer Form -->
      <div style="background:rgba(212,168,67,0.05); border:1px solid var(--border); padding:16px; margin-bottom:16px">
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:12px">
          <div>
            <div style="font-size:10px;color:var(--muted);margin-bottom:4px">MY OFFER (R)</div>
            <input id="co-my-offer" type="number" step="1000" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px;font-size:12px">
          </div>
          <div>
            <div style="font-size:10px;color:var(--muted);margin-bottom:4px">DEALER COUNTER (R)</div>
            <input id="co-dealer-counter" type="number" step="1000" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px;font-size:12px">
          </div>
          <div>
            <div style="font-size:10px;color:var(--muted);margin-bottom:4px">STATUS</div>
            <select id="co-status" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px;font-size:12px">
              <option value="ongoing">Ongoing</option>
              <option value="accepted">Accepted</option>
              <option value="walked away">Walked Away</option>
            </select>
          </div>
        </div>
        <div style="display:flex; gap:10px">
          <input id="co-notes" type="text" placeholder="Add some notes..." style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px;font-size:12px">
          <button class="action-btn" onclick="saveCounterOffer()" style="background:var(--gold); color:#080b10; font-weight:700">Log Offer</button>
        </div>
      </div>

      <!-- Offer History List -->
      <div id="counter-offer-list">
        <!-- Dynamically filled -->
      </div>
    </div>

    <button class="modal-close" style="margin-top:24px" onclick="closeModal()">Close</button>
  </div>
</div>

<script>
let allListings = [];
let filterYear = 'all';
let filterLocation = 'all';
let filterStatus = 'active';
let filterPriceMin = 0;
let filterPriceMax = 0;
let sortKey = 'change';
let sortAsc = false;

// ── User location & distance ──────────────────────────────────────────────────
let userLocation = null; // { lat, lng, label }
(function() {
  try {
    const saved = localStorage.getItem('dealradar_userloc');
    if (saved) userLocation = JSON.parse(saved);
  } catch(e) {}
})();

const SA_CITIES = {
  'johannesburg':[-26.2041,28.0473],'joburg':[-26.2041,28.0473],'jhb':[-26.2041,28.0473],
  'sandton':[-26.1070,28.0567],'midrand':[-25.9974,28.1347],'randburg':[-26.0940,27.9906],
  'roodepoort':[-26.1625,27.8697],'germiston':[-26.2278,28.1681],'boksburg':[-26.2145,28.2601],
  'kempton park':[-26.0982,28.2298],'edenvale':[-26.1478,28.1602],'alberton':[-26.2690,28.1232],
  'benoni':[-26.1879,28.3188],'brakpan':[-26.2348,28.3663],'springs':[-26.2495,28.4479],
  'soweto':[-26.2678,27.8585],'fourways':[-26.0178,28.0117],'woodmead':[-26.0620,28.1013],
  'sunninghill':[-26.0451,28.0898],'rivonia':[-26.0532,28.0598],'bryanston':[-26.0680,28.0104],
  'northgate':[-26.1066,27.9695],'rosebank':[-26.1476,28.0426],'parktown':[-26.1877,28.0432],
  'kyalami':[-25.9949,28.0727],'dainfern':[-26.0052,28.0250],'diepsloot':[-25.9370,28.0050],
  'krugersdorp':[-26.0974,27.7658],'randfontein':[-26.1844,27.6882],'florida':[-26.1693,27.9116],
  'pretoria':[-25.7479,28.2293],'tshwane':[-25.7479,28.2293],'centurion':[-25.8600,28.1892],
  'hatfield':[-25.7481,28.2342],'menlyn':[-25.7869,28.2775],'lynnwood':[-25.7681,28.2919],
  'soshanguve':[-25.5290,28.1008],'ga-rankuwa':[-25.6302,27.9984],'mabopane':[-25.5776,28.0741],
  'cape town':[-33.9249,18.4241],'cpt':[-33.9249,18.4241],'bellville':[-33.8999,18.6297],
  'brackenfell':[-33.8804,18.6804],'tygervalley':[-33.8740,18.6256],'parow':[-33.9025,18.5986],
  'claremont':[-33.9876,18.4656],'tokai':[-34.0626,18.4625],'tableview':[-33.8311,18.4897],
  'century city':[-33.8930,18.5125],'montague gardens':[-33.8727,18.5281],
  'paarl':[-33.7303,18.9640],'stellenbosch':[-33.9321,18.8602],'somerset west':[-34.0831,18.8373],
  'strand':[-34.1167,18.8317],'george':[-33.9646,22.4617],
  'durban':[-29.8587,31.0218],'dbn':[-29.8587,31.0218],'pinetown':[-29.8181,30.8622],
  'umhlanga':[-29.7309,31.0838],'westville':[-29.8386,30.9301],'chatsworth':[-29.9125,30.9270],
  'pietermaritzburg':[-29.6006,30.3794],'pmb':[-29.6006,30.3794],
  'port elizabeth':[-33.9608,25.6022],'gqeberha':[-33.9608,25.6022],'pe':[-33.9608,25.6022],
  'east london':[-33.0292,27.8546],'bloemfontein':[-29.0852,26.1596],'bloem':[-29.0852,26.1596],
  'polokwane':[-23.9045,29.4689],'nelspruit':[-25.4753,30.9694],'mbombela':[-25.4753,30.9694],
  'witbank':[-25.8823,29.2369],'emalahleni':[-25.8823,29.2369],'middelburg':[-25.7719,29.4702],
  'rustenburg':[-25.6753,27.2423],'klerksdorp':[-26.8672,26.6745],'potchefstroom':[-26.7148,27.0999],
  'kimberley':[-28.7382,24.7693],'upington':[-28.4478,21.2561],
  'vereeniging':[-26.6727,27.9258],'vanderbijlpark':[-26.7019,27.8318],'sasolburg':[-26.8148,27.8192],
  'mafikeng':[-25.8484,25.6447],'mahikeng':[-25.8484,25.6447],'tzaneen':[-23.8325,30.1653],
};

function getCoords(location) {
  if (!location) return null;
  const loc = location.toLowerCase().trim();
  if (SA_CITIES[loc]) return SA_CITIES[loc];
  for (const [city, coords] of Object.entries(SA_CITIES)) {
    if (loc.includes(city) || city.includes(loc)) return coords;
  }
  return null;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371, toR = x => x * Math.PI / 180;
  const dLat = toR(lat2-lat1), dLon = toR(lon2-lon1);
  const a = Math.sin(dLat/2)**2 + Math.cos(toR(lat1))*Math.cos(toR(lat2))*Math.sin(dLon/2)**2;
  return Math.round(R * 2 * Math.asin(Math.sqrt(a)));
}

function getDistKm(locationStr) {
  if (!userLocation) return null;
  const coords = getCoords(locationStr);
  if (!coords) return null;
  return haversineKm(userLocation.lat, userLocation.lng, coords[0], coords[1]);
}

function saveUserLocation(lat, lng, label) {
  userLocation = { lat, lng, label };
  localStorage.setItem('dealradar_userloc', JSON.stringify(userLocation));
  updateLocBtn();
  renderTable();
}

function clearUserLocation() {
  userLocation = null;
  localStorage.removeItem('dealradar_userloc');
  updateLocBtn();
  renderTable();
}

function updateLocBtn() {
  const btn = document.getElementById('loc-btn');
  if (!btn) return;
  btn.textContent = userLocation ? `📍 ${userLocation.label}` : '📍 Set my location';
  btn.style.borderColor = userLocation ? 'var(--gold)' : 'var(--border)';
  btn.style.color = userLocation ? 'var(--gold)' : 'var(--muted)';
}

function openLocModal() {
  let overlay = document.getElementById('loc-modal');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'loc-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(8,11,16,0.85);display:flex;align-items:center;justify-content:center;z-index:300';
    overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--surface);border:1px solid var(--border);padding:28px;min-width:340px;max-width:420px;width:90%';
    box.innerHTML = `
      <div style="font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600;margin-bottom:16px">SET MY LOCATION</div>
      <button id="loc-gps-btn" style="width:100%;background:rgba(212,168,67,0.1);border:1px solid var(--gold);color:var(--gold);padding:10px;font-family:'IBM Plex Mono',monospace;font-size:12px;cursor:pointer;margin-bottom:16px">
        USE GPS (auto-detect)
      </button>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px;text-align:center">— or type a city —</div>
      <input id="loc-city-input" list="loc-city-list" placeholder="e.g. Sandton, Cape Town, Durban…"
        style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:9px 12px;font-family:inherit;font-size:13px;outline:none;margin-bottom:4px" />
      <datalist id="loc-city-list">${Object.keys(SA_CITIES).map(c=>c.replace(/\b./,m=>m.toUpperCase())).map(c=>`<option value="${c}">`).join('')}</datalist>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button id="loc-city-confirm" style="flex:1;background:var(--gold);color:#080b10;border:none;padding:9px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700;cursor:pointer">CONFIRM CITY</button>
        <button onclick="clearUserLocation();document.getElementById('loc-modal').remove()" style="background:none;border:1px solid var(--border);color:var(--muted);padding:9px 14px;font-family:inherit;font-size:12px;cursor:pointer">Clear</button>
        <button onclick="document.getElementById('loc-modal').remove()" style="background:none;border:1px solid var(--border);color:var(--muted);padding:9px 14px;font-family:inherit;font-size:12px;cursor:pointer">✕</button>
      </div>
      <div id="loc-status" style="font-size:11px;color:var(--muted);margin-top:10px;min-height:16px"></div>
    `;
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    document.getElementById('loc-gps-btn').onclick = () => {
      const status = document.getElementById('loc-status');
      status.textContent = 'Detecting…';
      navigator.geolocation.getCurrentPosition(pos => {
        const { latitude: lat, longitude: lng } = pos.coords;
        // Reverse-lookup label from nearest SA city
        let nearest = null, nearestDist = Infinity;
        for (const [city, coords] of Object.entries(SA_CITIES)) {
          const d = haversineKm(lat, lng, coords[0], coords[1]);
          if (d < nearestDist) { nearestDist = d; nearest = city; }
        }
        const label = nearest ? nearest.replace(/\b./,m=>m.toUpperCase()) + ' (GPS)' : 'GPS';
        saveUserLocation(lat, lng, label);
        status.style.color = 'var(--green)';
        status.textContent = `Set to ${label} (${nearestDist} km from ${nearest})`;
        setTimeout(() => overlay.remove(), 1200);
      }, err => {
        status.style.color = 'var(--red)';
        status.textContent = 'GPS failed: ' + (err.message || 'permission denied');
      });
    };

    document.getElementById('loc-city-confirm').onclick = () => {
      const val = document.getElementById('loc-city-input').value.trim();
      const coords = getCoords(val);
      const status = document.getElementById('loc-status');
      if (!coords) {
        status.style.color = 'var(--red)';
        status.textContent = 'City not found. Try a major SA city name.';
        return;
      }
      const label = val.replace(/\b./,m=>m.toUpperCase());
      saveUserLocation(coords[0], coords[1], label);
      status.style.color = 'var(--green)';
      status.textContent = `Set to ${label}`;
      setTimeout(() => overlay.remove(), 900);
    };

    document.getElementById('loc-city-input').onkeydown = e => {
      if (e.key === 'Enter') document.getElementById('loc-city-confirm').click();
    };
  } else {
    overlay.remove();
  }
}

function applyChartFilter({ year, location, priceMin, priceMax } = {}) {
  if (year !== undefined) {
    filterYear = year;
    document.querySelectorAll('.filter-btn').forEach(b => {
      if (['all','2022','2023','2024'].includes(b.textContent)) b.classList.remove('active');
      if (b.textContent === year) b.classList.add('active');
    });
  }
  if (location !== undefined) {
    filterLocation = location;
    document.getElementById('filter-location').value = location;
  }
  if (priceMin !== undefined) { filterPriceMin = priceMin; filterPriceMax = priceMax; }

  // Show active filter badge
  const parts = [];
  if (filterYear !== 'all') parts.push('Year: ' + filterYear);
  if (filterLocation !== 'all') parts.push('Location: ' + filterLocation);
  if (filterPriceMin) parts.push(`Price: R${(filterPriceMin/1000).toFixed(0)}k–R${(filterPriceMax/1000).toFixed(0)}k`);
  const badge = document.getElementById('chart-filter-badge');
  if (badge) {
    badge.textContent = parts.length ? '⚡ Filtered by chart: ' + parts.join(' · ') + ' — click to clear' : '';
    badge.style.display = parts.length ? 'block' : 'none';
  }

  // Switch to listings tab
  const listingsBtn = [...document.querySelectorAll('.tab-btn')].find(b => b.textContent.trim() === 'Listings');
  if (listingsBtn) setTab('listings', listingsBtn);
  renderTable();
}

function clearChartFilter() {
  filterYear = 'all'; filterLocation = 'all'; filterPriceMin = 0; filterPriceMax = 0;
  document.querySelectorAll('.filter-btn').forEach(b => {
    if (b.textContent === 'All') b.classList.add('active');
    else if (['2022','2023','2024'].includes(b.textContent)) b.classList.remove('active');
  });
  document.getElementById('filter-location').value = 'all';
  const badge = document.getElementById('chart-filter-badge');
  if (badge) badge.style.display = 'none';
  renderTable();
}

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
  if (d.max_price) document.getElementById('max-price').value = d.max_price;
  if (d.telegram_token) document.getElementById('tele-token').value = d.telegram_token;
  if (d.telegram_chat_id) document.getElementById('tele-chat').value = d.telegram_chat_id;
  if (d.admin_password) document.getElementById('admin-pass').value = d.admin_password;
}
async function saveSettings() {
  const price_alert = document.getElementById('alert-price').value;
  const wbc_url = document.getElementById('wbc-url').value.trim();
  const max_price = document.getElementById('max-price').value;
  const telegram_token = document.getElementById('tele-token').value.trim();
  const telegram_chat_id = document.getElementById('tele-chat').value.trim();
  const admin_password = document.getElementById('admin-pass').value.trim();
  
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({price_alert, wbc_url, max_price, telegram_token, telegram_chat_id, admin_password})
  });
  alert('Settings saved!');
}
async function testTelegram() {
  const btn = document.getElementById('test-tele-btn');
  const oldText = btn.textContent;
  const telegram_token = document.getElementById('tele-token').value.trim();
  const telegram_chat_id = document.getElementById('tele-chat').value.trim();

  if (!telegram_token || !telegram_chat_id) {
    alert('Please enter both Token and Chat ID to test.');
    return;
  }

  btn.textContent = 'Sending...';
  btn.disabled = true;
  
  try {
    const res = await fetch('/api/test-telegram', { 
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({telegram_token, telegram_chat_id})
    });
    const d = await res.json();
    if (d.ok) alert('Test message sent! Check your Telegram.');
    else alert('Error: ' + d.error);
  } catch (e) {
    alert('Failed to send test. Check console for details.');
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
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
  document.getElementById('tab-price-changes').style.display = name === 'price-changes' ? 'block' : 'none';
  document.getElementById('tab-runs').style.display = name === 'runs' ? 'block' : 'none';
  document.getElementById('tab-dealers').style.display = name === 'dealers' ? 'block' : 'none';
  document.getElementById('tab-finance').style.display = name === 'finance' ? 'block' : 'none';
  if (name === 'charts') setTimeout(renderCharts, 0);
  if (name === 'price-changes') loadPriceChanges();
  if (name === 'runs') loadRuns();
  if (name === 'dealers') renderDealers();
  if (name === 'finance') {
    calcAffordability();
    renderBankComparison();
    loadPreApprovals();
  }
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


function renderTable() {
  let activeData = allListings.filter(l => l.is_active === 1);
  const prices = activeData.map(l => l.price).filter(Boolean);
  const avg = prices.length ? prices.reduce((a,b)=>a+b,0)/prices.length : 0;

  let data = allListings;
  if (filterStatus === 'active') data = data.filter(l => l.is_active === 1);
  if (filterStatus === 'gone') data = data.filter(l => l.is_active === 0);
  if (filterStatus === 'watchlisted') data = data.filter(l => l.watchlisted);
  if (filterYear !== 'all') data = data.filter(l => l.year === filterYear);
  if (filterLocation !== 'all') data = data.filter(l => l.location === filterLocation);
  if (filterPriceMin) data = data.filter(l => l.price >= filterPriceMin && l.price <= filterPriceMax);

  // Compute deal score before sorting
  data.forEach(l => {
    l._dealScore = getDealScore(l, avg);
    l._priceDrop = (l.prev_price && l.price < l.prev_price) ? (l.prev_price - l.price) : 0;
    l._distKm = getDistKm(l.location);
    
    // ENHANCED TRAIT DETECTION
    if (!l._detectedVariant) {
        const title = (l.title || "").toUpperCase();
        const TRAITS = ["EXECUTIVE", "LUXURY", "DISTINCT", "AWD", "2WD", "CVT", "DCT", "PRO"];
        let detected = [];
        TRAITS.forEach(v => {
            if (title.includes(v)) detected.push(v.charAt(0) + v.slice(1).toLowerCase());
        });
        l._detectedVariant = detected.join(" ") || l.variant || "Standard";
    }
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
    else if (sortKey === 'distance') { valA = a._distKm ?? 9e9; valB = b._distKm ?? 9e9; }
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

  // Show/hide distance column
  const thDist = document.getElementById('th-distance');
  if (thDist) thDist.style.display = userLocation ? '' : 'none';

  const tbody = document.getElementById('listings-tbody');
  if (!data.length) {
    tbody.innerHTML = `<tr><td colspan="16"><div class="empty-state">
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
    if (!l.is_active) {
        const soldLabel = document.createElement('div');
        soldLabel.style.cssText = 'font-size:10px;color:var(--red);margin-top:2px;font-weight:bold';
        soldLabel.textContent = 'LIKELY SOLD';
        divListingText.appendChild(soldLabel);
    }
    tdListing.appendChild(divListingText);
    tr.appendChild(tdListing);

    // 2b. Variant
    const tdMod = document.createElement('td');
    tdMod.style.fontFamily = 'monospace';
    tdMod.textContent = l._detectedVariant || '—';
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
      const deltaPct = l.prev_price ? ((deltaVal / l.prev_price) * 100).toFixed(1) : null;
      divDelta.className = `price-delta ${isDown ? 'down' : 'up'}`;
      divDelta.textContent = `${isDown ? '▼' : '▲'} ${fmt(deltaVal)}${deltaPct ? ' (' + deltaPct + '%)' : ''}`;
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

    // 9b. Distance (only shown when user location is set)
    const tdDist = document.createElement('td');
    tdDist.style.cssText = 'font-family:monospace;font-size:12px;display:' + (userLocation ? '' : 'none');
    if (userLocation && l._distKm !== null) {
      tdDist.textContent = l._distKm + ' km';
      tdDist.style.color = l._distKm < 20 ? 'var(--green)' : l._distKm < 60 ? 'var(--gold)' : 'var(--text)';
    } else {
      tdDist.textContent = userLocation ? '—' : '';
    }
    tr.appendChild(tdDist);

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
  // Fetch All Intelligence and Market data in parallel
  const [marketRes, analyticsRes, soldData, seasonalData, variantData] = await Promise.all([
    fetch('/api/market').then(r => r.json()),
    fetch('/api/analytics').then(r => r.json()),
    fetch('/api/intelligence/sold').then(r => r.json()),
    fetch('/api/intelligence/seasonal').then(r => r.json()),
    fetch('/api/intelligence/variants').then(r => r.json()),
  ]);

  renderSoldEstimates(soldData);
  renderSeasonalCharts(seasonalData);
  renderVariantAnalysis(variantData);

  const market = marketRes.chart;
  const vel = marketRes.velocity_30d;
  const forecast = analyticsRes.forecast || [];
  const dowData = analyticsRes.dow || [];
  const slope = analyticsRes.slope;
  const listings = allListings;

  // Update Velocity Badge
  const velBadge = document.getElementById('velocity-badge');
  if (vel !== undefined && vel !== 0 && velBadge) {
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
  if (slope !== null && slope !== undefined && forecastBadge) {
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
    options: {
      ...chartDefaults,
      onClick: (evt, els) => {
        if (!els.length) return;
        const i = els[0].index;
        applyChartFilter({ priceMin: Math.round(min + i * bucketSize), priceMax: Math.round(min + (i + 1) * bucketSize) });
      },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
    }
  });

  // Year breakdown
  const yearCounts = {};
  listings.forEach(l => { if (l.year) yearCounts[l.year] = (yearCounts[l.year]||0) + 1; });
  if (charts.year) charts.year.destroy();
  const yearKeys = Object.keys(yearCounts);
  charts.year = new Chart(document.getElementById('chart-year'), {
    type: 'doughnut',
    data: {
      labels: yearKeys,
      datasets: [{ data: Object.values(yearCounts), backgroundColor: ['#d4a843','#3fb950','#58a6ff'], borderColor: '#0d1117', borderWidth: 2 }]
    },
    options: {
      plugins: { legend: { display: true, labels: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 11 } } } },
      onClick: (evt, els) => {
        if (!els.length) return;
        applyChartFilter({ year: yearKeys[els[0].index] });
      },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
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
    if (hmBadge) {
      hmBadge.style.display = 'inline-block';
      hmBadge.textContent = 'Cheapest: ' + locStats[0].name + ' · R' + cheapestAvg.toLocaleString();
    }

    const hmColors = locStats.map(d =>
      d.avg === cheapestAvg ? 'rgba(63,185,80,0.75)' : 'rgba(212,168,67,0.45)'
    );
    const hmBorders = locStats.map(d =>
      d.avg === cheapestAvg ? '#3fb950' : '#d4a843'
    );
    const hmCanvas = document.getElementById('chart-heatmap');
    if (hmCanvas) {
        if (charts.heatmap) charts.heatmap.destroy();
        charts.heatmap = new Chart(hmCanvas, {
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
            },
            onClick: (evt, els) => {
              if (!els.length) return;
              applyChartFilter({ location: locStats[els[0].index].name });
            },
            onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
          }
        });
    }
  }

  // ── Price Change Charts ──────────────────────────────────────────────────
  const pcRes = await fetch('/api/price-changes');
  if (pcRes.ok) {
    const pcData = await pcRes.json();
    if (pcData.length) {
      // Activity chart: drops/rises per day
      const dayMap = {};
      pcData.forEach(c => {
        const d = c.scraped_at.slice(0, 10);
        if (!dayMap[d]) dayMap[d] = { drops: 0, rises: 0 };
        if (c.new_price < c.old_price) dayMap[d].drops++;
        else dayMap[d].rises++;
      });
      const days = Object.keys(dayMap).sort();
      const totalDrops = pcData.filter(c => c.new_price < c.old_price).length;
      const totalRises = pcData.filter(c => c.new_price > c.old_price).length;
      
      const pcaBadge = document.getElementById('pc-activity-badge');
      if (pcaBadge) pcaBadge.textContent = `${totalDrops} drops · ${totalRises} rises · ${pcData.length} total`;

      const pcaCanvas = document.getElementById('chart-pc-activity');
      if (pcaCanvas) {
        if (charts.pcActivity) charts.pcActivity.destroy();
        charts.pcActivity = new Chart(pcaCanvas, {
          type: 'bar',
          data: {
            labels: days,
            datasets: [
              { label: 'Drops', data: days.map(d => dayMap[d].drops), backgroundColor: 'rgba(74,222,128,0.7)', borderColor: '#4ade80', borderWidth: 1 },
              { label: 'Rises', data: days.map(d => dayMap[d].rises), backgroundColor: 'rgba(248,113,113,0.7)', borderColor: '#f87171', borderWidth: 1 },
            ]
          },
          options: {
            ...chartDefaults,
            scales: {
              x: { ...chartDefaults.scales.x, stacked: true },
              y: { ...chartDefaults.scales.y, stacked: true, ticks: { ...chartDefaults.scales.y.ticks, stepSize: 1 } },
            },
            onClick: (evt, els) => {
              if (!els.length) return;
              const day = days[els[0].index];
              const pcEl = document.getElementById('pc-list');
              if (pcEl) {
                  const allRows = pcEl.querySelectorAll('[data-date]');
                  allRows.forEach(r => { r.style.display = r.dataset.date === day ? '' : 'none'; });
                  const badge = document.getElementById('pc-summary');
                  if (badge) badge.innerHTML = `Showing ${day} <span style="color:var(--gold);cursor:pointer" onclick="loadPriceChanges()"> · clear ✕</span>`;
              }
            },
            onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
          }
        });
      }

      // Top drops
      const drops = pcData
        .filter(c => c.new_price < c.old_price)
        .map(c => ({ ...c, delta: c.old_price - c.new_price }))
        .sort((a, b) => b.delta - a.delta)
        .slice(0, 15);

      const pctCanvas = document.getElementById('chart-pc-top');
      if (pctCanvas && drops.length) {
        const labels = drops.map(c => {
          const name = [c.year, c.variant || c.title].filter(Boolean).join(' ');
          return name.length > 35 ? name.slice(0, 33) + '…' : name;
        });
        if (charts.pcTop) charts.pcTop.destroy();
        charts.pcTop = new Chart(pctCanvas, {
          type: 'bar',
          data: {
            labels,
            datasets: [{
              data: drops.map(c => c.delta),
              backgroundColor: 'rgba(74,222,128,0.7)',
              borderColor: '#4ade80',
              borderWidth: 1,
            }]
          },
          options: {
            ...chartDefaults,
            indexAxis: 'y',
            plugins: {
              ...chartDefaults.plugins,
              tooltip: {
                callbacks: {
                  label: ctx => {
                    const c = drops[ctx.dataIndex];
                    const pct = ((c.delta / c.old_price) * 100).toFixed(1);
                    return ` -R${c.delta.toLocaleString()} (${pct}%)  ·  R${c.old_price.toLocaleString()} → R${c.new_price.toLocaleString()}`;
                  }
                }
              }
            },
            scales: {
              x: { ...chartDefaults.scales.x, ticks: { ...chartDefaults.scales.x.ticks, callback: v => 'R' + (v/1000).toFixed(0) + 'k' } },
              y: { ...chartDefaults.scales.y, ticks: { ...chartDefaults.scales.y.ticks, font: { family: 'IBM Plex Mono', size: 10 } } },
            },
            onClick: (evt, els) => {
              if (!els.length) return;
              const c = drops[els[0].index];
              showHistory(c.listing_id, [c.year, c.title, c.variant].filter(Boolean).join(' '));
            },
            onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
          }
        });
      }
    }
  }
}

function renderSoldEstimates(data) {
  const tbody = document.getElementById('sold-tbody');
  if (!data || !data.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:30px; color:var(--dim)">No market exit events detected yet.</td></tr>';
    return;
  }

  tbody.innerHTML = data.map(l => `
    <tr>
      <td>
        <div style="font-weight:600">${l.title}</div>
        <div style="font-size:10px; color:var(--muted)">${l.source} · ${l.dealer}</div>
      </td>
      <td>${l.first_seen.split('T')[0]}</td>
      <td>${l.last_seen.split('T')[0]}</td>
      <td>${l.dom} days</td>
      <td>${fmt(l.last_price)}</td>
      <td>${l.drop_count}</td>
      <td style="color:var(--gold); font-weight:bold">${fmt(l.estimated_sold_price)}</td>
      <td><button class="action-btn" onclick="showHistory(${l.id}, '${l.title}')">History</button></td>
    </tr>
  `).join('');
}

function renderSeasonalCharts(data) {
  const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  
  // Day of Week
  if (data.dow && data.dow.length) {
    const minPrice = Math.min(...data.dow.map(d => d.avg_price));
    const bestRow = data.dow.find(d => d.avg_price === minPrice);
    const badge = document.getElementById('best-dow-badge');
    if (bestRow) {
      badge.textContent = `Shop on ${DAY_NAMES[bestRow.dow]} (Avg R${Math.round(minPrice/1000)}k)`;
    }

    if (charts.dowNew) charts.dowNew.destroy();
    charts.dowNew = new Chart(document.getElementById('chart-dow-new'), {
      type: 'bar',
      data: {
        labels: data.dow.map(d => DAY_NAMES[d.dow]),
        datasets: [{
          data: data.dow.map(d => d.avg_price),
          backgroundColor: data.dow.map(d => d.avg_price === minPrice ? 'rgba(63,185,80,0.6)' : 'rgba(212,168,67,0.3)'),
          borderColor: data.dow.map(d => d.avg_price === minPrice ? '#3fb950' : '#d4a843'),
          borderWidth: 1
        }]
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: false, ticks: { callback: v => 'R' + (v/1000).toFixed(0) + 'k' } } }
      }
    });
  }

  // Week of Month
  if (data.wom && data.wom.length) {
    const minPrice = Math.min(...data.wom.map(d => d.avg_price));
    const bestRow = data.wom.find(d => d.avg_price === minPrice);
    const badge = document.getElementById('best-wom-badge');
    if (bestRow) {
      badge.textContent = `Buying in Week ${bestRow.wom} saves ~R${Math.round((data.wom[0].avg_price - minPrice)/1000)}k`;
    }

    if (charts.wom) charts.wom.destroy();
    charts.wom = new Chart(document.getElementById('chart-wom'), {
      type: 'line',
      data: {
        labels: data.wom.map(d => 'Week ' + d.wom),
        datasets: [{
          data: data.wom.map(d => d.avg_price),
          borderColor: '#d4a843',
          backgroundColor: 'rgba(212,168,67,0.1)',
          fill: true,
          tension: 0.4
        }]
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: false, ticks: { callback: v => 'R' + (v/1000).toFixed(0) + 'k' } } }
      }
    });
  }
}

function renderVariantAnalysis(data) {
  if (!data || !data.length) return;

  if (charts.variantAnalysis) charts.variantAnalysis.destroy();
  charts.variantAnalysis = new Chart(document.getElementById('chart-variant-analysis'), {
    type: 'bar',
    data: {
      labels: data.map(d => d.variant),
      datasets: [{
        label: 'Avg Price',
        data: data.map(d => d.avg_price),
        backgroundColor: 'rgba(212,168,67,0.5)',
        borderColor: '#d4a843',
        borderWidth: 1
      }, {
        label: 'Low Entry',
        data: data.map(d => d.min_price),
        backgroundColor: 'rgba(63,185,80,0.3)',
        borderColor: '#3fb950',
        borderWidth: 1
      }]
    },
    options: {
      plugins: { 
        legend: { display: true, labels: { color: '#7d8590', font: { family: 'IBM Plex Mono', size: 10 } } },
        tooltip: {
            callbacks: {
                label: ctx => ` ${ctx.dataset.label}: R${ctx.raw.toLocaleString()} (${data[ctx.dataIndex].count} listings)`
            }
        }
      },
      scales: {
        y: { ticks: { callback: v => 'R' + (v/1000).toFixed(0) + 'k' } }
      }
    }
  });
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

  const activePrices = allListings.filter(l => l.is_active && l.price).map(l => l.price);
  const marketAvg = activePrices.length ? activePrices.reduce((a,b)=>a+b,0)/activePrices.length : 0;
  cars.forEach(c => { c._cmpScore = getDealScore(c, marketAvg); });

  const content = document.getElementById('cmp-content');
  content.innerHTML = '';

  const table = document.createElement('table');
  table.className = 'cmp-table';

  // ── thead: one <th> per car ──────────────────────────────────────────────
  const thead = document.createElement('thead');
  const headTr = document.createElement('tr');
  // empty corner cell
  const corner = document.createElement('th');
  corner.className = 'row-label';
  headTr.appendChild(corner);

  cars.forEach(c => {
    const th = document.createElement('th');
    if (c.image) {
      const img = document.createElement('img');
      img.src = c.image;
      img.style.cssText = 'width:100%;height:80px;object-fit:cover;border-radius:3px;border:1px solid var(--border);display:block;margin-bottom:8px';
      img.onerror = function() { this.style.display='none'; };
      th.appendChild(img);
    }
    const titleEl = document.createElement('a');
    titleEl.href = c.url || '#';
    titleEl.target = '_blank';
    titleEl.style.cssText = 'font-size:12px;font-weight:600;color:var(--text);text-decoration:none;display:block;margin-bottom:4px';
    titleEl.textContent = (c.title || 'Unknown') + ' ↗';
    th.appendChild(titleEl);
    const src = document.createElement('div');
    src.style.cssText = 'font-size:10px;color:var(--muted);font-family:monospace';
    src.textContent = c.source || 'AutoTrader';
    th.appendChild(src);
    headTr.appendChild(th);
  });
  thead.appendChild(headTr);
  table.appendChild(thead);

  // ── tbody: one <tr> per metric ───────────────────────────────────────────
  const tbody = document.createElement('tbody');

  function addRow(label, values, highlight) {
    // values: array of { _num, text } or plain strings
    const nums = values.map(v => (v && typeof v === 'object') ? v._num : null);
    const validNums = nums.filter(n => n !== null && !isNaN(n));
    let bestNum = null, worstNum = null;
    if (validNums.length > 1 && highlight) {
      bestNum  = highlight === 'low' ? Math.min(...validNums) : Math.max(...validNums);
      worstNum = highlight === 'low' ? Math.max(...validNums) : Math.min(...validNums);
    }

    const tr = document.createElement('tr');
    const labelTd = document.createElement('td');
    labelTd.className = 'row-label';
    labelTd.textContent = label;
    tr.appendChild(labelTd);

    values.forEach(v => {
      const td = document.createElement('td');
      const num  = (v && typeof v === 'object') ? v._num  : null;
      const text = (v && typeof v === 'object') ? v.text  : (v || '—');
      if (num !== null && bestNum  !== null && num === bestNum)  td.classList.add('best');
      if (num !== null && worstNum !== null && num === worstNum) td.classList.add('worst');
      td.textContent = text || '—';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }

  addRow('ASKING PRICE',  cars.map(c => ({ _num: c.price, text: fmt(c.price) })), 'low');
  addRow('YEAR',          cars.map(c => ({ _num: parseInt(c.year), text: c.year || '—' })), 'high');
  addRow('MILEAGE',       cars.map(c => ({ _num: c.mileage, text: c.mileage ? Number(c.mileage).toLocaleString() + ' km' : '—' })), 'low');
  addRow('VARIANT',       cars.map(c => c.variant || '—'), null);
  addRow('LOCATION',      cars.map(c => c.location || '—'), null);
  if (userLocation) addRow('DISTANCE', cars.map(c => {
    const d = getDistKm(c.location);
    return d !== null ? { _num: d, text: d + ' km' } : '—';
  }), 'low');
  addRow('DEALER',        cars.map(c => c.dealer || '—'), null);
  addRow('DEAL SCORE',    cars.map(c => ({ _num: c._cmpScore, text: c._cmpScore ? String(c._cmpScore) : '—' })), 'high');
  addRow('PRICE DROP',    cars.map(c => {
    if (!c.prev_price || c.price >= c.prev_price) return { _num: 0, text: 'None' };
    return { _num: c.prev_price - c.price, text: '▼ ' + fmt(c.prev_price - c.price) };
  }), 'high');
  addRow('DAYS LISTED',   cars.map(c => {
    if (!c.first_seen) return '—';
    const end = (c.is_active || !c.last_seen) ? new Date() : new Date(c.last_seen);
    const d = Math.floor((end - new Date(c.first_seen)) / 86400000);
    return { _num: d, text: d + 'd' };
  }), null);
  addRow('EST. MONTHLY',  cars.map(c => {
    if (!c.price) return '—';
    const P = Math.max(0, c.price - 50000);
    const r = 0.125 / 12;
    const n = 60;
    const m = P * r * Math.pow(1+r,n) / (Math.pow(1+r,n) - 1);
    return { _num: Math.round(m), text: fmt(Math.round(m)) + '/mo' };
  }), 'low');
  addRow('VS MARKET AVG', cars.map(c => {
    if (!c.price || !marketAvg) return '—';
    const diff = ((c.price - marketAvg) / marketAvg * 100).toFixed(1);
    return { _num: parseFloat(diff), text: (parseFloat(diff) > 0 ? '▲ +' : '▼ ') + diff + '%' };
  }), 'low');

  table.appendChild(tbody);
  content.appendChild(table);

  const note = document.createElement('div');
  note.style.cssText = 'font-size:11px;color:var(--dim);margin-top:12px';
  note.textContent = 'Green = best value  ·  Red = worst value  ·  Est. Monthly assumes R50k deposit, 60 months at 12.5%';
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

  // Counter Offers
  currentListingId = id; // Store for the save function
  loadCounterOffers(id);
}

let currentListingId = null;

async function downloadReport() {
    const btn = document.getElementById('report-btn');
    const oldHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span>⏳</span> Generating...';

    const url = `/api/listings/${currentListingId}/report`;
    try {
        const response = await fetch(url);
        if (!response.ok) throw new Error('Report generation failed');
        const blob = await response.blob();
        const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = `DealRadar_Report_${currentListingId}.pdf`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch (e) {
        alert('Could not generate PDF. Make sure the server is healthy.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = oldHtml;
    }
}

async function loadCounterOffers(listingId) {
    const res = await fetch(`/api/listings/${listingId}/counter-offers`);
    const offers = await res.json();
    const listEl = document.getElementById('counter-offer-list');
    listEl.innerHTML = '';
    
    if (offers.length === 0) {
        listEl.innerHTML = '<div style="font-size:11px;color:var(--dim);text-align:center;padding:10px">No negotiation history yet.</div>';
        return;
    }

    offers.forEach(o => {
        const statusClass = 'status-' + o.status.replace(' ', '-');
        const row = document.createElement('div');
        row.className = 'offer-row';
        row.innerHTML = `
          <div class="offer-header">
            <div style="font-size:11px;color:var(--muted)">${o.date}</div>
            <div class="offer-status ${statusClass}">${o.status}</div>
          </div>
          <div style="display:flex; gap:20px; margin-bottom:6px">
            <div>
              <div style="font-size:9px;color:var(--muted)">MY OFFER</div>
              <div style="font-size:14px;font-weight:700;color:var(--green)">${fmt(o.my_offer)}</div>
            </div>
            <div>
              <div style="font-size:9px;color:var(--muted)">DEALER</div>
              <div style="font-size:14px;font-weight:700;color:var(--red)">${fmt(o.dealer_counter)}</div>
            </div>
            <div style="flex:1; text-align:right">
               <button class="action-btn" onclick="deleteCounterOffer(${o.id})" style="color:var(--dim); border:none">✕</button>
            </div>
          </div>
          ${o.notes ? `<div style="font-size:11px;color:var(--text);font-style:italic">"${o.notes}"</div>` : ''}
        `;
        listEl.appendChild(row);
    });
}

async function saveCounterOffer() {
    const myOffer = document.getElementById('co-my-offer').value;
    const dealerCounter = document.getElementById('co-dealer-counter').value;
    const status = document.getElementById('co-status').value;
    const notes = document.getElementById('co-notes').value.trim();

    if (!myOffer) { alert('Please enter your offer amount.'); return; }

    const data = {
        date: new Date().toISOString().split('T')[0],
        my_offer: parseFloat(myOffer),
        dealer_counter: dealerCounter ? parseFloat(dealerCounter) : null,
        status: status,
        notes: notes
    };

    const res = await fetch(`/api/listings/${currentListingId}/counter-offers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });

    if (res.ok) {
        // Clear form
        document.getElementById('co-my-offer').value = '';
        document.getElementById('co-dealer-counter').value = '';
        document.getElementById('co-notes').value = '';
        loadCounterOffers(currentListingId);
    }
}

async function deleteCounterOffer(id) {
    if (!confirm('Delete this negotiation log entry?')) return;
    const res = await fetch(`/api/counter-offers/${id}`, { method: 'DELETE' });
    if (res.ok) loadCounterOffers(currentListingId);
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

// ─── PRICE CHANGES ─────────────────────────────────────────────────────────
let _pcAllChanges = [];

async function loadPriceChanges(filterDate) {
  const el = document.getElementById('pc-list');
  const sumEl = document.getElementById('pc-summary');
  const dailyEl = document.getElementById('pc-daily');
  const badgeEl = document.getElementById('pc-filter-badge');
  el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:20px 0">Loading…</div>';

  if (!_pcAllChanges.length || !filterDate) {
    _pcAllChanges = await fetch('/api/price-changes').then(r => r.json());
  }

  const changes = _pcAllChanges;

  if (!changes.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:20px 0">No price changes recorded yet.</div>';
    sumEl.textContent = '';
    dailyEl.innerHTML = '';
    return;
  }

  // Build daily stats
  const byDay = {};
  changes.forEach(c => {
    const date = c.scraped_at.slice(0, 10);
    if (!byDay[date]) byDay[date] = { drops: 0, rises: 0, dropAmt: 0, riseAmt: 0, total: 0 };
    const delta = c.new_price - c.old_price;
    byDay[date].total++;
    if (delta < 0) { byDay[date].drops++; byDay[date].dropAmt += Math.abs(delta); }
    else { byDay[date].rises++; byDay[date].riseAmt += delta; }
  });

  // Render daily summary cards
  dailyEl.innerHTML = '';
  Object.keys(byDay).sort((a,b) => b.localeCompare(a)).forEach(date => {
    const d = byDay[date];
    const isActive = filterDate === date;
    const label = new Date(date + 'T12:00:00').toLocaleDateString('en-ZA', { weekday:'short', day:'numeric', month:'short' });
    const card = document.createElement('div');
    card.style.cssText = `background:${isActive ? 'var(--gold)' : 'var(--bg)'};border:1px solid ${isActive ? 'var(--gold)' : 'var(--border)'};padding:12px;cursor:pointer;border-radius:2px`;
    card.innerHTML = `
      <div style="font-size:10px;letter-spacing:1px;font-weight:700;color:${isActive ? '#000' : 'var(--muted)'};margin-bottom:6px">${label}</div>
      <div style="font-size:18px;font-weight:700;color:${isActive ? '#000' : 'var(--text)'};margin-bottom:6px">${d.total} change${d.total !== 1 ? 's' : ''}</div>
      <div style="display:flex;gap:8px;font-size:11px">
        ${d.drops ? `<span style="color:${isActive ? '#000' : 'var(--green)'}">▼ ${d.drops} drop${d.drops !== 1 ? 's' : ''} (R${d.dropAmt.toLocaleString()})</span>` : ''}
        ${d.rises ? `<span style="color:${isActive ? '#000' : 'var(--red)'}">▲ ${d.rises} rise${d.rises !== 1 ? 's' : ''}</span>` : ''}
      </div>`;
    card.onclick = () => loadPriceChanges(isActive ? null : date);
    dailyEl.appendChild(card);
  });

  // Filter changes for list
  const filtered = filterDate ? changes.filter(c => c.scraped_at.slice(0, 10) === filterDate) : changes;
  const drops = filtered.filter(c => c.new_price < c.old_price).length;
  const rises = filtered.filter(c => c.new_price > c.old_price).length;
  sumEl.textContent = `${filtered.length} changes · ${drops} drops · ${rises} rises`;
  badgeEl.textContent = filterDate ? `Filtered: ${new Date(filterDate + 'T12:00:00').toLocaleDateString('en-ZA', {day:'numeric',month:'short',year:'numeric'})} · click card to clear` : '';

  // Group by date for visual separation
  let lastDate = null;
  const rows = [];

  filtered.forEach(c => {
    const date = c.scraped_at.slice(0, 10);
    if (date !== lastDate) {
      lastDate = date;
      const d = document.createElement('div');
      d.style.cssText = 'font-size:10px;letter-spacing:2px;color:var(--muted);font-weight:600;padding:14px 0 6px 0;border-bottom:1px solid var(--border)';
      d.textContent = new Date(date + 'T12:00:00').toLocaleDateString('en-ZA', { weekday:'long', year:'numeric', month:'long', day:'numeric' });
      rows.push(d);
    }

    const delta = c.new_price - c.old_price;
    const pct = ((delta / c.old_price) * 100).toFixed(1);
    const isDrop = delta < 0;
    const colour = isDrop ? 'var(--green)' : 'var(--red)';
    const arrow = isDrop ? '▼' : '▲';
    const sign = isDrop ? '' : '+';

    const row = document.createElement('div');
    row.dataset.date = date;
    row.style.cssText = 'display:flex;align-items:center;gap:16px;padding:10px 0;border-bottom:1px solid var(--border)';

    const time = document.createElement('div');
    time.style.cssText = 'font-family:monospace;font-size:11px;color:var(--muted);white-space:nowrap;width:48px;flex-shrink:0';
    time.textContent = c.scraped_at.slice(11, 16);

    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0';
    const titleEl = document.createElement('a');
    titleEl.href = c.url;
    titleEl.target = '_blank';
    titleEl.style.cssText = 'color:var(--text);text-decoration:none;font-weight:600;font-size:13px;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    titleEl.textContent = [c.year, c.title, c.variant].filter(Boolean).join(' ');
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:11px;color:var(--muted);margin-top:2px';
    meta.textContent = c.source || '';
    info.appendChild(titleEl);
    info.appendChild(meta);

    const prices = document.createElement('div');
    prices.style.cssText = 'text-align:center;white-space:nowrap;flex-shrink:0;width:150px';
    const oldP = document.createElement('div');
    oldP.style.cssText = 'font-size:11px;color:var(--muted);text-decoration:line-through';
    oldP.textContent = 'R' + Number(c.old_price).toLocaleString();
    const newP = document.createElement('div');
    newP.style.cssText = 'font-size:14px;font-weight:700;color:var(--text)';
    newP.textContent = 'R' + Number(c.new_price).toLocaleString();
    prices.appendChild(oldP);
    prices.appendChild(newP);

    const badge = document.createElement('div');
    badge.style.cssText = `font-size:13px;font-weight:700;color:${colour};white-space:nowrap;flex-shrink:0;width:140px;text-align:center`;
    badge.textContent = `${arrow} ${sign}R${Math.abs(delta).toLocaleString()} (${sign}${pct}%)`;

    row.appendChild(time);
    row.appendChild(info);
    row.appendChild(prices);
    row.appendChild(badge);
    rows.push(row);
  });

  el.innerHTML = '';
  rows.forEach(r => el.appendChild(r));
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

// ─── FINANCE ──────────────────────────────────────────────────────────────
let preApprovals = [];
let editingPreId = null;

function calcAffordability() {
  const net = parseFloat(document.getElementById('calc-net').value) || 0;
  const deposit = parseFloat(document.getElementById('calc-deposit').value) || 0;
  const term = parseInt(document.getElementById('calc-term').value) || 60;
  const rate = 12.5; // Average market rate for affordability calculation

  const maxInstalment = net * 0.20; // 20% rule
  
  // Solve for Principal: P = (Instalment * (1 - (1 + r)^-n)) / r
  const r = (rate / 100) / 12;
  const n = term;
  
  const maxLoan = (maxInstalment * (1 - Math.pow(1 + r, -n))) / r;
  const maxPrice = maxLoan + deposit;
  const totalRepay = (maxInstalment * n) + deposit;

  document.getElementById('res-max-price').textContent = fmt(Math.round(maxPrice));
  document.getElementById('res-max-instalment').textContent = fmt(Math.round(maxInstalment));
  document.getElementById('res-total-repay').textContent = fmt(Math.round(totalRepay));

  // Also update comparison inputs for convenience
  document.getElementById('comp-price').value = Math.round(maxPrice);
  document.getElementById('comp-deposit').value = Math.round(deposit);
  document.getElementById('comp-term').value = term;
  renderBankComparison();
}

function renderBankComparison() {
  const price = parseFloat(document.getElementById('comp-price').value) || 0;
  const deposit = parseFloat(document.getElementById('comp-deposit').value) || 0;
  const term = parseInt(document.getElementById('comp-term').value) || 60;
  const loan = price - deposit;

  const banks = [
    { name: 'WesBank', rate: 11.75 },
    { name: 'Absa', rate: 12.25 },
    { name: 'Standard Bank', rate: 12.5 },
    { name: 'Nedbank', rate: 12.75 },
    { name: 'Capitec', rate: 13.5 }
  ];

  const tbody = document.getElementById('bank-comp-tbody');
  tbody.innerHTML = '';

  banks.forEach(b => {
    const r = (b.rate / 100) / 12;
    const n = term;
    const inst = loan > 0 ? (loan * r) / (1 - Math.pow(1 + r, -n)) : 0;
    const totalRepay = (inst * n) + deposit;
    const totalInterest = totalRepay - price;

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-weight:600">${b.name}</td>
      <td>${b.rate}%</td>
      <td style="color:var(--gold)">${fmt(Math.round(inst))}</td>
      <td>${fmt(Math.round(totalRepay))}</td>
      <td style="color:var(--muted)">${fmt(Math.round(totalInterest))}</td>
    `;
    tbody.appendChild(tr);
  });
}

function openPreApprovalModal(id = null) {
  editingPreId = id;
  const modal = document.getElementById('pre-approval-modal');
  modal.style.display = 'flex';
  
  if (id) {
    const app = preApprovals.find(a => a.id === id);
    document.getElementById('pre-title').textContent = 'Edit Pre-approval';
    document.getElementById('pre-bank').value = app.bank_name;
    document.getElementById('pre-date').value = app.date_applied;
    document.getElementById('pre-status').value = app.status;
    document.getElementById('pre-amount').value = app.amount;
    document.getElementById('pre-rate').value = app.interest_rate;
    document.getElementById('pre-instalment').value = app.monthly_instalment;
    document.getElementById('pre-notes').value = app.notes || '';
  } else {
    document.getElementById('pre-title').textContent = 'Add Pre-approval';
    document.getElementById('pre-bank').value = '';
    document.getElementById('pre-date').value = new Date().toISOString().split('T')[0];
    document.getElementById('pre-status').value = 'Pending';
    document.getElementById('pre-amount').value = document.getElementById('comp-price').value;
    document.getElementById('pre-rate').value = 11.75;
    document.getElementById('pre-instalment').value = '';
    document.getElementById('pre-notes').value = '';
  }
}

function closePreApprovalModal() {
  document.getElementById('pre-approval-modal').style.display = 'none';
}

async function savePreApproval() {
  const data = {
    bank_name: document.getElementById('pre-bank').value,
    date_applied: document.getElementById('pre-date').value,
    status: document.getElementById('pre-status').value,
    amount: parseFloat(document.getElementById('pre-amount').value),
    interest_rate: parseFloat(document.getElementById('pre-rate').value),
    monthly_instalment: parseFloat(document.getElementById('pre-instalment').value),
    notes: document.getElementById('pre-notes').value
  };

  const url = editingPreId ? `/api/finance/pre-approvals/${editingPreId}` : '/api/finance/pre-approvals';
  const method = editingPreId ? 'PUT' : 'POST';

  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });

  if (res.ok) {
    closePreApprovalModal();
    loadPreApprovals();
  } else {
    alert('Failed to save application.');
  }
}

async function loadPreApprovals() {
  const res = await fetch('/api/finance/pre-approvals');
  preApprovals = await res.json();
  const tbody = document.getElementById('pre-approval-tbody');
  tbody.innerHTML = '';

  if (preApprovals.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:40px; color:var(--dim)">No applications logged yet.</td></tr>';
    return;
  }

  preApprovals.forEach(a => {
    const statusClass = a.status.toLowerCase();
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-weight:600">${a.bank_name}</td>
      <td style="font-size:11px; color:var(--muted)">${a.date_applied}</td>
      <td>${fmt(a.amount)}</td>
      <td>${a.interest_rate}%</td>
      <td style="color:var(--gold)">${fmt(a.monthly_instalment)}</td>
      <td><span class="status-badge ${statusClass}">${a.status}</span></td>
      <td style="font-size:11px; color:var(--muted); max-width:150px; overflow:hidden; text-overflow:ellipsis">${a.notes || ''}</td>
      <td style="text-align:right">
        <button class="action-btn" onclick="openPreApprovalModal(${a.id})">Edit</button>
        <button class="action-btn" onclick="deletePreApproval(${a.id})" style="color:var(--red); border-color:transparent">✕</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function deletePreApproval(id) {
  if (!confirm('Delete this application record?')) return;
  const res = await fetch(`/api/finance/pre-approvals/${id}`, { method: 'DELETE' });
  if (res.ok) loadPreApprovals();
}

// ─── INIT ──────────────────────────────────────────────────────────────────
loadListings();
updateLocBtn();
</script>
</body>
</html>
"""


def telegram_worker():
    """Background thread to handle Telegram commands."""
    offset = None
    print("Telegram Worker: Started polling...")
    while True:
        try:
            token = get_setting("telegram_token")
            chat_id_saved = get_setting("telegram_chat_id")
            
            if not token or not chat_id_saved:
                time.sleep(10)
                continue
                
            updates = get_telegram_updates(offset)
            for upd in updates:
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message", {})
                chat_id_incoming = msg.get("chat", {}).get("id")
                text = msg.get("text", "").lower().strip()
                
                # Verify chat ID matches owner
                if str(chat_id_incoming) != str(chat_id_saved):
                    continue
                    
                if text == "/scrape":
                    send_telegram_msg("Scrape started! 🚀")
                    with _scrape_lock:
                        if not _scrape_status["running"]:
                            _scrape_status["running"] = True
                            _scrape_status["log"] = ["Starting scrape via Telegram..."]
                            threading.Thread(target=_do_scrape, daemon=True).start()
                        else:
                            send_telegram_msg("A scrape is already in progress.")
                elif text == "/status":
                    count = len(get_listings_with_latest_price())
                    send_telegram_msg(f"<b>DealRadar Status</b>\nTracking {count} items.\nSend /scrape to trigger a manual scan.")

        except Exception as e:
            print(f"Telegram Worker Error: {e}")
        time.sleep(5)


if __name__ == "__main__":
    print("\n  DealRadar Price Tracker")
    print("  Open → http://localhost:5001\n")
    
    # Start Telegram worker
    threading.Thread(target=telegram_worker, daemon=True).start()
    
    app.run(debug=False, port=5001, host="127.0.0.1")
