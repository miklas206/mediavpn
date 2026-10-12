import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_app import login
from test_configuration import CONFIG, KEY

from mediavpn.app import create_app
from mediavpn.proton import ProtonSelector, selected_configuration
from mediavpn.runtime import Runtime

PUBLIC = base64.b64encode(b'x' * 32).decode()


def selector(probe=lambda address: 10):
    rows = [{"vpn": "wireguard", "country": "Denmark", "hostname": "dk-01.protonvpn.net", "wgpubkey": PUBLIC, "port_forward": True, "ips": ["8.8.8.8", "1.1.1.1", "192.168.1.1"]}]
    return ProtonSelector(fetch=lambda: json.dumps({"timestamp": 1000, "servers": rows}).encode(), clock=lambda: 1001, probe=probe)


def test_selection_filters_private_addresses_ranks_latency_and_preserves_key():
    service = selector(lambda address: 5 if address == "1.1.1.1" else 20)
    assert service.countries() == ["Denmark"]
    selected = service.choose("Denmark")
    assert selected["ip"] == "1.1.1.1" and selected["tested"] == 2
    normalized = selected_configuration(CONFIG, selected)
    assert f"PrivateKey = {KEY}" in normalized
    assert f"PublicKey = {PUBLIC}" in normalized
    assert "AllowedIPs = 0.0.0.0/0" in normalized
    with pytest.raises(ValueError):
        service.choose("Atlantis")
    with pytest.raises(ValueError):
        selector(lambda address: None).choose("Denmark")


@pytest.mark.parametrize("payload", [b'{}', b'x' * (2 * 1024 * 1024 + 1), b'{"timestamp":1,"servers":[]}'], ids=["missing", "oversized", "expired"])
def test_catalog_fails_closed(payload):
    with pytest.raises(ValueError):
        ProtonSelector(fetch=lambda: payload).countries()


def test_automatic_runtime_uses_derived_file_and_keeps_running_server(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "synthetic.conf").write_text(CONFIG)
    calls, existing = [], []
    def run(*args, **options):
        calls.append(options)
        existing.append(SimpleNamespace(attrs={"State": {"Running": True}, "Config": {"Env": ["VPN_PORT_FORWARDING=on"]}}, labels=options["labels"], reload=lambda: None))
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: existing, run=run)))
    runtime.selector = selector()
    runtime.start("synthetic", True, "Denmark")
    assert (profiles / "synthetic.conf").read_text() == CONFIG
    assert KEY in (profiles / "synthetic.auto.conf").read_text()
    assert "/opt/mediavpn-data/profiles/synthetic.auto.conf" in calls[0]["volumes"]
    assert KEY not in json.dumps(calls)
    runtime.selector = SimpleNamespace(choose=lambda country: pytest.fail("Running server must remain stable"))
    runtime.start("synthetic", True, "Denmark")
    assert len(calls) == 1


def test_api_opt_in_persistence_csrf_and_original_file(tmp_path):
    service = selector()
    client = TestClient(create_app(tmp_path, "http://testserver", selector=service))
    assert client.get("/api/proton/countries").status_code == 401
    login(client)
    identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "port_forwarding": True}).json()["id"]
    path = tmp_path / "profiles" / f"{identifier}.conf"
    original = path.read_bytes()
    endpoint = f"/api/profiles/{identifier}/server-selection"
    assert client.put(endpoint, json={"country": "Denmark"}).status_code == 200
    assert client.get("/api/profiles").json()[0]["auto_country"] == "Denmark"
    assert path.read_bytes() == original
    assert KEY not in client.get("/api/profiles").text
    assert client.put(endpoint, json={"country": "Atlantis"}).status_code == 422
    assert client.put(endpoint, json={"country": None}, headers={"X-MediaVPN-CSRF": "wrong"}).status_code == 403
    restarted = TestClient(create_app(tmp_path, "http://testserver", selector=service))
    restarted.post("/api/auth/login", json={"username": "synthetic", "password": "synthetic-test-password"})
    assert restarted.get("/api/profiles").json()[0]["auto_country"] == "Denmark"
    assert client.put(endpoint, json={"country": None}).status_code == 200
    assert path.read_bytes() == original


def test_mode_change_stops_attached_client_before_tunnel(tmp_path, monkeypatch):
    from mediavpn.seedbox import Seedbox
    events = []
    monkeypatch.setattr(Seedbox, "stop", lambda self: events.append("client stopped"))
    runtime = SimpleNamespace(enabled=True, stop=lambda identifier: events.append("tunnel stopped"))
    client = TestClient(create_app(tmp_path, "http://testserver", runtime=runtime, selector=selector()))
    login(client)
    identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "port_forwarding": True}).json()["id"]
    (tmp_path / "seedbox.json").write_text(json.dumps({"enabled": True, "profileId": identifier}))
    assert client.put(f"/api/profiles/{identifier}/server-selection", json={"country": "Denmark"}).status_code == 200
    assert events == ["client stopped", "tunnel stopped"]
