import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mediavpn.app import create_app


def test_managed_login_roles_revocation_and_one_time_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIAVPN_HUB_URL", "https://hub.example")
    monkeypatch.setenv("MEDIAVPN_HUB_BROWSER_URL", "https://public-hub.example")
    role = "administrator"
    allowed = True
    online = True
    exchanges = []

    def hub(request):
        assert request.url.host == "hub.example"
        if not online:
            raise httpx.ConnectError("Synthetic outage")
        if request.url.path.endswith("origin"):
            return httpx.Response(200, json={"data": {"origin": "https://vpn.example"}})
        if request.url.path.endswith("exchange"):
            exchanges.append(request.content)
            return httpx.Response(200, json={"data": {"token": "t" * 64, "expires": int(time.time()) + 600, "user": {"id": "member", "username": "test", "role": role}}})
        assert request.url.params["packageId"] == "org.mediahub.mediavpn"
        assert request.headers["Authorization"] == "Bearer " + "t" * 64
        return httpx.Response(200, json={"data": {"id": "member", "username": "test", "role": role}}) if allowed else httpx.Response(403)

    with TestClient(create_app(tmp_path, "https://vpn.example", hub_transport=httpx.MockTransport(hub)), base_url="https://vpn.example") as client:
        status = client.get("/api/auth/status").json()
        assert status["managed"] and not status["needsSetup"]
        assert status["loginUrl"] == "https://vpn.example/auth/hub/start"
        body = {"username": "local", "password": "synthetic-password"}
        assert client.post("/api/auth/setup", json=body).status_code == 403
        assert client.post("/api/auth/login", json=body).status_code == 403
        start = client.get("/auth/hub/start", follow_redirects=False)
        params = parse_qs(urlsplit(start.headers["location"]).query)
        assert urlsplit(start.headers["location"]).netloc == "public-hub.example"
        assert params["packageId"] == ["org.mediahub.mediavpn"]
        state = params["state"][0]
        query = {"state": state, "code": "c" * 64}
        assert client.get("/auth/hub/callback", params={**query, "state": "x" * 64}).status_code == 403
        assert client.get("/auth/hub/callback", params=query, follow_redirects=False).status_code == 303
        client.cookies.set("mediavpn_sso_state", state, domain="vpn.example", path="/auth/hub")
        assert client.get("/auth/hub/callback", params=query, follow_redirects=False).status_code == 401
        assert len(exchanges) == 1
        identity = client.get("/api/auth/me").json()
        assert identity["id"] == "member" and identity["role"] == "administrator"
        assert client.post("/api/profiles/" + "a" * 24 + "/start").status_code == 403
        client.headers["X-MediaVPN-CSRF"] = identity["csrf"]
        assert client.get("/api/profiles").status_code == 200
        for role in ["user", "viewer"]:
            assert client.get("/api/profiles").status_code == 200
            assert client.post("/api/profiles/" + "a" * 24 + "/start").status_code == 403
            assert client.put("/api/seedbox", json={"enabled": False}).status_code == 403
        allowed = False
        assert client.get("/api/profiles").status_code == 403
        allowed = True
        online = False
        assert client.get("/api/profiles").status_code == 503
        online = True
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("hub,app", [("http://hub.example", "https://vpn.example"), ("https://hub.example", "http://vpn.example")])
def test_managed_login_requires_https(tmp_path, monkeypatch, hub, app):
    monkeypatch.setenv("MEDIAVPN_HUB_URL", hub)
    with pytest.raises(ValueError):
        create_app(tmp_path, app)
