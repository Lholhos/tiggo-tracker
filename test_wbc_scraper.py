from playwright.sync_api import sync_playwright
import time
from scraper import _parse_wbc

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    def log(msg): print(msg)
    
    results = _parse_wbc(page, log)
    print("Results:", len(results))
    if results:
        print(results[0])
    
    browser.close()
