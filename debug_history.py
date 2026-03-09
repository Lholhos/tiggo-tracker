import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        errors = []
        page.on("console", lambda msg: errors.append(f"CONSOLE {msg.type}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda err: errors.append(f"PAGE ERROR: {err}"))
        
        try:
            await page.goto("http://localhost:5001", wait_until="networkidle")
            
            # Click the first history button
            hb = page.locator(".history-btn").first
            await hb.wait_for()
            await hb.click()
            await page.wait_for_timeout(1000)
            
            with open("browser_errors.txt", "w") as f:
                if errors:
                    f.write("\n".join(errors))
                else:
                    f.write("No JS errors detected.")
        except Exception as e:
            with open("browser_errors.txt", "w") as f:
                f.write(f"PYTHON ERROR: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
