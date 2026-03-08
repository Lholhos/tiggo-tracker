# Tiggo 8 Pro · Price Tracker

AutoTrader price monitor for **Chery Tiggo 8 Pro 2022–2024**, sorted by price.  
Runs locally. Scrapes on demand via a web UI. Stores all history in SQLite.

---

## Quick Start

```bash
cd tiggo-tracker
chmod +x run.sh
./run.sh
```

Then open **http://localhost:5000** in your browser.

---

## What It Does

- **Scrapes AutoTrader** using headless Chromium (Playwright) — bypasses the 503 bot block
- **Stores every scrape** in a local SQLite database (`tracker.db`)
- **Tracks price changes** per listing over time
- **Web UI** with:
  - Live scrape log
  - Listings table sorted by price, with % above/below average
  - Price delta indicators (up/down from last scrape)
  - Per-listing price history chart (click "History")
  - Market trend chart (avg price over time)
  - Price vs Mileage scatter plot
  - Price distribution histogram
  - Scrape run history

---

## Requirements

- macOS or Linux
- Python 3.10+
- Internet connection (for scraping)

Dependencies installed automatically by `run.sh`:
- `flask` — web server
- `playwright` — headless Chromium

---

## Files

```
tiggo-tracker/
├── app.py          # Flask server + HTML UI
├── scraper.py      # Playwright scraper
├── database.py     # SQLite layer
├── requirements.txt
├── run.sh          # Setup + launch script
└── tracker.db      # Created on first run
```

---

## Scheduling (Optional)

To run a scrape automatically every day at 08:00, add a cron job:

```bash
crontab -e
```

Add:
```
0 8 * * * cd /path/to/tiggo-tracker && python3 -c "from scraper import scrape; from database import upsert_listings, start_run, finish_run; r=start_run(); l=scrape(); finish_run(r, {'total':len(l), **upsert_listings(l)})"
```

---

## Notes

- AutoTrader blocks simple HTTP scrapers with 503. This tool uses real headless Chromium to render pages like a real browser.
- All data is local — nothing is sent anywhere.
- `tracker.db` grows slowly. Each scrape adds ~100 rows max.
