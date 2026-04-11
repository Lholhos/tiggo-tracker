import os
import time
from datetime import datetime
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore
from database import (
    get_listings_with_latest_price, get_price_history, get_market_snapshots,
    get_price_changes, get_recent_runs, get_day_of_week_prices,
    get_week_of_month_prices, get_sold_listings_with_estimates,
)

load_dotenv()

def inject_public_secrets(log):
    """Inject secrets from .env into public/index.html before syncing."""
    log("  Injecting secrets into public/index.html…")
    html_path = "public/index.html"
    if not os.path.exists(html_path):
        log(f"  ⚠ Error: {html_path} not found")
        return

    with open(html_path, "r") as f:
        content = f.read()

    replacements = {
        "CLARITY_ID_PLACEHOLDER": os.environ.get("CLARITY_PROJECT_ID", ""),
        "FIREBASE_API_KEY_PLACEHOLDER": os.environ.get("FIREBASE_API_KEY", ""),
        "FIREBASE_PROJECT_ID_PLACEHOLDER": os.environ.get("FIREBASE_PROJECT_ID", ""),
        "FIREBASE_APP_ID_PLACEHOLDER": os.environ.get("FIREBASE_APP_ID", ""),
    }

    # Verify that all secrets are present
    missing = [k for k, v in replacements.items() if not v]
    if missing:
        log(f"  ⚠ Missing secrets in .env: {', '.join(missing)}")
    
    modified = False
    for placeholder, value in replacements.items():
        if value and placeholder in content:
            content = content.replace(placeholder, value)
            modified = True

    if modified:
        with open(html_path, "w") as f:
            f.write(content)
        log("  Secrets injected successfully.")
    else:
        log("  No placeholders found or secrets already injected.")

# Fields that are explicitly NEVER synced
_PRIVATE_FIELDS = {
    "negotiation_script", "counter_offers", "notes", "preapproval_data",
    "affordability_data", "telegram_token", "telegram_chat_id",
    "price_alert_threshold", "scrape_logs", "opening_offer",
    "target_price", "walk_away_price", "watchlisted", "price_raw",
    "source",
}

# Public listing fields to sync
_PUBLIC_LISTING_FIELDS = {
    "url", "title", "year", "price", "mileage", "mileage_raw",
    "location", "dealer", "is_active", "first_seen", "last_seen", "image",
}

def _score(listing: dict) -> int:
    price = listing.get("price") or 0
    mileage = listing.get("mileage") or 0
    year = int(listing.get("year") or 0)
    score = 50

    if price < 200_000: score += 25
    elif price < 250_000: score += 15
    elif price < 300_000: score += 5
    elif price > 400_000: score -= 10

    if mileage < 30_000: score += 20
    elif mileage < 60_000: score += 10
    elif mileage < 100_000: score += 0
    elif mileage < 150_000: score -= 10
    else: score -= 20

    if year >= 2023: score += 10
    elif year == 2022: score += 5
    elif year <= 2019: score -= 10

    return max(0, min(100, score))

def _init_firebase():
    if firebase_admin._apps:
        return firestore.client()
        
    current_dir = os.path.dirname(os.path.abspath(__file__))
    cred_path = os.path.join(current_dir, "serviceAccountKey.json")
    
    if not os.path.exists(cred_path):
        raise FileNotFoundError(
            "Missing 'serviceAccountKey.json'! "
            "Please download your Firebase Admin Service Account key from "
            "Project Settings -> Service Accounts and place it in this folder."
        )
        
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    return firestore.client()

def sync_to_firestore(status_callback=None):
    log = status_callback or (lambda m: print(m))
    
    log("🚀 Starting Firestore Sync…")
    
    # Pre-sync: Inject secrets into index.html
    inject_public_secrets(log)
    
    try:
        print("  Initializing Firebase…")
        db = _init_firebase()
        print("  Firebase initialized.")
    except Exception as e:
        log(f"  ⚠ Firebase sync disabled: {e}")
        import traceback
        traceback.print_exc()
        return

    # 1. Sync Listings
    listings = get_listings_with_latest_price(include_inactive=True)
    log(f"  Syncing {len(listings)} listings…")
    batch = db.batch()
    batch_count = 0
    
    for listing in listings:
        doc_id = str(listing["id"])
        public = {k: listing[k] for k in _PUBLIC_LISTING_FIELDS if k in listing}
        public["score"] = _score(listing)
        
        for f in _PRIVATE_FIELDS:
            public.pop(f, None)
            
        # Calculate price change
        history = get_price_history(listing["id"])
        if len(history) > 1:
            # history is ordered by scraped_at DESC usually, let's sort
            history.sort(key=lambda x: x["scraped_at"], reverse=True)
            current_price = history[0]["price"]
            # Find the most recent DIFFERENT price
            prev_price = next((h["price"] for h in history[1:] if h["price"] != current_price), None)
            if prev_price:
                public["old_price"] = prev_price
                public["price_diff"] = current_price - prev_price
            
        doc_ref = db.collection("listings").document(doc_id)
        batch.set(doc_ref, public, merge=True)
        batch_count += 1
        
        if batch_count == 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
            
    if batch_count > 0:
        batch.commit()

    # 2. Sync Price History
    log("  Syncing price history…")
    batch = db.batch()
    batch_count = 0
    total_history = 0
    
    for listing in listings:
        history = get_price_history(listing["id"])
        for i, entry in enumerate(history):
            doc_id = f"{listing['id']}_{i}"
            fields = {
                "listing_id": int(listing["id"]),
                "price": entry.get("price"),
                "scraped_at": entry.get("scraped_at"),
                "mileage": entry.get("mileage"),
            }
            doc_ref = db.collection("price_history").document(doc_id)
            batch.set(doc_ref, fields, merge=True)
            batch_count += 1
            total_history += 1
            
            if batch_count == 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0
                
    if batch_count > 0:
        batch.commit()
    log(f"  Synced {total_history} price history records")
    
    # 2b. Sync Price Changes (Combined drops/hikes)
    changes = get_price_changes()
    log(f"  Syncing {len(changes)} price change events…")
    batch = db.batch()
    batch_count = 0
    for i, change in enumerate(changes):
        # Create a unique but stable ID for the change event
        doc_id = f"{change['listing_id']}_{change['scraped_at'].replace(':','').replace('-','')}"
        fields = {
            "listing_id": int(change["listing_id"]),
            "title": change.get("title"),
            "new_price": change.get("new_price"),
            "old_price": change.get("old_price"),
            "scraped_at": change.get("scraped_at"),
            "variant": change.get("variant"),
        }
        doc_ref = db.collection("price_changes").document(doc_id)
        batch.set(doc_ref, fields, merge=True)
        batch_count += 1
        
        if batch_count == 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
            
    if batch_count > 0:
        batch.commit()

    # 3. Market snapshots
    snapshots = get_market_snapshots()
    log(f"  Syncing {len(snapshots)} market snapshots…")
    batch = db.batch()
    batch_count = 0
    
    for snap in snapshots:
        doc_id = snap["date"].replace("-", "")
        fields = {
            "date": snap["date"],
            "avg_price": snap.get("avg_price"),
            "min_price": snap.get("min_price"),
            "max_price": snap.get("max_price"),
            "listing_count": snap.get("listing_count"),
        }
        doc_ref = db.collection("market_snapshots").document(doc_id)
        batch.set(doc_ref, fields, merge=True)
        batch_count += 1
        
        if batch_count == 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
            
    if batch_count > 0:
        batch.commit()
        
    # 4. Scrape runs
    runs = get_recent_runs(20)
    log(f"  Syncing {len(runs)} scrape runs…")
    batch = db.batch()
    batch_count = 0
    for i, run in enumerate(runs):
        doc_id = f"run_{i}"
        fields = {
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "status": run.get("status"),
            "listings_found": run.get("listings_found"),
            "new_listings": run.get("new_listings"),
            "price_changes": run.get("price_changes"),
        }
        doc_ref = db.collection("scrape_runs").document(doc_id)
        batch.set(doc_ref, fields, merge=True)
        batch_count += 1
    if batch_count > 0:
        batch.commit()

    # 5. Day-of-week prices
    dow_data = get_day_of_week_prices()
    log(f"  Syncing {len(dow_data)} day-of-week records…")
    batch = db.batch()
    for d in dow_data:
        doc_ref = db.collection("day_of_week_prices").document(str(d["dow"]))
        batch.set(doc_ref, d, merge=True)
    batch.commit()

    # 6. Week-of-month prices
    wom_data = get_week_of_month_prices()
    log(f"  Syncing {len(wom_data)} week-of-month records…")
    batch = db.batch()
    for d in wom_data:
        doc_ref = db.collection("week_of_month_prices").document(str(d["wom"]))
        batch.set(doc_ref, d, merge=True)
    batch.commit()

    # 7. Sold listings with estimates
    sold = get_sold_listings_with_estimates()
    log(f"  Syncing {len(sold)} sold listing estimates…")
    batch = db.batch()
    batch_count = 0
    for s in sold:
        doc_id = str(s["id"])
        fields = {
            "title": s.get("title"),
            "price": s.get("price"),
            "last_price": s.get("last_price"),
            "estimated_sold_price": s.get("estimated_sold_price"),
            "dom": s.get("dom"),
            "drop_count": s.get("drop_count"),
            "discount_pct": round(min(0.10, (s.get("dom",0) / 1000) + ((s.get("drop_count",0)) * 0.01)) * 100, 1),
            "first_seen": s.get("first_seen"),
            "last_seen": s.get("last_seen"),
            "year": s.get("year"),
            "mileage": s.get("mileage"),
            "location": s.get("location"),
            "dealer": s.get("dealer"),
        }
        doc_ref = db.collection("sold_estimates").document(doc_id)
        batch.set(doc_ref, fields, merge=True)
        batch_count += 1
        if batch_count == 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
    if batch_count > 0:
        batch.commit()

    log("  Sync complete!")

if __name__ == "__main__":
    sync_to_firestore()
