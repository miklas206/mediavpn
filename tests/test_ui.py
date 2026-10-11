import base64
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("MEDIAVPN_UI_URL"), reason="Set MEDIAVPN_UI_URL for isolated local browser checks")


def test_preview_upload_and_responsive_views():
    from playwright.sync_api import sync_playwright

    target = Path("test-results")
    target.mkdir(exist_ok=True)
    key = base64.b64encode(bytes(range(32))).decode()
    configuration = f"[Interface]\nPrivateKey={key}\nAddress=10.2.0.2/32\n[Peer]\nPublicKey={key}\nAllowedIPs=0.0.0.0/0\nEndpoint=1.1.1.1:51820\n"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.environ["MEDIAVPN_UI_URL"])
        page.wait_for_load_state("networkidle")
        for width in [1440, 390, 320]:
            page.set_viewport_size({"width": width, "height": 900})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(target / f"mediavpn-login-{width}.png"))
        page.get_by_role("textbox", name="Brugernavn").fill("synthetic")
        page.get_by_role("textbox", name="Adgangskode").fill("synthetic-test-password")
        page.locator("#login-submit").click()
        page.get_by_role("heading", name="Forbindelser", exact=True).wait_for()
        page.get_by_role("button", name="Ny forbindelse", exact=True).click()
        page.get_by_role("textbox", name="Navn", exact=True).fill("Synthetic VPN")
        page.locator("#configuration").set_input_files({"name": "synthetic.conf", "mimeType": "text/plain", "buffer": configuration.encode()})
        page.get_by_role("button", name="Gem forbindelse", exact=True).click()
        page.get_by_role("heading", name="Synthetic VPN", exact=True).wait_for()
        assert page.get_by_role("button", name="Start", exact=True).is_disabled()
        assert key not in page.locator("body").inner_text()
        for width in [1440, 390, 320]:
            page.set_viewport_size({"width": width, "height": 900})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.screenshot(path=str(target / f"mediavpn-profiles-{width}.png"))
        page.on("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Fjern", exact=True).click()
        page.get_by_text("Ingen forbindelser endnu.", exact=True).wait_for()
        assert errors == []
        browser.close()