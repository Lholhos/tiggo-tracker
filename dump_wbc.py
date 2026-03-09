from playwright.sync_api import sync_playwright
import time

def main():
    url = "https://www.webuycars.co.za/buy-a-car?q=%22Chery%20Tiggo%208%20PRO%22&Year=[2022,2024]&Year_Gte=%222022%22&Year_Lte=%222024%22"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print("Navigating to WeBuyCars...")
        page.goto(url, timeout=60000)
        
        # Wait for listings to load. We might need to guess a selector or just wait network idle
        page.wait_for_load_state("networkidle")
        time.sleep(5)  # give it extra time to render client side
        
        html = page.content()
        with open("wbc_dump.html", "w", encoding="utf-8") as f:
            f.write(html)
            
        print(f"Dumped {len(html)} bytes to wbc_dump.html")
        browser.close()

if __name__ == "__main__":
    main()
