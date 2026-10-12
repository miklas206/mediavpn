"""Account language and shipped catalog coverage; no live clients or VPNs."""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mediavpn.app import create_app

STATIC = Path(__file__).parents[1] / "mediavpn" / "static"


def catalog():
    source = (STATIC / "i18n.js").read_text(encoding="utf8")
    return json.loads(source.split("const messages = ", 1)[1].split(";\n", 1)[0])


class Labels(HTMLParser):
    def __init__(self):
        super().__init__()
        self.labels = set()
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "code"):
            self.ignored += 1
        for key, value in attrs:
            if key in ("placeholder", "title", "aria-label") and value:
                self.labels.add(value.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style", "code"):
            self.ignored -= 1

    def handle_data(self, text):
        if not self.ignored and re.search("[A-Za-zæøå]", text):
            self.labels.add(text.strip())


def test_all_static_labels_and_translated_calls_have_catalog_entries():
    messages = catalog()
    known = set(messages) | set(messages.values())
    neutral = {"MediaSeed", "MediaSeed 0.1.0", "MediaVPN", "MediaVPN 0.1.0", "MEDIAVPN", "MediaHub apps", "App", "Internet", "VPN", "RSS", "Torrent", "beta", "releases", "Port-forwarding", "Dansk", "Firefox", "Chrome / Edge / Brave / Opera"}
    html = list(STATIC.glob("*.html"))
    if STATIC.parent.name == "mediaseed":
        html.append(STATIC.parents[1] / "browser-extension" / "popup.html")
    for file in html:
        labels = Labels()
        labels.feed(file.read_text(encoding="utf8"))
        assert not {label for label in labels.labels - known - neutral if not label.startswith(("https://", "magnet:", "xcrun "))}, file
    scripts = list(STATIC.glob("*.js"))
    if STATIC.parent.name == "mediaseed":
        scripts += list((STATIC.parents[1] / "browser-extension").glob("*.js"))
    for file in scripts:
        if file.name == "i18n.js":
            continue
        for literal in re.findall(r'\bt\(("(?:[^"\\]|\\.)*")', file.read_text(encoding="utf8")):
            assert json.loads(literal).strip() in known, (file.name, literal)


@pytest.mark.parametrize("language", ["en", "da"])
def test_language_chosen_at_account_creation_survives_login_and_restart(tmp_path, language):
    body = {"username": "Synthetic", "password": "synthetic-test-password", "language": language}
    with TestClient(create_app(tmp_path, "http://testserver")) as client:
        assert client.post("/api/auth/setup", json=body).status_code == 200
    with TestClient(create_app(tmp_path, "http://testserver")) as client:
        assert client.post("/api/auth/login", json=body).status_code == 200
        assert client.get("/api/auth/me").json()["language"] == language
        assert client.post("/api/auth/setup", json={**body, "language": "fr"}).status_code == 422


def test_existing_danish_account_migrates_without_resetting_credentials(tmp_path):
    import sqlite3

    from argon2 import PasswordHasher

    password = "synthetic-existing-password"
    with sqlite3.connect(tmp_path / "mediavpn.sqlite") as database:
        database.execute("CREATE TABLE accounts (username TEXT PRIMARY KEY, password TEXT NOT NULL)")
        database.execute("INSERT INTO accounts VALUES (?,?)", ("Existing", PasswordHasher().hash(password)))
    for _ in range(2):
        with TestClient(create_app(tmp_path, "http://testserver")) as client:
            assert client.get("/api/auth/status").json()["needsSetup"] is False
            assert client.post("/api/auth/login", json={"username": "Existing", "password": password}).status_code == 200
            assert client.get("/api/auth/me").json()["language"] == "da"
