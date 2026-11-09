"""Open the Streamlit app in a headless browser and wake it up if it is sleeping."""
from __future__ import annotations

import os
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

APP_URL = os.getenv("APP_URL", "https://skyu-cycling-training.streamlit.app/")
WAKE_BUTTON_TEXT = "Yes, get this app back up"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(APP_URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(10_000)

        button = page.get_by_role("button", name=WAKE_BUTTON_TEXT)
        if button.count() > 0:
            print("App is sleeping. Waking it up...")
            button.first.click()
            page.wait_for_timeout(5_000)
        else:
            print("Wake button not found. App is probably awake.")

        try:
            page.frame_locator("iframe").locator("text=S級コーチ").first.wait_for(timeout=180_000)
            print("App is up.")
        except PlaywrightTimeoutError:
            try:
                page.locator("text=S級コーチ").first.wait_for(timeout=30_000)
                print("App is up.")
            except PlaywrightTimeoutError:
                print("Could not confirm the app title, but the wake request was sent.")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
