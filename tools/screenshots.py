"""
Retake the README screenshots against the demo instance `tools/demo_fleet.py`
seeds. Needs `playwright` and a Chromium: `pip install playwright` then
`playwright install chromium`.

    python tools/screenshots.py           # expects the demo on :8899
"""

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("PM_DEMO_URL", "http://127.0.0.1:8899")
USER, PASSWORD = "jdoe", "demo-password-for-shots"
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs"
WIDTH, SCALE = 1300, 2


def clip_below(page, selector, pad=26):
    """Full width, trimmed to just past the bottom of `selector`."""
    box = page.locator(selector).last.bounding_box()
    return {"x": 0, "y": 0, "width": WIDTH,
            "height": round(box["y"] + box["height"] + pad)}


def clip_around(page, first, last, pad=36):
    """A window spanning two elements, so a short page is not mostly blank."""
    a = page.locator(first).bounding_box()
    b = page.locator(last).bounding_box()
    top = max(0, a["y"] - pad)
    return {"x": 0, "y": top, "width": WIDTH,
            "height": round(b["y"] + b["height"] + pad - top)}


def sign_in(page):
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("input[name=username]", USER)
    page.fill("input[name=password]", PASSWORD)
    page.click("form.signin-card button[type=submit]")
    page.wait_for_url(BASE + "/", wait_until="networkidle")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": WIDTH, "height": 1000},
            device_scale_factor=SCALE,
        )
        page = ctx.new_page()

        # --- the sign-in page, before there is any session -----------------
        page.goto(BASE + "/login", wait_until="networkidle")
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.screenshot(path=OUT / "login.png", full_page=True,
                        clip=clip_around(page, ".signin-brand", ".meta-note"))

        sign_in(page)

        # --- cards -------------------------------------------------------
        page.wait_for_selector(".card")
        page.screenshot(path=OUT / "dashboard.png", full_page=True,
                        clip=clip_below(page, ".wrap > .meta-note"))

        # --- table, sorted by printer name --------------------------------
        page.click("#view-table")
        page.select_option("#sort-key", "display_name")
        page.wait_for_selector("table tbody tr")
        page.screenshot(path=OUT / "dashboard-table.png", full_page=True,
                        clip=clip_below(page, ".wrap > .meta-note"))

        # --- subnet editor ------------------------------------------------
        page.click("#view-cards")
        page.click("#edit-subnets")
        page.wait_for_selector("#subnet-dialog[open]")
        if "10.10.30.0/24" not in page.inner_text("#subnet-list"):
            page.fill("#subnet-cidr", "10.10.30.0/24")
            page.fill("#subnet-label", "Floor 3 printers")
            page.click("#subnet-add button[type=submit]")
            page.wait_for_timeout(700)
        # Park the second range, so the shot shows what a disabled one looks like.
        second = page.locator('#subnet-list input[type=checkbox]').nth(1)
        if second.is_checked():
            second.click()
            page.wait_for_timeout(700)
        page.screenshot(path=OUT / "subnets.png", full_page=True,
                        clip=clip_below(page, "#subnet-dialog"))
        page.keyboard.press("Escape")

        # --- one printer's detail page ------------------------------------
        page.goto(BASE + "/printer/10.10.20.41", wait_until="networkidle")
        page.screenshot(path=OUT / "printer-detail.png", full_page=True,
                        clip=clip_below(page, ".wrap > .meta-note"))

        browser.close()
    print("screenshots written to", OUT)


if __name__ == "__main__":
    sys.exit(main())
