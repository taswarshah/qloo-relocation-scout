"""Headless browser check against a LOCAL server only (http://127.0.0.1:8077).
Loads the page, runs a persona (live Qloo via the local server), checks for console/CSP errors,
exercises the comparison view and saves desktop + mobile screenshots to docs/screenshots/.
Run: python tests/ui/browser_check.py  (needs `pip install playwright` and a Chrome/Chromium)."""
import os, sys
from playwright.sync_api import sync_playwright

BASE = os.getenv("UI_BASE", "http://127.0.0.1:8077")
assert BASE.startswith(("http://127.0.0.1", "http://localhost")), "UI check only runs against localhost"
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "screenshots")
CHROME = os.getenv("CHROME_PATH", "/usr/bin/google-chrome")
problems = []

def watch(page):
    page.on("console", lambda m: m.type in ("error", "warning") and problems.append(f"console {m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url}"))
    page.on("response", lambda r: r.status >= 400 and problems.append(f"HTTP {r.status}: {r.url}"))

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True)
    # Desktop
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    watch(page)
    page.goto(BASE + "/")
    page.screenshot(path=f"{OUT}/desktop-home.png", full_page=False)
    page.click('[data-preset="0"]')
    page.fill("#compare", "Dubai")
    page.click("#submit")
    page.wait_for_selector(".city-card", timeout=30000)
    cards = page.locator(".city-card").count()
    page.locator(".compare-toggle input").nth(0).check()
    page.locator(".compare-toggle input").nth(cards - 1).check()  # top pick vs "your pick"
    page.wait_for_selector(".compare-table", timeout=5000)
    rows = page.locator(".compare-table tbody tr").count()
    page.screenshot(path=f"{OUT}/desktop-results.png", full_page=True)
    page.locator(".comparison-section").screenshot(path=f"{OUT}/desktop-compare.png")
    share_url = page.url
    # Mobile (iPhone-ish), via shareable link (auto-runs)
    m = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    watch(m)
    m.goto(share_url)
    m.wait_for_selector(".city-card", timeout=30000)
    m.locator(".city-card").first.scroll_into_view_if_needed()
    m.screenshot(path=f"{OUT}/mobile-results.png", full_page=False)
    m.screenshot(path=f"{OUT}/mobile-results-full.png", full_page=True)
    m.goto(BASE + "/")
    m.screenshot(path=f"{OUT}/mobile-home.png", full_page=False)
    browser.close()

print(f"cards={cards} compare_rows={rows} share_url_has_params={'?' in share_url}")
print("problems:", problems or "none")
sys.exit(1 if problems else 0)
