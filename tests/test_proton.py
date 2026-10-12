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


@pytest.fixture(autouse=True)
def no_real_load_requests(monkeypatch):
    monkeypatch.setattr(ProtonSelector, "download_load", staticmethod(lambda: b'{"Code":1000,"LogicalServers":[]}'))


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


def test_global_selection_and_recommendations_cache():
    calls = []
    service = selector(lambda address: calls.append(address) or (5 if address == "1.1.1.1" else 20))
    assert service.choose("*")["ip"] == "1.1.1.1"
    result = service.recommendations()
    assert result["recommendations"] == [{"country": "Denmark", "server": "dk-01.protonvpn.net", "latencyMs": 5.0, "loadPercent": None}]
    assert result["tested"] == 2
    assert result["measuredAt"] == 1001
    count = len(calls)
    assert service.recommendations() == result and len(calls) == count


def test_global_probe_budget_is_bounded_and_countries_are_distributed():
    service = selector()
    service.rows = [{"country": "Country" + chr(65 + index // 26) + chr(65 + index % 26), "hostname": "node.protonvpn.net", "key": PUBLIC, "ip": f"8.8.{index}.{number}"} for index in range(40) for number in (1, 2, 3)]
    service.expires = 2000
    called = []
    service.probe = lambda address: called.append(address) or 5
    result = service.recommendations()
    assert len(called) == 40
    assert len(result["recommendations"]) == 40
    assert result["totalCountries"] == 40


def test_global_selection_never_omits_a_fast_country_by_random_country_sampling():
    service = selector()
    service.rows = [{"country": "Country" + chr(65 + index), "hostname": "node.protonvpn.net", "key": PUBLIC, "ip": f"8.8.{index}.1"} for index in range(25)]
    service.rows.append({"country": "Denmark", "hostname": "dk-01.protonvpn.net", "key": PUBLIC, "ip": "1.1.1.1"})
    service.expires = 2000
    service.sample = lambda rows, count: rows[:count]
    service.probe = lambda ip: 3 if ip == "1.1.1.1" else 20
    result = service.choose("*")
    assert result["country"] == "Denmark"
    assert result["tested"] == 26


def test_catalog_keeps_provider_country_names_with_commas():
    service = selector()
    original = json.loads(service.fetch())
    original["servers"][0]["country"] = "Palestine, State of"
    service.fetch = lambda: json.dumps(original).encode()
    assert service.countries() == ["Palestine, State of"]


def test_failed_selection_never_starts_container_or_changes_original(tmp_path):
    from fastapi import HTTPException
    (tmp_path / "profiles").mkdir()
    original = tmp_path / "profiles" / "synthetic.conf"
    original.write_text(CONFIG)
    runtime = Runtime(tmp_path, True, "/opt/mediavpn-data", SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: [], run=lambda *args, **kwargs: pytest.fail("Failed measurement must not start a VPN"))))
    runtime.selector = selector(lambda address: None)
    with pytest.raises(HTTPException) as error:
        runtime.start("synthetic", True, "*")
    assert error.value.status_code == 503
    assert original.read_text() == CONFIG
    assert not (tmp_path / "profiles" / "synthetic.auto.conf").exists()


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
    assert client.put(endpoint, json={"country": "*"}).status_code == 200
    assert client.get("/api/profiles").json()[0]["auto_country"] == "*"
    assert client.get("/api/proton/recommendations").json()["tested"] == 2
    assert path.read_bytes() == original


def test_mode_change_stops_attached_client_before_tunnel(tmp_path, monkeypatch):
    from mediavpn.seedbox import Seedbox
    events = []
    monkeypatch.setattr(Seedbox, "stop", lambda self: events.append("client stopped"))
    runtime = SimpleNamespace(enabled=True, status=lambda identifier: {"state": "stopped"}, stop=lambda identifier: events.append("tunnel stopped"))
    client = TestClient(create_app(tmp_path, "http://testserver", runtime=runtime, selector=selector()))
    login(client)
    identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "port_forwarding": True}).json()["id"]
    (tmp_path / "seedbox.json").write_text(json.dumps({"enabled": True, "profileId": identifier}))
    assert client.put(f"/api/profiles/{identifier}/server-selection", json={"country": "Denmark"}).status_code == 200
    assert events == ["client stopped", "tunnel stopped"]


@pytest.mark.parametrize("desired,state", [(1, "connected"), (1, "stopped"), (0, "connected"), (0, "stopped")])
def test_server_change_preserves_running_intent_and_attachment(tmp_path, monkeypatch, desired, state):
    import sqlite3
    from mediavpn.seedbox import Seedbox
    events = []
    monkeypatch.setattr(Seedbox, "stop", lambda self: events.append("client stopped"))
    runtime = SimpleNamespace(enabled=True, status=lambda identifier: {"state": state}, stop=lambda identifier: events.append("tunnel stopped"))
    app = create_app(tmp_path, "http://testserver", runtime=runtime, selector=selector())
    client = TestClient(app)
    login(client)
    identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "port_forwarding": True}).json()["id"]
    with sqlite3.connect(tmp_path / "mediavpn.sqlite") as db:
        db.execute("UPDATE profiles SET desired=?", (desired,))
    settings = json.dumps({"enabled": True, "profileId": identifier})
    (tmp_path / "seedbox.json").write_text(settings)
    endpoint = f"/api/profiles/{identifier}/server-selection"
    response = client.put(endpoint, json={"country": "Denmark"})
    resume = bool(desired or state == "connected")
    assert response.status_code == 200
    assert response.json()["reconnecting"] == resume
    assert response.json()["restartRequired"] is False
    assert events == ["client stopped", "tunnel stopped"]
    with sqlite3.connect(tmp_path / "mediavpn.sqlite") as db:
        assert db.execute("SELECT auto_country,desired FROM profiles").fetchone() == ("Denmark", int(resume))
    assert (tmp_path / "seedbox.json").read_text() == settings
    # Explicit re-selection of an active choice must also reconnect; a stopped
    # choice remains idempotent and never unexpectedly starts a tunnel.
    events.clear()
    assert client.put(endpoint, json={"country": "Denmark"}).status_code == 200
    assert events == (["client stopped", "tunnel stopped"] if resume else [])


def test_catalog_only_accepts_wireguard_forwarding_servers_with_public_ipv4():
    rows = [
        {"vpn": vpn, "country": country, "hostname": "node.protonvpn.net", "wgpubkey": PUBLIC, "port_forward": forwarding, "ips": ips}
        for vpn, country, forwarding, ips in [
            ("wireguard", "Denmark", True, ["8.8.8.8", "2606:4700:4700::1111"]),
            ("wireguard", "NoP2P", False, ["1.1.1.1"]),
            ("wireguard", "IPvSixOnly", True, ["2606:4700:4700::1111"]),
            ("wireguard", "Private", True, ["192.168.1.1"]),
            ("openvpn", "OtherProtocol", True, ["9.9.9.9"]),
        ]
    ]
    service = ProtonSelector(fetch=lambda: json.dumps({"timestamp": 1000, "servers": rows}).encode(), clock=lambda: 1001, probe=lambda ip: 1)
    assert service.countries() == ["Denmark"]
    assert service.choose("*")["ip"] == "8.8.8.8"


def test_load_ranks_before_latency_and_refresh_fetches_fresh_data():
    service = selector(lambda ip: 1 if ip == "8.8.8.8" else 20)
    loads = [80, 10]
    calls = []
    def fetch_load():
        calls.append(True)
        return json.dumps({"Code": 1000, "LogicalServers": [
            {"Load": load, "Features": 4, "Status": 1, "Servers": [{"Status": 1, "EntryIP": ip, "X25519PublicKey": PUBLIC}]}
            for ip, load in zip(["8.8.8.8", "1.1.1.1"], loads)
        ]}).encode()
    service.fetch_load = fetch_load
    assert service.choose("Denmark")["ip"] == "1.1.1.1"
    result = service.recommendations()
    assert result["loadAvailable"] and result["recommendations"][0]["loadPercent"] == 10
    assert service.recommendations() == result and len(calls) == 1
    loads[:] = [5, 90]
    fresh = service.recommendations(force=True)
    assert len(calls) == 2 and fresh["recommendations"][0]["loadPercent"] == 5
    assert fresh["recommendations"][0]["latencyMs"] == 1


def test_unavailable_or_invalid_load_never_becomes_zero_percent():
    service = selector()
    for data in [b'{"Code":401}', b'{"Code":1000,"LogicalServers":[{"Load":true}]}', b'x' * (24 * 1024 * 1024 + 1)]:
        service.fetch_load = lambda: data
        result = service.recommendations(force=True)
        assert not result["loadAvailable"] and result["loadMeasuredAt"] is None
        assert result["recommendations"][0]["loadPercent"] is None
    def unavailable():
        raise OSError("unavailable")
    service.fetch_load = unavailable
    assert not service.recommendations(force=True)["loadAvailable"]


def test_supervisor_reconnects_without_another_start_request(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from mediavpn.seedbox import Seedbox
    started = threading.Event()
    reconciled = threading.Event()
    monkeypatch.setattr(Seedbox, "stop", lambda self: None)
    monkeypatch.setattr(Seedbox, "reconcile", lambda self: reconciled.set() if started.is_set() else None)
    runtime = SimpleNamespace(enabled=True, status=lambda identifier: {"state": "connected"},
        container=lambda identifier: None, stop=lambda identifier: None,
        start=lambda identifier, **options: started.set() if options == {"port_forwarding": True, "auto_country": "Denmark"} else None)
    with TestClient(create_app(tmp_path, "http://testserver", runtime=runtime, selector=selector())) as client:
        login(client)
        identifier = client.post("/api/profiles", json={"name": "Test", "configuration": CONFIG, "port_forwarding": True}).json()["id"]
        assert client.put(f"/api/profiles/{identifier}/server-selection", json={"country": "Denmark"}).json()["reconnecting"]
        assert reconciled.wait(2), "Saving the selection must wake the supervisor immediately"
    # Desired state survives an app restart; no second user Start is needed.
    started.clear()
    reconciled.clear()
    with TestClient(create_app(tmp_path, "http://testserver", runtime=runtime, selector=selector())):
        assert reconciled.wait(2)
    with sqlite3.connect(tmp_path / "mediavpn.sqlite") as db:
        assert db.execute("SELECT desired FROM profiles").fetchone()[0] == 1


def test_recommendation_refresh_requires_login_csrf_and_bypasses_cache(tmp_path):
    service = selector()
    calls = []
    original = service.recommendations
    service.recommendations = lambda **kwargs: calls.append(kwargs) or original(**kwargs)
    client = TestClient(create_app(tmp_path, "http://testserver", selector=service))
    path = "/api/proton/recommendations/refresh"
    assert client.post(path, json={}).status_code == 401
    login(client)
    assert client.post(path, json={}, headers={"X-MediaVPN-CSRF": "wrong"}).status_code == 403
    assert client.post(path, json={}).status_code == 200
    assert calls == [{"force": True}]
