"""
AutoTrader scraper using Playwright (headless Chromium).
Targets: Chery Tiggo 8 Pro, 2022-2024, sorted by price ascending.
"""

import re
import time
import random
from playwright.sync_api import sync_playwright

BASE_URL = (
    "https://www.autotrader.co.za/cars-for-sale/chery/tiggo-8-pro"
    "?sortorder=PriceLow&year=2022-to-2024"
)


def parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_mileage(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def scrape(max_pages: int = 5, headless: bool = True, status_callback=None) -> list[dict]:
    listings = []

    def log(msg):
        if status_callback:
            status_callback(msg)
        print(msg)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-ZA",
        )
        page = context.new_page()

        # Stealth: remove webdriver fingerprint
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for page_num in range(1, max_pages + 1):
            url = BASE_URL if page_num == 1 else f"{BASE_URL}&pagenumber={page_num}"
            log(f"Scraping page {page_num}: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for listing cards to appear
                page.wait_for_selector(
                    "[data-testid='results-tile'], .e-listing-tile, [class*='listing'], [class*='tile']",
                    timeout=15000,
                )
            except Exception as e:
                log(f"Page {page_num} load error: {e}")
                # Try to grab whatever rendered
                pass

            time.sleep(random.uniform(1.5, 3.0))

            # Scroll to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            time.sleep(0.8)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)

            # Extract listings via multiple selector strategies
            page_listings = page.evaluate("""
                () => {
                    const results = [];
                    
                    // Strategy 1: data-testid tiles
                    const tiles1 = document.querySelectorAll('[data-testid="results-tile"]');
                    // Strategy 2: class-based tiles
                    const tiles2 = document.querySelectorAll('.e-listing-tile, [class*="ListingTile"], [class*="listing-tile"]');
                    // Strategy 3: article tags (common pattern)
                    const tiles3 = document.querySelectorAll('article[class*="listing"], article[class*="tile"]');
                    // Strategy 4: generic link cards with prices
                    const tiles4 = document.querySelectorAll('a[href*="/car-for-sale/"][href*="chery"]');

                    const tiles = tiles1.length > 0 ? tiles1 :
                                  tiles2.length > 0 ? tiles2 :
                                  tiles3.length > 0 ? tiles3 : tiles4;

                    tiles.forEach(tile => {
                        try {
                            // Price
                            const priceEl = tile.querySelector('[data-testid="price"], [class*="price"], [class*="Price"]');
                            const price = priceEl ? priceEl.innerText.trim() : '';

                            // Title / make+model+variant
                            let title = '';
                            const titleEls = tile.querySelectorAll('span, h1, h2, h3, h4, div, [class*="title"]');
                            for (let el of titleEls) {
                                let txt = el.innerText ? el.innerText.trim() : '';
                                if (/chery|tiggo/i.test(txt) && txt.length > 5 && txt.length < 60) {
                                    title = txt.split('\\n')[0];
                                    break;
                                }
                            }
                            if (!title) title = 'Chery Tiggo 8 Pro';

                            // Year
                            const yearMatch = (tile.innerText || '').match(/\\b(202[2-4])\\b/);
                            const year = yearMatch ? yearMatch[1] : '';

                            // Mileage
                            const mileageEl = tile.querySelector('[data-testid="mileage"], [class*="mileage"], [class*="Mileage"], [class*="km"]');
                            const mileageText = mileageEl ? mileageEl.innerText : (tile.innerText.match(/\\d[\\d\\s]*km/i) || [''])[0];

                            // Location
                            const locEl = tile.querySelector('[data-testid="location"], [class*="location"], [class*="Location"]');
                            const location = locEl ? locEl.innerText.trim() : '';

                            // Dealer
                            const dealerEl = tile.querySelector('span[class*="e-name"]');
                            const dealer = dealerEl ? dealerEl.innerText.trim() : '';

                            // Link
                            const linkEl = tile.tagName === 'A' ? tile : tile.querySelector('a[href*="/car-for-sale/"]');
                            const href = linkEl ? linkEl.getAttribute('href') : '';
                            const url = href.startsWith('http') ? href : 'https://www.autotrader.co.za' + href;

                            // Image
                            const imgEl = tile.querySelector('img');
                            const image = imgEl ? (imgEl.src || imgEl.dataset.src || '') : '';

                            if (price || title) {
                                results.push({ price, title, year, mileage: mileageText, location, dealer, url, image });
                            }
                        } catch(e) {}
                    });

                    return results;
                }
            """)

            log(f"  Found {len(page_listings)} listings on page {page_num}")

            if not page_listings:
                log(f"  No listings found — stopping pagination")
                break

            for item in page_listings:
                parsed_price = parse_price(item.get("price", ""))
                parsed_mileage = parse_mileage(item.get("mileage", ""))

                # Skip obviously wrong prices (< R100k or > R2M)
                if parsed_price and (parsed_price < 100_000 or parsed_price > 2_000_000):
                    continue

                listing = {
                    "title": item.get("title", "").replace("\n", " ").strip(),
                    "price_raw": item.get("price", "").strip(),
                    "price": parsed_price,
                    "year": item.get("year", "").strip(),
                    "mileage": parsed_mileage,
                    "mileage_raw": item.get("mileage", "").strip(),
                    "location": item.get("location", "").replace("\n", " ").strip(),
                    "dealer": item.get("dealer", "").replace("\n", " ").strip(),
                    "url": item.get("url", "").strip(),
                    "image": item.get("image", "").strip(),
                }

                # Deduplicate by URL
                if listing["url"] and not any(l["url"] == listing["url"] for l in listings):
                    listings.append(listing)

            # Check if there's a next page
            has_next = page.evaluate("""
                () => {
                    const next = document.querySelector('[aria-label="Next"], [data-testid="next-page"], a[rel="next"], link[rel="next"], .pagination__next:not(.disabled)');
                    return !!next;
                }
            """)
            if not has_next:
                log(f"  No next page button — done")
                break

            time.sleep(random.uniform(2, 4))

        browser.close()

    log(f"Total listings scraped: {len(listings)}")
    return listings

def scrape_single_url(url: str, headless: bool = True, status_callback=None) -> list[dict]:
    def log(msg):
        if status_callback:
            status_callback(msg)
        else:
            print(msg)

    log(f"Scraping single URL: {url}")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for some main element to load
            page.wait_for_selector('h1, [data-testid="price"], [class*="price"]', timeout=15000)

            item = page.evaluate("""
                () => {
                    const getElText = (selector) => {
                        const el = document.querySelector(selector);
                        return el ? el.innerText.trim() : '';
                    };

                    const price_raw = getElText('[data-testid="price"], .e-price, [class*="price"], .price');
                    const title = getElText('h1, h2, [class*="title"]');
                    
                    const yearMatch = document.body.innerText.match(/\\b(202[2-4])\\b/);
                    const year = yearMatch ? yearMatch[1] : '';

                    let mileage_raw = getElText('[data-testid="mileage"], [class*="mileage"], [class*="km"]');
                    if (!mileage_raw || !mileage_raw.match(/\\d/)) {
                        const m = document.body.innerText.match(/\\d[\\d\\s]*km/i);
                        if (m) mileage_raw = m[0];
                    }

                    const loc = getElText('[data-testid="location"], [class*="location"]');
                    const location = loc.replace('Km from you?', '').trim();

                    const dealer = getElText('[data-testid="dealer-name"], span[class*="e-name"]');

                    let imgUrl = '';
                    const imgEl = document.querySelector('img.e-gallery-image__-Otz2bA3mQj') || 
                                  document.querySelector('.gallery img') || 
                                  document.querySelector('meta[property="og:image"]');
                    if (imgEl) {
                        imgUrl = imgEl.content || imgEl.src;
                    }

                    return { price_raw, title, year, mileage_raw, location, dealer, image: imgUrl, url: window.location.href };
                }
            """)
            
            if item and (item.get('price_raw') or item.get('title')):
                parsed_price = parse_price(item.get('price_raw', ''))
                parsed_mileage = parse_mileage(item.get('mileage_raw', ''))
                
                listing = {
                    "title": item.get("title", "").replace("\n", " ").strip(),
                    "price_raw": item.get("price_raw", "").strip(),
                    "price": parsed_price,
                    "year": item.get("year", "").strip(),
                    "mileage": parsed_mileage,
                    "mileage_raw": item.get("mileage_raw", "").strip(),
                    "location": item.get("location", "").replace("\n", " ").strip(),
                    "dealer": item.get("dealer", "").replace("\n", " ").strip(),
                    "url": item.get("url", "").strip(),
                    "image": item.get("image", "").strip(),
                }
                results.append(listing)

        except Exception as e:
            log(f"Error scraping single URL: {str(e)}")
        finally:
            browser.close()

    return results

if __name__ == "__main__":
    results = scrape(max_pages=3, headless=False)
    for r in results:
        print(r)
