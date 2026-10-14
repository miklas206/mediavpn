import hashlib
import json
import sqlite3
import time

import httpx
from fastapi.testclient import TestClient

from mediavpn.app import create_app
from mediavpn.auth import COOKIE


def test_installed_app_selection_proxies_session_and_enforces_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIAVPN_HUB_URL", "https://hub.example")
    calls = []

    def hub(request):
        assert request.headers["Authorization"] == "Bearer " + "t" * 64
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"data": {"id": "admin", "role": "administrator"}})
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"data": {"apps": [{"id": "jellyfin", "enabled": True}]}})

    app = create_app(tmp_path, "https://vpn.example", hub_transport=httpx.MockTransport(hub))
    # Existing authenticated session, as produced by the separately tested SSO exchange.
    with sqlite3.connect(tmp_path / "mediavpn.sqlite") as db:
        digest = hashlib.sha256(b"browser-session").hexdigest()
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest, "csrf-value", time.time() + 600))
        db.execute("INSERT INTO hub_sessions VALUES (?,?,?)", (digest, "t" * 64, "admin"))
    with TestClient(app, base_url="https://vpn.example") as client:
        client.cookies.set(COOKIE, "browser-session")
        assert client.get("/api/hub-apps").status_code == 200
        assert client.put("/api/hub-apps/jellyfin", json={"enabled": True}).status_code == 403
        client.headers["X-MediaVPN-CSRF"] = "csrf-value"
        assert client.put("/api/hub-apps/jellyfin", json={"enabled": True}).status_code == 200
        assert calls[-1] == ("PUT", "/api/v1/sso/vpn/apps/jellyfin", {"enabled": True})
        assert client.put("/api/hub-apps/arbitrary", json={"enabled": True}).status_code == 422
