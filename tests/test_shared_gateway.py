import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_app import login
from test_configuration import CONFIG, KEY

from mediavpn.app import create_app
from mediavpn.relay_image.runtime import firewall, validate, wireguard
from mediavpn.runtime import Runtime
from mediavpn.shared_gateway import SharedGateway, new_key


def test_peers_keep_provider_key_private_and_survive_restart(tmp_path):
    gateway = SharedGateway(
        SimpleNamespace(enabled=False), tmp_path, "https://192.168.1.50:18803"
    )
    gateway.configure("synthetic", True)
    _, public = new_key()
    first = gateway.peer("host-app", public, "Jellyfin")
    assert first == gateway.peer("host-app", public, "Jellyfin")
    assert first["ready"] is False
    assert first["endpointIp"] == "192.168.1.50"
    assert "privateKey" not in json.dumps(first)
    second = SharedGateway(
        SimpleNamespace(enabled=False), tmp_path, "https://192.168.1.50:18803"
    )
    assert second.peer("host-app", public, "Jellyfin") == first
    _, replacement = new_key()
    with pytest.raises(HTTPException):
        second.peer("host-app", replacement, "Jellyfin")
    second.remove_peer("host-app")
    assert second.public()["peers"] == []


def test_shared_profile_prevents_second_provider_connection(tmp_path):
    calls = []
    client = SimpleNamespace(
        containers=SimpleNamespace(
            list=lambda **kw: [], run=lambda *a, **kw: calls.append(kw)
        )
    )
    runtime = Runtime(tmp_path, True, "/approved", client)
    runtime.shared_profile = lambda: "one"
    runtime.shared_endpoint = lambda: ("192.168.1.50", 18804)
    with pytest.raises(HTTPException):
        runtime.start("two")
    assert not calls
    runtime.start("one")
    assert len(calls) == 1
    assert calls[0]["ports"] == {"51879/udp": ("192.168.1.50", 18804)}
    assert calls[0]["environment"]["FIREWALL_INPUT_PORTS"] == "51879"


def test_relay_has_no_physical_default_allow_or_ipv6_fallback():
    private, public = new_key()
    value = validate(
        {
            "mode": "client",
            "address": "10.250.87.2/24",
            "listenPort": 51879,
            "privateKey": private,
            "gatewayKey": public,
            "endpointIp": "192.168.1.50",
            "endpointPort": 18804,
        }
    )
    rules = firewall(value)
    assert ["iptables", "-P", "OUTPUT", "DROP"] in rules
    assert ["ip6tables", "-P", "OUTPUT", "DROP"] in rules
    physical = [row for row in rules if "eth0" in row and "ACCEPT" in row]
    assert len(physical) == 1 and "192.168.1.50" in physical[0] and "udp" in physical[0]
    assert "AllowedIPs = 0.0.0.0/0" in wireguard(value)
    assert len(base64.b64decode(private)) == 32
    value["endpointIp"] = "8.8.8.8"
    with pytest.raises(ValueError):
        validate(value)


def test_shared_api_requires_login_csrf_and_never_echoes_keys(tmp_path):
    client = TestClient(create_app(tmp_path, "http://testserver"))
    assert client.get("/api/shared").status_code == 401
    assert client.get("/api/integration/shared").status_code == 401
    assert (
        client.post(
            "/api/integration/shared/peers", json={"privateKey": KEY}
        ).status_code
        == 401
    )
    login(client)
    profile = client.post(
        "/api/profiles", json={"name": "Synthetic", "configuration": CONFIG}
    ).json()["id"]
    client.headers.pop("X-MediaVPN-CSRF")
    assert (
        client.put(
            "/api/shared", json={"enabled": True, "profileId": profile}
        ).status_code
        == 403
    )
    assert KEY not in client.get("/api/shared").text
    assert client.get("/api/shared").json()["ready"] is False


def test_torrent_integration_key_cannot_manage_other_app_relays(tmp_path):
    runtime = SimpleNamespace(enabled=True)
    app = create_app(tmp_path, "http://testserver", runtime=runtime)
    client = TestClient(app)
    torrent_key = (tmp_path / "integrations/mediaseed.token").read_text()
    management_key = (tmp_path / "integrations/mediahub.token").read_text()
    assert torrent_key != management_key
    assert client.get("/api/integration/shared", headers={"X-MediaHub-Key": torrent_key}).status_code == 401
    response = client.get("/api/integration/shared", headers={"X-MediaHub-Key": management_key})
    assert response.status_code == 200
    assert management_key not in response.text and torrent_key not in response.text


@pytest.mark.parametrize("age,expected", [(0, True), (89, True), (120, True), (165, True), (179, True), (180, False), (181, False), (-1, False)])
def test_client_health_accepts_normal_rekey_window_but_not_expired_session(monkeypatch, age, expected):
    from mediavpn.relay_image import runtime as relay
    monkeypatch.setattr(relay, "CONFIG", SimpleNamespace(read_text=lambda: "{}"))
    monkeypatch.setattr(relay, "validate", lambda value: {"mode": "client"})
    monkeypatch.setattr(relay.time, "time", lambda: 1000)
    monkeypatch.setattr(relay, "command", lambda *args: "synthetic-key " + str(1000 - age) if args[0] == "wg" else "")
    assert relay.health() is expected
